#!/usr/bin/env python3
"""Definición, como código, del workflow de n8n (rpa.artero.com) "Snapshot rotura" (eE3hVZbOvffS7naG).

    python n8n/workflows.py > wf.json

El JSON es el cuerpo para la API de n8n (PUT /api/v1/workflows/<id>). Dos ramas, cada una con su trigger:
    23:30  Países → bucle por país: Login SAP → 4 SQLQueries → /List → CSV → SharePoint → Logout
           (si un paso falla, el país va a "Fallo" y el bucle sigue) → resumen_<fecha>.json → mensaje en Teams
    07:00  watchdog: lee el resumen de ayer y los CSV esperados → si falta algo, Teams
Si se cambia en la UI, actualizar aquí. Lo que no se guarda: usuario/contraseña de SAP (nodo "Login SAP")
y la URL del webhook de Teams (TEAMS_WEBHOOK; nodos "Avisar en Teams" y "Post to Teams").
"""

import json
import os
import sys
import uuid

SP_CRED = {"microsoftSharePointOAuth2Api": {"id": "U5tv38ZYn2cyfs0C", "name": "Microsoft SharePoint account 5"}}
SSL_CRED = {"httpSslAuth": {"id": "ZaSzcLW3lOxpNBEi", "name": "SAP SL - certificado (ARTEROhap02, caduca 2027-11-16)"}}
SETTINGS = {"executionOrder": "v1", "timezone": "Europe/Madrid", "saveDataErrorExecution": "all",
            "saveDataSuccessExecution": "all", "saveManualExecutions": True}
Y_SNAP, Y_WD = 0, 620
TEAMS_WEBHOOK = os.getenv("TEAMS_WEBHOOK", "PASTE_TEAMS_WEBHOOK_URL_HERE")


def node(name, type_, version, pos, params, **extra):
    return {"id": str(uuid.uuid4()), "name": name, "type": type_, "typeVersion": version,
            "position": list(pos), "parameters": params, **extra}


def code(name, pos, js, **extra):
    return node(name, "n8n-nodes-base.code", 2, pos, {"jsCode": js}, **extra)


def link(to, index=0):
    return {"node": to, "type": "main", "index": index}


def chain(*names):
    return {a: {"main": [[link(b)]]} for a, b in zip(names, names[1:])}


def sp_put(name, pos, url, ctype, body, **extra):
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": "PUT", "url": url, "authentication": "predefinedCredentialType",
        "nodeCredentialType": "microsoftSharePointOAuth2Api", "sendBody": True, "contentType": "raw",
        "rawContentType": ctype, "body": body, "options": {"timeout": 120000}},
        credentials=SP_CRED, retryOnFail=True, maxTries=3, waitBetweenTries=5000, **extra)


def sp_get(name, pos, url):
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": "GET", "url": url, "authentication": "predefinedCredentialType",
        "nodeCredentialType": "microsoftSharePointOAuth2Api",
        "options": {"response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "text"}},
                    "timeout": 60000}}, credentials=SP_CRED)


def teams_post(name, pos):
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": "POST", "url": TEAMS_WEBHOOK, "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($json) }}", "options": {"timeout": 20000}},
        retryOnFail=True, maxTries=3, waitBetweenTries=5000)


def sap(name, pos, method, url, extra_params=None, cookie="={{ $('Consultas').item.json.cookie }}", **extra):
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": method, "url": url, "provideSslCertificates": True, "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "Cookie", "value": cookie}]}, **(extra_params or {})},
        credentials=SSL_CRED, **extra)


# ---------------------------------------------------------------- rama 23:30: snapshot

PAISES = r"""// Países activos: empezar con España; añadir 'uk', 'usa', 'france' cuando lleve 3-5 noches sin alertas.
const PAISES = ['spain'];
const DB = { spain: 'SBO_CARTERO', uk: 'SBO_ARTEROUK', usa: 'SBO_ARTEROUSA', france: 'SBO_ARTEROFR' };
const SL = 'https://10.80.13.139:50000/b1s/v2';
// Destino provisional (mismo que el nodo "Ayer" del watchdog: cambiar los dos a la vez)
const SITE = 'arteroa58778176.sharepoint.com,07434299-9f23-42fa-a3c4-c0f88b856053,eba9b00e-8192-4700-b5a8-193d773a0d76'; // Compras Department › canal privado Roturas
const FOLDER = 'Roturas';
const base = 'https://arteroa58778176.sharepoint.com/_api/v2.0/sites/' + SITE + '/drive/root:/' + FOLDER;
const d = $now.setZone('Europe/Madrid').toFormat('yyyy-MM-dd');
return PAISES.map(country => ({ json: { country, db: DB[country], d, sl: SL, base } }));"""

