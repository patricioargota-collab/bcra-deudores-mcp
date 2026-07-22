# BCRA Central de Deudores — MCP

Conector MCP sobre la API pública del Banco Central de la República Argentina
para consultar situación crediticia por CUIT/CUIL/CDI.

- **Fuente**: `https://api.bcra.gob.ar/CentralDeDeudores/v1.0`
- **Consulta web equivalente**: https://www.bcra.gob.ar/situacion-crediticia/
- **Autenticación**: no requerida
- **Costo**: gratuito
- **Manual oficial**: https://www.bcra.gob.ar/archivos/Catalogo/Content/files/pdf/central-deudores-v1.pdf

## Herramientas expuestas

| Tool | Qué devuelve |
|---|---|
| `bcra_estado` | Health-check. No consulta datos de ninguna persona. |
| `bcra_deudas` | Situación crediticia del último período informado, entidad por entidad. |
| `bcra_deudas_historicas` | Serie de 24 meses + cálculo de tendencia (deterioro / mejora / estable). |
| `bcra_cheques_rechazados` | Cheques rechazados, causal, monto y estado de la multa. |
| `bcra_informe_consolidado` | Los tres anteriores en una llamada, con bloque de alertas graduadas. |

Todas aceptan el CUIT con o sin guiones o puntos.

## Valor agregado sobre la API cruda

- Decodifica el campo `situacion` (1 a 5) a su etiqueta normativa según el
  T.O. de Clasificación de Deudores del BCRA.
- **Convierte los montos**: el BCRA informa en *miles de pesos*. Se devuelven ambos valores.
- Levanta banderas explícitas para los encuadres especiales, en particular
  `situacionJuridica` (concurso, quiebra, gestión judicial o concordato), que es el
  dato de mayor relevancia para el Fisco.
- Calcula deuda total por período y peor clasificación vigente.
- Detecta deterioro de la clasificación a lo largo de la serie histórica.

## Despliegue en Railway

1. Crear repositorio nuevo en GitHub (cuenta `patricioargota-collab`), por ejemplo
   `bcra-deudores-mcp`, y subir los cuatro archivos: `server.py`, `requirements.txt`,
   `Dockerfile`, `.gitignore`.
   > Cuidado con el artefacto de renombrado del navegador (`server (3).py`). Si aparece,
   > crear el archivo a mano en el editor web de GitHub y pegar el contenido.
2. En Railway: **New Project → Deploy from GitHub repo** y seleccionar el repositorio.
3. Railway detecta el `Dockerfile`. No hace falta configurar variables de entorno:
   `PORT` la inyecta Railway automáticamente.
4. **Settings → Networking → Generate Domain**. Queda algo como
   `https://bcra-deudores-mcp-production.up.railway.app`.
5. En Claude, agregar el conector con la URL terminada en `/mcp`:
   `https://<dominio>.up.railway.app/mcp`
6. Abrir una conversación nueva y llamar `bcra_estado` para validar el handshake.

### Notas de configuración ya resueltas en el código

- `FastMCP(..., host="0.0.0.0", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))`
- Transporte `streamable-http` (con guion).
- El puerto se fija vía `mcp.settings.port`, no como argumento de `mcp.run()`.
- Sin instrucción `VOLUME` en el Dockerfile.

### Cold start

Igual que los otros conectores del ecosistema, Railway duerme el servicio. Si falla el
handshake, abrir una conversación nueva: la llamada previa despierta el servidor y la
sesión nueva captura el catálogo completo.

## Prueba local opcional

Para probar desde el contenedor de Claude hay que agregar `api.bcra.gob.ar` al
allowlist de egreso de red. Sin eso, la lógica interna se puede testear pero las
llamadas HTTP devuelven `Host not in allowlist`.

```bash
python server.py       # levanta en :8000
```

## Advertencias de uso

1. **La difusión de estos datos no implica conformidad del BCRA.** La información la
   suministran las entidades informantes.
2. **No acredita deuda tributaria ni patrimonio.** Solo refleja financiaciones del
   sistema financiero, más cheques rechazados.
3. **Ley 25.326.** Los campos `enRevision` (art. 16 inc. 6) y `procesoJud`
   (art. 38 inc. 3) señalan datos impugnados. Los derechos de rectificación se ejercen
   ante la entidad cedente, no ante el BCRA ni ante el organismo consultante.
4. **Para incorporar a un expediente administrativo**, verificar contra la consulta web
   oficial y dejar constancia de fecha y hora de la consulta. La salida del conector es
   insumo de trabajo, no constancia oficial.
