# Snapshot diario de rotura (n8n)

Foto diaria, por país, de las líneas de pedido de venta abiertas y vencidas (con stock y marca de albarán) y del
watermark de Almacén. El objetivo es el **histórico**: cada día añade ficheros y nunca borra los anteriores.
Origen: `snapshot_rotura-v03.py` (Jep); la lógica vive ahora en el workflow de n8n **"Snapshot rotura"**,
definido como código en `n8n/workflows.py`.

## Cómo funciona

Un solo workflow en n8n (rpa.artero.com) con dos triggers:

```
23:30  Países → bucle por país:
          Login SAP → guardar 4 SQLQueries → leer /List (paginado) → calcular → CSV a SharePoint → Logout
          si un paso falla: el país va a "Fallo" y el bucle sigue con el siguiente
       → resumen_<fecha>.json a SharePoint (países ok, fallidos y su error)
07:00  watchdog: lee el resumen de ayer y comprueba los CSV esperados → si falta algo, aviso en Teams
```

n8n llega a la red de SAP (10.80.13.x); no depende de ningún otro servidor.

| | |
|---|---|
| SharePoint (provisional) | sitio **SAP-MCP** › Documentos › `MCP-status/{rotura,watermark,log}` |
| Ficheros | `rotura_<pais>_<AAAA-MM-DD>.csv`, `watermark_<pais>_<AAAA-MM-DD>.csv`, `resumen_<AAAA-MM-DD>.json` |
| Login SAP | Usuario PRODUCTION en los campos `UserName`/`Password` del nodo **Login SAP**, como el resto de workflows de SAP |
| Credenciales n8n | `SAP SL - certificado (…)` (CA = certificado autofirmado del Service Layer, verificación TLS activa), `Microsoft SharePoint account 5` (OAuth delegado de un usuario) |
| Alertas | Teams, mismo canal que el monitor del MCP |
| Zona horaria | Europe/Madrid (la instancia está en America/New_York) |

## Criterios

- **Vencida** = `ShipDate <= hoy` (incluye lo que vence el mismo día). Pendiente de confirmar con Jep; cambiarlo rompe la comparabilidad del histórico.
- **StockOtrosAlm** = stock positivo total del artículo − stock del almacén de la línea (si es negativo, cuenta como 0).
- **TieneAlbaran** = el pedido (abierto) tiene algún albarán no cancelado.
- **Watermark** = por almacén, la `ShipDate` más reciente de las líneas servidas con albaranes de fecha de hoy.

## Operación

- **Añadir países:** constante `PAISES` del nodo **Países**. Antes, ejecutar el workflow a mano con solo ese país y revisar el CSV.
- **Relanzar hoy:** ejecutar a mano el trigger **Cada día 23:30**. Sustituye los ficheros de hoy de los países activos.
- **Un día pasado no se puede rehacer:** el snapshot es una foto del momento. Si falta, queda el hueco; anotarlo.
- **Reintentos:** cada llamada a SAP y SharePoint se reintenta 3 veces; si aun así falla, el país queda en `fallos` del resumen (con el error en `errores`) y el watchdog avisa a las 07:00.
- **Alerta "no hay resumen":** el workflow no llegó a terminar (n8n caído, workflow desactivado…). Ver sus ejecuciones.
- **Alerta "Falta X (HTTP 401/403)" en todo:** la credencial de SharePoint ha caducado o la han revocado → reconectarla.

## Cambiar el destino en SharePoint

Constantes `SITE` y `FOLDER` en los nodos **Países** y **Ayer**: cambiar los dos, aquí y en n8n.
El ID del sitio sale del selector de sitios del nodo SharePoint de n8n. Lo ya subido no se mueve solo.

## Caducidades y dependencias (apuntar en calendario)

- **Certificado del Service Layer: 16/11/2027.** Al renovarlo fallará el login de todos los países → actualizar la credencial `SAP SL - certificado`.
- **Credencial SharePoint de n8n:** OAuth de un usuario concreto. Si esa persona sale de la empresa o se le revocan las sesiones, deja de subir.
- **Contraseña de SAP (PRODUCTION):** compartida con el MCP y otros workflows; si cambia, actualizarla también en el nodo **Login SAP**.