CONSULTAS = r"""// Sesión del Service Layer + las 4 consultas del snapshot (texto fijo salvo la fecha).
const { d, sl } = $('Por país').first().json;
const login = $input.first().json;
const route = [].concat(login.headers?.['set-cookie'] || []).map(c => c.split(';')[0]).find(c => c.startsWith('ROUTEID='));
const cookie = ['B1SESSION=' + login.body.SessionId, route].filter(Boolean).join('; ');
const hoy = d.replace(/-/g, '');
const Q = {
  ARTSNAP_ROT: 'SELECT T1."DocEntry", T1."DocNum", T1."DocDate", T1."CardCode", T1."CardName", T1."SlpCode", T1."Confirmed", ' +
    'T0."LineNum", T0."ItemCode", T0."Dscription", T0."WhsCode", T0."Quantity", T0."DelivrdQty", T0."OpenQty", T0."Price", T0."OpenSum", ' +
    'T0."ShipDate", T2."OnHand" AS "StockAlmLinea", T2."IsCommited" AS "ComprAlmLinea" ' +
    'FROM RDR1 T0 INNER JOIN ORDR T1 ON T0."DocEntry" = T1."DocEntry" ' +
    'LEFT JOIN OITW T2 ON T0."ItemCode" = T2."ItemCode" AND T0."WhsCode" = T2."WhsCode" ' +
    "WHERE T0.\"LineStatus\" = 'O' AND T1.\"DocStatus\" = 'O' AND T1.\"CANCELED\" = 'N' AND T0.\"OpenQty\" > 0 AND T0.\"ShipDate\" <= '" + hoy + "'",
  // Stock positivo total por artículo (todos los almacenes)
  ARTSNAP_STK: 'SELECT T0."ItemCode", SUM(T0."OnHand") AS "OnHand" FROM OITW T0 WHERE T0."OnHand" > 0 GROUP BY T0."ItemCode"',
  // Pedidos abiertos con algún albarán no cancelado
  ARTSNAP_ALB: 'SELECT T0."BaseEntry" FROM DLN1 T0 INNER JOIN ODLN T1 ON T0."DocEntry" = T1."DocEntry" ' +
    'INNER JOIN ORDR T2 ON T0."BaseEntry" = T2."DocEntry" ' +
    "WHERE T0.\"BaseType\" = 17 AND T1.\"CANCELED\" = 'N' AND T2.\"DocStatus\" = 'O' GROUP BY T0.\"BaseEntry\"",
  // Fecha de entrega de las líneas servidas hoy (watermark por almacén)
  ARTSNAP_WM: 'SELECT T0."WhsCode", T2."ShipDate" FROM DLN1 T0 INNER JOIN ODLN T1 ON T0."DocEntry" = T1."DocEntry" ' +
    'INNER JOIN RDR1 T2 ON T0."BaseEntry" = T2."DocEntry" AND T0."BaseLine" = T2."LineNum" ' +
    "WHERE T0.\"BaseType\" = 17 AND T1.\"CANCELED\" = 'N' AND T1.\"DocDate\" = '" + hoy + "'",
};
return Object.entries(Q).map(([code, sql]) => ({ json: { code, sql, cookie, sl } }));"""

COMPROBAR = r"""// PATCH guarda la consulta; si no existía (404), el POST la crea. Si ninguno funcionó,
// /List devolvería la consulta de ayer: mejor fallar.
const patch = $('Guardar SQL').all(), post = $input.all();
return $('Consultas').all().map((c, i) => {
  const p = patch[i].json.statusCode, q = post[i].json.statusCode;
  if (p >= 300 && q >= 300)
    throw new Error(c.json.code + ': no se pudo guardar la consulta (PATCH ' + p + ', POST ' + q + '): ' + JSON.stringify(post[i].json.body).slice(0, 300));
  return { json: c.json };
});"""

