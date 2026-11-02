#!/usr/bin/env python3
"""
ARTERO — Snapshot diario de rotura de stock y watermark de Almacén (v03)
=======================================================================

Qué hace cada noche, para cada base de datos de SAP B1 (ES, UK, USA, FR):

  1. ROTURA: guarda todas las líneas de pedido de venta abiertas y vencidas
     (confirmadas o no), con el stock del artículo en el almacén de la línea
     y en el resto de almacenes, y si el pedido ya tiene algún albarán.
  2. WATERMARK: guarda, por almacén, la fecha de entrega más reciente de las
     líneas de pedido servidas hoy (albaranes del día).

Los ficheros se AÑADEN cada día; nunca se sobrescriben ni se borran:
    <ARTERO_SNAPSHOT_DIR>/rotura/rotura_<pais>_<AAAA-MM-DD>.csv
    <ARTERO_SNAPSHOT_DIR>/watermark/watermark_<pais>_<AAAA-MM-DD>.csv
    <ARTERO_SNAPSHOT_DIR>/log/resumen_<AAAA-MM-DD>.json

Cambios frente a v02:
  - Sin CURRENT_DATE en el SQL: la fecha de corte se pasa como literal.
  - Añade Confirmed, stock en el almacén de la línea y en el resto,
    y marca de albarán (prueba de que el pedido estaba validado).
  - Añade el watermark de Almacén.
  - Un fichero por país y día (si un país falla, los demás se guardan).

Uso
---
    python snapshot_rotura-v03.py                  # hoy, 4 países
    python snapshot_rotura-v03.py --country spain  # solo España
    python snapshot_rotura-v03.py --dry-run        # no escribe nada

Programación en el servidor (Linux, cron), todos los días a las 23:30:
    30 23 * * *  /usr/bin/python3 /opt/artero/snapshot_rotura-v03.py >> /opt/artero/log/cron.log 2>&1

Windows (Programador de tareas):
    schtasks /create /tn "Artero - Snapshot Rotura" /tr "python C:\\artero\\snapshot_rotura-v03.py" /sc daily /st 23:30 /ru SYSTEM

Credenciales (variables de entorno, nunca en el código):
    SAP_SL_USER, SAP_SL_PASS
    SAP_SL_URL_SPAIN, SAP_SL_DB_SPAIN   (y _UK, _USA, _FRANCE)
    ARTERO_SNAPSHOT_DIR                 (carpeta de salida)
"""

import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

COUNTRIES = ["spain", "uk", "usa", "france"]
OUT = Path(os.getenv("ARTERO_SNAPSHOT_DIR", "./snapshots"))

Q_ROTURA = """
SELECT T1."DocEntry", T1."DocNum", T1."DocDate", T1."CardCode", T1."CardName",
       T1."SlpCode", T1."Confirmed",
       T0."LineNum", T0."ItemCode", T0."Dscription", T0."WhsCode",
       T0."Quantity", T0."DelivrdQty", T0."OpenQty", T0."Price", T0."OpenSum",
       T0."ShipDate", T2."OnHand" AS "StockAlmLinea", T2."IsCommited" AS "ComprAlmLinea"
FROM RDR1 T0
INNER JOIN ORDR T1 ON T0."DocEntry" = T1."DocEntry"
LEFT JOIN OITW T2 ON T0."ItemCode" = T2."ItemCode" AND T0."WhsCode" = T2."WhsCode"
WHERE T0."LineStatus" = 'O' AND T1."DocStatus" = 'O' AND T1."CANCELED" = 'N'
  AND T0."OpenQty" > 0 AND T0."ShipDate" <= '{hoy}'
"""

Q_STOCK = """
SELECT T0."ItemCode", T0."WhsCode", T0."OnHand"
FROM OITW T0
WHERE T0."OnHand" > 0 AND T0."ItemCode" IN ({items})
"""

Q_ALBARANES = """
SELECT T0."BaseEntry"
FROM DLN1 T0 INNER JOIN ODLN T1 ON T0."DocEntry" = T1."DocEntry"
WHERE T0."BaseType" = 17 AND T1."CANCELED" = 'N' AND T0."BaseEntry" IN ({docs})
GROUP BY T0."BaseEntry"
"""

Q_WATERMARK = """
SELECT T0."WhsCode", T2."ShipDate"
FROM DLN1 T0
INNER JOIN ODLN T1 ON T0."DocEntry" = T1."DocEntry"
INNER JOIN RDR1 T2 ON T0."BaseEntry" = T2."DocEntry" AND T0."BaseLine" = T2."LineNum"
WHERE T0."BaseType" = 17 AND T1."CANCELED" = 'N' AND T1."DocDate" = '{hoy}'
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("rotura")


def env(name):
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Falta la variable de entorno {name}")
    return v


class SL:
    """Sesión mínima contra el Service Layer de SAP B1."""

    def __init__(self, country):
        self.base = env(f"SAP_SL_URL_{country.upper()}").rstrip("/")
        self.s = requests.Session()
        self.s.verify = False
        r = self.s.post(f"{self.base}/Login", json={
            "CompanyDB": env(f"SAP_SL_DB_{country.upper()}"),
            "UserName": env("SAP_SL_USER"), "Password": env("SAP_SL_PASS")}, timeout=60)
        r.raise_for_status()

    def query(self, sql, code="ARTSNAP"):
        """Ejecuta SQL con el mismo mecanismo que el conector MCP: se guarda
        como SQLQuery en el Service Layer y se lee con /List (paginado).
        El SQL debe respetar las reglas del Service Layer: sin funciones de
        fecha, sin aritmética entre columnas, sin SELECT *."""
        body = {"SqlCode": code, "SqlName": code, "SqlText": " ".join(sql.split())}
        r = self.s.patch(f"{self.base}/SQLQueries('{code}')", json=body, timeout=60)
        if r.status_code == 404:
            r = self.s.post(f"{self.base}/SQLQueries", json=body, timeout=60)
        r.raise_for_status()
        rows = []
        url = f"{self.base}/SQLQueries('{code}')/List"
        while url:
            r = self.s.get(url, headers={"Prefer": "odata.maxpagesize=5000"}, timeout=300)
            r.raise_for_status()
            p = r.json()
            rows += p.get("value", [])
            nxt = p.get("odata.nextLink") or p.get("@odata.nextLink")
            url = f"{self.base}/{nxt.lstrip('/')}" if nxt else None
        return rows

    def close(self):
        try:
            self.s.post(f"{self.base}/Logout", timeout=30)
        except Exception:  # noqa: BLE001
            pass


def chunks(seq, n=200):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def quote(values):
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def run_country(country, hoy, dry):
    sl = SL(country)
    try:
        rotura = sl.query(Q_ROTURA.format(hoy=hoy.strftime("%Y%m%d")), "ARTSNAP_ROT")
        items = {r["ItemCode"] for r in rotura if r.get("ItemCode")}
        docs = {r["DocEntry"] for r in rotura}

        stock = defaultdict(float)          # stock en TODOS los almacenes
        for part in chunks(items):
            for s in sl.query(Q_STOCK.format(items=quote(part)), "ARTSNAP_STK"):
                stock[s["ItemCode"]] += float(s["OnHand"] or 0)

        con_albaran = set()
        for part in chunks(docs):
            con_albaran |= {a["BaseEntry"] for a in sl.query(Q_ALBARANES.format(docs=",".join(map(str, part))), "ARTSNAP_ALB")}

        for r in rotura:
            alm = float(r.get("StockAlmLinea") or 0)
            r["StockOtrosAlm"] = round(stock.get(r["ItemCode"], 0.0) - alm, 4)
            r["TieneAlbaran"] = "Y" if r["DocEntry"] in con_albaran else "N"
            r["SnapshotDate"] = hoy.isoformat()
            r["Country"] = country

        wm = defaultdict(str)
        for d in sl.query(Q_WATERMARK.format(hoy=hoy.strftime("%Y%m%d")), "ARTSNAP_WM"):
            ship = str(d.get("ShipDate") or "")[:10]
            if ship > wm[d["WhsCode"]]:
                wm[d["WhsCode"]] = ship
        watermark = [{"SnapshotDate": hoy.isoformat(), "Country": country, "WhsCode": k,
                      "UltimaFechaEntregaServida": v} for k, v in sorted(wm.items())]

        if not dry:
            write(OUT / "rotura" / f"rotura_{country}_{hoy.isoformat()}.csv", rotura)
            write(OUT / "watermark" / f"watermark_{country}_{hoy.isoformat()}.csv", watermark)

        conf = [r for r in rotura if r.get("Confirmed") == "Y"]
        return {"lineas": len(rotura), "lineas_confirmadas": len(conf),
                "importe_confirmado": round(sum(float(r.get("OpenSum") or 0) for r in conf), 2),
                "almacenes_watermark": len(watermark)}
    finally:
        sl.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", choices=COUNTRIES)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    hoy = date.today()
    targets = [a.country] if a.country else COUNTRIES
    resumen, fallos = {}, []
    for c in targets:
        try:
            resumen[c] = run_country(c, hoy, a.dry_run)
            log.info("[%s] %s", c, resumen[c])
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] fallo: %s", c, exc)
            fallos.append(c)
    if not a.dry_run:
        (OUT / "log").mkdir(parents=True, exist_ok=True)
        (OUT / "log" / f"resumen_{hoy.isoformat()}.json").write_text(
            json.dumps({"fecha": hoy.isoformat(), "resumen": resumen, "fallos": fallos}, indent=2),
            encoding="utf-8")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