CALCULAR = r"""// Port de run_country (snapshot_rotura.py v04): une stock y albaranes a las líneas y genera los CSV.
const { country, d } = $('Por país').first().json;
// Cada página de /List viene de una de las 4 consultas: itemMatching la enlaza con la suya
const rows = Object.fromEntries($('Comprobar').all().map(c => [c.json.code, []]));
$input.all().forEach((page, k) => rows[$('Comprobar').itemMatching(k).json.code].push(...(page.json.value || [])));

const stock = Object.fromEntries(rows.ARTSNAP_STK.map(s => [s.ItemCode, Number(s.OnHand || 0)]));
const conAlbaran = new Set(rows.ARTSNAP_ALB.map(a => a.BaseEntry));
const rotura = rows.ARTSNAP_ROT;
for (const r of rotura) {
  // ARTSNAP_STK solo suma almacenes en positivo: restar un negativo inflaría el resto
  const alm = Math.max(Number(r.StockAlmLinea || 0), 0);
  r.StockOtrosAlm = Math.round(((stock[r.ItemCode] || 0) - alm) * 1e4) / 1e4;
  r.TieneAlbaran = conAlbaran.has(r.DocEntry) ? 'Y' : 'N';
  r.SnapshotDate = d;
  r.Country = country;
}
const wm = {};
for (const x of rows.ARTSNAP_WM) {
  const ship = String(x.ShipDate || '').slice(0, 10);
  if (ship > (wm[x.WhsCode] || '')) wm[x.WhsCode] = ship;
}
// Orden de columnas fijo (el del SELECT de v03): el Service Layer devuelve los campos en orden alfabético
const ROT_FIELDS = ['DocEntry', 'DocNum', 'DocDate', 'CardCode', 'CardName', 'SlpCode', 'Confirmed', 'LineNum', 'ItemCode', 'Dscription',
  'WhsCode', 'Quantity', 'DelivrdQty', 'OpenQty', 'Price', 'OpenSum', 'ShipDate', 'StockAlmLinea', 'ComprAlmLinea',
  'StockOtrosAlm', 'TieneAlbaran', 'SnapshotDate', 'Country'];
const WM_FIELDS = ['SnapshotDate', 'Country', 'WhsCode', 'UltimaFechaEntregaServida'];
const watermark = Object.keys(wm).sort().map(k => ({ SnapshotDate: d, Country: country, WhsCode: k, UltimaFechaEntregaServida: wm[k] }));

const cell = v => { const s = v == null ? '' : String(v); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
const toCsv = (rs, fields) => fields ? [fields, ...rs.map(r => fields.map(f => r[f]))].map(l => l.map(cell).join(',')).join('\r\n') + '\r\n' : '';
const conf = rotura.filter(r => r.Confirmed === 'Y');
const summary = { lineas: rotura.length, lineas_confirmadas: conf.length,
  importe_confirmado: Math.round(conf.reduce((s, r) => s + Number(r.OpenSum || 0), 0) * 100) / 100,
  almacenes_watermark: watermark.length };
return [
  { json: { name: 'rotura/rotura_' + country + '_' + d + '.csv', content: toCsv(rotura, ROT_FIELDS), summary } },
  { json: { name: 'watermark/watermark_' + country + '_' + d + '.csv', content: toCsv(watermark, WM_FIELDS), summary } },
];"""

RESULTADO = "return [{ json: { country: $('Por país').first().json.country, ok: true, ...$('Calcular').first().json.summary } }];"

FALLO = """// Un paso del país ha fallado: se apunta y el bucle sigue con el siguiente país
const e = $input.first().json.error;
return [{ json: { country: $('Por país').first().json.country, ok: false, error: String(e?.message || e || 'error').slice(0, 300) } }];"""

RESUMEN = """// Fin del bucle: un resultado por país (ok o fallo)
const { d, base } = $('Países').first().json;
const resumen = {}, fallos = [], errores = {};
for (const { json: r } of $input.all()) {
  if (r.ok) { const { country, ok, ...s } = r; resumen[country] = s; }
  else { fallos.push(r.country); errores[r.country] = r.error; }
}
return [{ json: { d, base, resumen, fallos, errores } }];"""

AVISO = r"""// Mensaje del día al canal: resumen por país y, si los hay, errores
const { d, resumen, fallos, errores } = $('Resumen').first().json;
const subida = $input.first().json.error;
const n = v => Number(v).toLocaleString('es-ES');
const eur = v => Number(v).toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €';
const ok = !fallos.length && !subida;
const t = (text, extra) => ({ type: 'TextBlock', text, wrap: true, ...extra });
const body = [
  t((ok ? '✅' : '⚠️') + ' Snapshot rotura ' + d, { weight: 'Bolder', size: 'Medium', color: ok ? 'Good' : 'Attention' }),
  ...Object.entries(resumen).map(([c, s]) => t('**' + c + '**: ' + n(s.lineas) + ' líneas vencidas · ' + n(s.lineas_confirmadas) +
    ' confirmadas (' + eur(s.importe_confirmado) + ') · watermark de ' + s.almacenes_watermark + ' almacenes')),
  ...fallos.map(c => t('**' + c + '**: falló, sin foto de hoy. ' + errores[c], { color: 'Attention' })),
];
if (subida) body.push(t('No se pudo subir el resumen del día a SharePoint: ' + String(subida.message || subida).slice(0, 300), { color: 'Attention' }));
if (!ok) body.push(t('Se puede relanzar a mano en n8n ("Snapshot rotura" › Cada día 23:30) antes de las 00:00; después el día queda sin foto.', { isSubtle: true }));
return [{ json: {
  type: 'message',
  attachments: [{ contentType: 'application/vnd.microsoft.card.adaptive', content: {
    type: 'AdaptiveCard', version: '1.4', $schema: 'http://adaptivecards.io/schemas/adaptive-card.json', body,
    actions: [{ type: 'Action.OpenUrl', title: 'Abrir carpeta', url: 'https://arteroa58778176.sharepoint.com/sites/ComprasDepartment-Roturas/Shared%20Documents/Roturas' }],
  } }],
} }];"""

# Pasos que pueden fallar: su salida de error lleva al nodo "Fallo"
CAN_FAIL = ("Login SAP", "Consultas", "Comprobar", "Leer resultados", "Calcular", "Subir a SharePoint")


def snapshot_branch():
    # Una petición cada vez: en paralelo sobre la misma sesión el Service Layer falla con -2035 al crear consultas
    one_by_one = {"batching": {"batch": {"batchSize": 1, "batchInterval": 0}}}
    never = {"timeout": 60000, "response": {"response": {"fullResponse": True, "neverError": True}}, **one_by_one}
    y = Y_SNAP - 200
    loop_chain = [
        # Como el resto de workflows de SAP: usuario y contraseña en el propio nodo (se rellenan en n8n, no aquí)
        node("Login SAP", "n8n-nodes-base.httpRequest", 4.2, (660, y), {
            "method": "POST", "url": "={{ $json.sl }}/Login", "provideSslCertificates": True, "sendBody": True,
            "bodyParameters": {"parameters": [{"name": "CompanyDB", "value": "={{ $json.db }}"},
                                              {"name": "UserName", "value": ""}, {"name": "Password", "value": ""}]},
            "options": {"timeout": 60000, "response": {"response": {"fullResponse": True}}}},
            credentials=SSL_CRED, retryOnFail=True, maxTries=3, waitBetweenTries=5000,
            notes="Rellenar UserName y Password (usuario PRODUCTION), igual que en el resto de workflows de SAP.",
            notesInFlow=True),
        code("Consultas", (880, y), CONSULTAS),
        sap("Guardar SQL", (1100, y), "PATCH", "={{ $json.sl }}/SQLQueries('{{ $json.code }}')", {
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ SqlCode: $json.code, SqlName: $json.code, SqlText: $json.sql }) }}",
            "options": never}),
        sap("Crear SQL", (1320, y), "POST", "={{ $('Consultas').item.json.sl }}/SQLQueries", {
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ SqlCode: $('Consultas').item.json.code, SqlName: $('Consultas').item.json.code, SqlText: $('Consultas').item.json.sql }) }}",
            "options": never}),
        code("Comprobar", (1540, y), COMPROBAR),
        node("Leer resultados", "n8n-nodes-base.httpRequest", 4.2, (1760, y), {
            "method": "GET", "url": "={{ $json.sl }}/SQLQueries('{{ $json.code }}')/List", "provideSslCertificates": True,
            "sendHeaders": True, "headerParameters": {"parameters": [
                {"name": "Cookie", "value": "={{ $json.cookie }}"}, {"name": "Prefer", "value": "odata.maxpagesize=5000"}]},
            # $skip = páginas leídas × tamaño de la anterior (no depende del tamaño de página que aplique SAP)
            "options": {"timeout": 300000, **one_by_one, "pagination": {"pagination": {
                "paginationMode": "updateAParameterInEachRequest",
                "parameters": {"parameters": [{"type": "qs", "name": "$skip",
                                               "value": "={{ $pageCount * $response.body.value.length }}"}]},
                "paginationCompleteWhen": "other",
                "completeExpression": "={{ !($response.body['@odata.nextLink'] || $response.body['odata.nextLink']) }}",
                "limitPagesFetched": True, "maxRequests": 200}}}},
            credentials=SSL_CRED),
        code("Calcular", (1980, y), CALCULAR),
        sp_put("Subir a SharePoint", (2200, y), "={{ $('Por país').first().json.base }}/{{ $json.name }}:/content",
               "text/csv", "={{ $json.content }}"),
        sap("Logout SAP", (2420, y), "POST", "={{ $('Consultas').first().json.sl }}/Logout", {"options": {"timeout": 30000}},
            cookie="={{ $('Consultas').first().json.cookie }}", executeOnce=True, onError="continueRegularOutput"),
        code("Resultado", (2640, y), RESULTADO),
    ]
    for n in loop_chain:
        if n["name"] in CAN_FAIL:
            n["onError"] = "continueErrorOutput"
    nodes = [
        node("Cada día 23:30", "n8n-nodes-base.scheduleTrigger", 1.2, (0, Y_SNAP),
             {"rule": {"interval": [{"field": "cronExpression", "expression": "30 23 * * *"}]}}),
        code("Países", (220, Y_SNAP), PAISES),
        node("Por país", "n8n-nodes-base.splitInBatches", 3, (440, Y_SNAP), {"options": {}}),
        *loop_chain,
        code("Fallo", (1540, Y_SNAP + 40), FALLO),
        code("Resumen", (660, Y_SNAP + 260), RESUMEN),
        sp_put("Subir resumen", (880, Y_SNAP + 260), "={{ $json.base }}/log/resumen_{{ $json.d }}.json:/content",
               "application/json",
               "={{ JSON.stringify({ fecha: $json.d, resumen: $json.resumen, fallos: $json.fallos, errores: $json.errores }, null, 2) }}",
               onError="continueRegularOutput"),   # sin resumen subido, el aviso sale igual (y lo dice)
        code("Aviso", (1100, Y_SNAP + 260), AVISO),
        teams_post("Avisar en Teams", (1320, Y_SNAP + 260)),
    ]
    conns = {
        "Cada día 23:30": {"main": [[link("Países")]]},
        "Países": {"main": [[link("Por país")]]},
        "Por país": {"main": [[link("Resumen")], [link("Login SAP")]]},   # salida 0 = terminado, 1 = siguiente país
        **chain("Resumen", "Subir resumen", "Aviso", "Avisar en Teams"),
        "Fallo": {"main": [[link("Por país")]]},
        "Resultado": {"main": [[link("Por país")]]},
    }
    for a, b in zip(loop_chain, loop_chain[1:]):
        conns[a["name"]] = {"main": [[link(b["name"])], [link("Fallo")]] if a["name"] in CAN_FAIL else [[link(b["name"])]]}
    return nodes, conns


# ---------------------------------------------------------------- rama 07:00: watchdog

AYER = """// Mismo destino que el nodo "Países" (cambiar los dos a la vez)
const SITE = 'arteroa58778176.sharepoint.com,07434299-9f23-42fa-a3c4-c0f88b856053,eba9b00e-8192-4700-b5a8-193d773a0d76'; // Compras Department › canal privado Roturas
const FOLDER = 'Roturas';
const d = $now.setZone('Europe/Madrid').minus({ days: 1 }).toFormat('yyyy-MM-dd');
return [{ json: { d, base: 'https://arteroa58778176.sharepoint.com/_api/v2.0/sites/' + SITE + '/drive/root:/' + FOLDER } }];"""

ESPERADOS = r"""// Qué ficheros deberían existir, según el resumen del día (países ejecutados + fallidos)
const { d, base } = $('Ayer').first().json;
const r = $input.first().json;
if (r.statusCode !== 200) {
  return [{ json: { problem: 'No hay resumen del ' + d + ' en SharePoint (HTTP ' + r.statusCode + '): la tarea no se ejecutó o no subió nada', url: base + '/log' } }];
}
const res = JSON.parse(String(r.data ?? r.body).replace(/^\uFEFF/, ''));
const fallos = res.fallos || [];
const paises = [...new Set([...Object.keys(res.resumen || {}), ...fallos])];
const out = [];
for (const c of paises) for (const k of ['rotura', 'watermark']) {
  const name = k + '/' + k + '_' + c + '_' + d + '.csv';
  out.push({ json: { name, url: base + '/' + name, fallos } });
}
return out.length ? out : [{ json: { problem: 'El resumen del ' + d + ' no tiene ningún país', url: base + '/log' } }];"""

EVALUAR = r"""const d = $('Ayer').first().json.d;
const esp = $('Esperados').all();
const res = $input.all();
const problemas = [];
const fallos = esp[0].json.fallos || [];
if (fallos.length) problemas.push('Países fallidos: ' + fallos.join(', '));
esp.forEach((e, i) => {
  if (e.json.problem) problemas.push(e.json.problem);
  else if (res[i].json.statusCode !== 200) problemas.push('Falta ' + e.json.name + ' (HTTP ' + res[i].json.statusCode + ')');
});
if (!problemas.length) return [];
return [{ json: {
  type: 'message',
  attachments: [{ contentType: 'application/vnd.microsoft.card.adaptive', content: {
    type: 'AdaptiveCard', version: '1.4', $schema: 'http://adaptivecards.io/schemas/adaptive-card.json',
    body: [
      { type: 'TextBlock', text: 'Snapshot rotura: el día ' + d + ' está incompleto', weight: 'Bolder', size: 'Medium', color: 'Attention', wrap: true },
      ...problemas.map(t => ({ type: 'TextBlock', text: '• ' + t, wrap: true })),
      { type: 'TextBlock', text: 'Un día que falta no se puede rehacer. Revisar en n8n las ejecuciones de "Snapshot rotura" (el resumen del día incluye el error de cada país fallido).', wrap: true, isSubtle: true },
    ] } }],
} }];"""


def watchdog_branch():
    y = Y_WD
    nodes = [
        node("Cada día 07:00", "n8n-nodes-base.scheduleTrigger", 1.2, (0, y),
             {"rule": {"interval": [{"field": "cronExpression", "expression": "0 7 * * *"}]}}),
        code("Ayer", (220, y), AYER),
        sp_get("Leer resumen", (440, y), "={{ $json.base }}/log/resumen_{{ $json.d }}.json:/content"),
        code("Esperados", (660, y), ESPERADOS),
        sp_get("Comprobar ficheros", (880, y), "={{ $json.url }}"),
        code("Evaluar", (1100, y), EVALUAR),
        teams_post("Post to Teams", (1320, y)),
    ]
    return nodes, chain(*[n["name"] for n in nodes])


# ---------------------------------------------------------------- notas en el lienzo

NOTAS = [
    ((-80, -160), 640, 300, 7, "## 1 · Cada noche, 23:30\n"
     "Un elemento por país activo (lista `PAISES` del nodo **Países**, junto con el destino en SharePoint). "
     "**Por país** los pasa al bucle de uno en uno."),
    ((600, -440), 2200, 560, 5, "## 2 · Bucle por país\n"
     "Login en SAP (usuario y contraseña en **Login SAP**) → guarda las 4 consultas en el Service Layer y lee sus resultados → "
     "**Calcular** genera `rotura_<país>_<fecha>.csv` y `watermark_<país>_<fecha>.csv` → se suben a SharePoint → logout.\n"
     "Si un paso falla, el país va a **Fallo** (se apunta el error) y el bucle sigue con el siguiente."),
    ((600, 140), 900, 340, 4, "## 3 · Cierre\n"
     "**Resumen** junta el resultado de todos los países → `log/resumen_<fecha>.json` → "
     "**Avisar en Teams** publica siempre una tarjeta en el canal Roturas (✅, o ⚠️ con los errores)."),
    ((-80, 500), 1560, 300, 3, "## 4 · Control, 07:00\n"
     "Comprueba que existen el resumen y los CSV de ayer. **Post to Teams** solo publica si falta algo o hubo países fallidos; "
     "si todo está bien, no hace nada."),
]


def notas():
    return [node(f"Nota {i}", "n8n-nodes-base.stickyNote", 1, pos,
                 {"content": text, "width": w, "height": h, "color": color})
            for i, (pos, w, h, color, text) in enumerate(NOTAS, 1)]


def snapshot():
    snap_nodes, snap_conns = snapshot_branch()
    wd_nodes, wd_conns = watchdog_branch()
    return {"name": "Snapshot rotura", "nodes": notas() + snap_nodes + wd_nodes,
            "connections": {**snap_conns, **wd_conns}, "settings": SETTINGS}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
