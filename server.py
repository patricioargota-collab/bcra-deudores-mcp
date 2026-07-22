"""
MCP - Central de Deudores del BCRA
Fuente oficial: https://api.bcra.gob.ar/CentralDeDeudores/v1.0
Documentacion: https://www.bcra.gob.ar/situacion-crediticia/

Uso institucional: Direccion General de Rentas de Tucuman - Asesoria Legal y Tecnica.
La informacion es suministrada por las entidades informantes. Su difusion no implica
conformidad del BCRA. Los montos se informan EN MILES DE PESOS.
"""

import os
import re
import logging
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bcra-deudores")

BASE = "https://api.bcra.gob.ar/CentralDeDeudores/v1.0"
TIMEOUT = 25.0
UA = "DGR-Tucuman-AsesoriaLegal/1.0 (MCP; contacto institucional)"

mcp = FastMCP(
    "BCRA Central de Deudores",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)

# ---------------------------------------------------------------- utilidades

SITUACIONES = {
    1: ("Normal", "Situacion normal. Cartera comercial: atencion regular. Consumo/vivienda: atraso menor a 31 dias."),
    2: ("Riesgo bajo / Seguimiento especial", "Cartera comercial: con seguimiento especial. Consumo/vivienda: riesgo bajo."),
    3: ("Riesgo medio / Con problemas", "Cartera comercial: con problemas. Consumo/vivienda: riesgo medio."),
    4: ("Riesgo alto / Alto riesgo de insolvencia", "Cartera comercial: alto riesgo de insolvencia. Consumo/vivienda: riesgo alto."),
    5: ("Irrecuperable", "Irrecuperable en ambas carteras."),
    6: ("Irrecuperable por disposicion tecnica", "Encuadre tecnico de irrecuperabilidad."),
}


def _normalizar_id(identificacion: str) -> str:
    """Acepta 30-70810672-7, 30708106727, con puntos o espacios."""
    limpio = re.sub(r"\D", "", str(identificacion))
    if len(limpio) != 11:
        raise ValueError(
            f"CUIT/CUIL/CDI invalido: se recibio '{identificacion}' "
            f"({len(limpio)} digitos). La API exige exactamente 11 digitos."
        )
    return limpio


def _get(path: str) -> dict:
    url = f"{BASE}/{path}"
    try:
        with httpx.Client(timeout=TIMEOUT, verify=True, follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": UA, "Accept": "application/json"})
    except httpx.HTTPError as e:
        return {"_error": "No se pudo contactar la API del BCRA.", "_detalle": str(e), "_url": url}

    if r.status_code == 404:
        return {"_sin_datos": True, "_mensaje": "El BCRA no registra datos para esa identificacion.", "_url": url}
    if r.status_code == 400:
        return {"_error": "Parametro erroneo segun el BCRA.", "_detalle": r.text[:400], "_url": url}
    if r.status_code >= 500:
        return {"_error": f"Error del servidor BCRA (HTTP {r.status_code}).", "_url": url}
    try:
        return r.json()
    except Exception:
        return {"_error": "Respuesta no interpretable como JSON.", "_detalle": r.text[:400], "_url": url}


def _fmt_monto(monto: Any) -> dict:
    """El BCRA informa en MILES de pesos."""
    try:
        m = float(monto)
    except (TypeError, ValueError):
        return {"miles_de_pesos": None, "pesos": None}
    return {"miles_de_pesos": m, "pesos": round(m * 1000, 2)}


def _enriquecer_entidad(e: dict) -> dict:
    sit = e.get("situacion")
    etiqueta, desc = SITUACIONES.get(sit, (f"Codigo {sit} no catalogado", ""))
    banderas = []
    if e.get("situacionJuridica"):
        banderas.append("SITUACION JURIDICA (concurso, quiebra, gestion judicial o concordato)")
    if e.get("irrecDisposicionTecnica"):
        banderas.append("IRRECUPERABLE POR DISPOSICION TECNICA")
    if e.get("refinanciaciones"):
        banderas.append("REFINANCIACIONES")
    if e.get("recategorizacionOblig"):
        banderas.append("RECATEGORIZACION OBLIGATORIA")
    if e.get("enRevision"):
        banderas.append("EN REVISION (Ley 25.326, art. 16 inc. 6)")
    if e.get("procesoJud"):
        banderas.append("SOMETIDA A PROCESO JUDICIAL (Ley 25.326, art. 38 inc. 3)")
    out = {
        "entidad": e.get("entidad"),
        "situacion_codigo": sit,
        "situacion": etiqueta,
        "situacion_detalle": desc,
        "monto": _fmt_monto(e.get("monto")),
        "dias_atraso": e.get("diasAtrasoPago"),
        "en_situacion_1_desde": e.get("fechaSit1"),
        "banderas": banderas,
    }
    return {k: v for k, v in out.items() if v is not None}


def _resumir_periodo(per: dict) -> dict:
    ents = [_enriquecer_entidad(e) for e in per.get("entidades", []) or []]
    total_miles = sum(
        (e["monto"]["miles_de_pesos"] or 0) for e in ents if e.get("monto")
    )
    peor = max((e.get("situacion_codigo") or 0) for e in ents) if ents else None
    return {
        "periodo": per.get("periodo"),
        "cantidad_entidades": len(ents),
        "peor_situacion": peor,
        "peor_situacion_etiqueta": SITUACIONES.get(peor, ("s/d", ""))[0] if peor else None,
        "deuda_total": _fmt_monto(total_miles),
        "entidades": ents,
    }


def _envolver(payload: dict, identificacion: str, endpoint: str) -> dict:
    if "_error" in payload or "_sin_datos" in payload:
        payload.setdefault("identificacion_consultada", identificacion)
        payload["_fuente"] = f"{BASE}/{endpoint}"
        return payload
    return payload


AVISO = (
    "Fuente: BCRA - Central de Deudores (API publica, sin autenticacion). "
    "La informacion es suministrada por las entidades informantes; su difusion no implica "
    "conformidad del BCRA. Montos originales EN MILES DE PESOS (se agrega conversion a pesos). "
    "Los derechos de rectificacion se ejercen ante la entidad cedente, no ante el BCRA."
)


# ------------------------------------------------------------------- tools

@mcp.tool()
def bcra_estado() -> dict:
    """Health-check del conector: verifica que la API del BCRA responda.

    Usar antes de una serie de consultas, en la linea de tfn_estado / ca_estado_indice.
    No consulta datos de ninguna persona: usa una identificacion de prueba invalida
    a proposito para comprobar unicamente que el servicio contesta.
    """
    r = _get("Deudas/00000000000")
    disponible = ("_error" not in r) or ("Parametro" in str(r.get("_detalle", "")))
    return {
        "servicio": "BCRA - Central de Deudores v1.0",
        "base_url": BASE,
        "disponible": bool(disponible),
        "autenticacion": "No requerida",
        "endpoints": [
            "Deudas/{identificacion} - ultimo periodo informado",
            "Deudas/Historicas/{identificacion} - ultimos 24 meses",
            "Deudas/ChequesRechazados/{identificacion} - cheques rechazados y multas",
        ],
        "respuesta_cruda": r,
        "_aviso": AVISO,
    }


@mcp.tool()
def bcra_deudas(identificacion: str) -> dict:
    """Situacion crediticia ACTUAL de un CUIT/CUIL/CDI en el sistema financiero.

    Devuelve, para el ultimo periodo informado, cada entidad acreedora con su
    clasificacion de deudor (situacion 1 a 5), monto, dias de atraso y los encuadres
    especiales (refinanciacion, recategorizacion obligatoria, situacion juridica,
    irrecuperable por disposicion tecnica, en revision, proceso judicial).

    Parametros:
      identificacion : CUIT/CUIL/CDI. Acepta con o sin guiones o puntos (ej. '30-70810672-7').

    Interpretacion de 'situacion': 1 normal, 2 riesgo bajo/seguimiento especial,
    3 riesgo medio/con problemas, 4 riesgo alto/alto riesgo de insolvencia,
    5 irrecuperable. La bandera 'SITUACION JURIDICA' indica concurso, quiebra,
    gestion judicial o concordato: es el dato de mayor relevancia para el Fisco.
    """
    try:
        ident = _normalizar_id(identificacion)
    except ValueError as e:
        return {"_error": str(e)}

    raw = _get(f"Deudas/{ident}")
    if "_error" in raw or "_sin_datos" in raw:
        return _envolver(raw, ident, f"Deudas/{ident}")

    res = raw.get("results", {}) or {}
    periodos = [_resumir_periodo(p) for p in (res.get("periodos") or [])]
    return {
        "identificacion": res.get("identificacion", ident),
        "denominacion": res.get("denominacion"),
        "periodos": periodos,
        "_fuente": f"{BASE}/Deudas/{ident}",
        "_aviso": AVISO,
    }


@mcp.tool()
def bcra_deudas_historicas(identificacion: str, solo_resumen: bool = False) -> dict:
    """Evolucion de la situacion crediticia en los ULTIMOS 24 MESES.

    Permite detectar deterioro progresivo: un contribuyente que pasa de situacion 1 o 2
    a 4 o 5 en el periodo es un indicador de degradacion patrimonial relevante para
    evaluar riesgo de incobrabilidad.

    Parametros:
      identificacion : CUIT/CUIL/CDI, con o sin guiones.
      solo_resumen   : True devuelve unicamente la serie periodo/peor-situacion/deuda-total,
                       sin el detalle entidad por entidad. Util para lecturas rapidas o
                       cuando se consultan varios CUIT.
    """
    try:
        ident = _normalizar_id(identificacion)
    except ValueError as e:
        return {"_error": str(e)}

    raw = _get(f"Deudas/Historicas/{ident}")
    if "_error" in raw or "_sin_datos" in raw:
        return _envolver(raw, ident, f"Deudas/Historicas/{ident}")

    res = raw.get("results", {}) or {}
    periodos = [_resumir_periodo(p) for p in (res.get("periodos") or [])]
    periodos.sort(key=lambda p: str(p.get("periodo") or ""))

    serie = [
        {
            "periodo": p["periodo"],
            "peor_situacion": p["peor_situacion"],
            "deuda_total_pesos": p["deuda_total"]["pesos"],
            "cantidad_entidades": p["cantidad_entidades"],
        }
        for p in periodos
    ]

    tendencia = None
    codigos = [s["peor_situacion"] for s in serie if s["peor_situacion"]]
    if len(codigos) >= 2:
        if codigos[-1] > codigos[0]:
            tendencia = "DETERIORO: la peor clasificacion empeoro respecto del inicio de la serie."
        elif codigos[-1] < codigos[0]:
            tendencia = "MEJORA: la peor clasificacion mejoro respecto del inicio de la serie."
        else:
            tendencia = "ESTABLE: la peor clasificacion no vario entre extremos de la serie."

    out = {
        "identificacion": res.get("identificacion", ident),
        "denominacion": res.get("denominacion"),
        "periodos_informados": len(serie),
        "serie": serie,
        "tendencia": tendencia,
        "_fuente": f"{BASE}/Deudas/Historicas/{ident}",
        "_aviso": AVISO,
    }
    if not solo_resumen:
        out["detalle_por_periodo"] = periodos
    return out


@mcp.tool()
def bcra_cheques_rechazados(identificacion: str) -> dict:
    """Cheques rechazados de un CUIT/CUIL/CDI, con causal, monto y estado de la multa.

    Discrimina por causal (sin fondos / defectos formales) y por entidad. Para personas
    humanas informa ademas si el cheque esta vinculado a una persona juridica, dato util
    para conectar a un administrador con la sociedad que administra.

    Parametros:
      identificacion : CUIT/CUIL/CDI, con o sin guiones.
    """
    try:
        ident = _normalizar_id(identificacion)
    except ValueError as e:
        return {"_error": str(e)}

    raw = _get(f"Deudas/ChequesRechazados/{ident}")
    if "_error" in raw or "_sin_datos" in raw:
        return _envolver(raw, ident, f"Deudas/ChequesRechazados/{ident}")

    res = raw.get("results", {}) or {}
    causales = []
    total_monto = 0.0
    total_cheques = 0
    impagos = 0

    for c in (res.get("causales") or []):
        ents = []
        for e in (c.get("entidades") or []):
            det = []
            for d in (e.get("detalle") or []):
                total_cheques += 1
                try:
                    total_monto += float(d.get("monto") or 0)
                except (TypeError, ValueError):
                    pass
                if (d.get("estadoMulta") or "").upper() == "IMPAGA":
                    impagos += 1
                det.append({
                    "nro_cheque": d.get("nroCheque"),
                    "fecha_rechazo": d.get("fechaRechazo"),
                    "monto_pesos": d.get("monto"),
                    "fecha_pago": d.get("fechaPago"),
                    "fecha_pago_multa": d.get("fechaPagoMulta"),
                    "estado_multa": d.get("estadoMulta"),
                    "cuenta_personal": d.get("ctaPersonal"),
                    "denominacion_juridica_vinculada": d.get("denomJuridica"),
                    "en_revision": d.get("enRevision"),
                    "proceso_judicial": d.get("procesoJud"),
                })
            ents.append({"entidad_agrupamiento": e.get("entidad"), "detalle": det})
        causales.append({"causal": c.get("causal"), "entidades": ents})

    return {
        "identificacion": res.get("identificacion", ident),
        "denominacion": res.get("denominacion"),
        "resumen": {
            "total_cheques_rechazados": total_cheques,
            "monto_total_pesos": round(total_monto, 2),
            "multas_impagas": impagos,
        },
        "causales": causales,
        "_fuente": f"{BASE}/Deudas/ChequesRechazados/{ident}",
        "_aviso": AVISO,
    }


@mcp.tool()
def bcra_informe_consolidado(identificacion: str) -> dict:
    """Informe unico que integra los tres endpoints para un CUIT/CUIL/CDI.

    Ejecuta en una sola llamada: situacion actual, evolucion de 24 meses y cheques
    rechazados, y devuelve ademas un bloque de ALERTAS con lectura orientada al
    interes fiscal (situacion juridica, irrecuperabilidad, deterioro, multas impagas).

    Es la herramienta recomendada cuando se releva un contribuyente por primera vez.
    Para lotes de CUIT conviene usar bcra_deudas o bcra_deudas_historicas con
    solo_resumen=True, que devuelven menos volumen.

    Parametros:
      identificacion : CUIT/CUIL/CDI, con o sin guiones.
    """
    try:
        ident = _normalizar_id(identificacion)
    except ValueError as e:
        return {"_error": str(e)}

    actual = bcra_deudas(ident)
    historico = bcra_deudas_historicas(ident, solo_resumen=True)
    cheques = bcra_cheques_rechazados(ident)

    alertas = []

    # Alertas sobre situacion actual
    for p in (actual.get("periodos") or []):
        for e in p.get("entidades", []):
            for b in e.get("banderas", []):
                alertas.append({
                    "nivel": "ALTO" if ("JURIDICA" in b or "IRRECUPERABLE" in b) else "MEDIO",
                    "periodo": p.get("periodo"),
                    "entidad": e.get("entidad"),
                    "detalle": b,
                })
            if (e.get("situacion_codigo") or 0) >= 4:
                alertas.append({
                    "nivel": "ALTO",
                    "periodo": p.get("periodo"),
                    "entidad": e.get("entidad"),
                    "detalle": f"Clasificado en situacion {e.get('situacion_codigo')} - {e.get('situacion')}",
                })

    if historico.get("tendencia", "").startswith("DETERIORO"):
        alertas.append({"nivel": "ALTO", "detalle": historico["tendencia"]})

    ch_res = (cheques.get("resumen") or {})
    if ch_res.get("total_cheques_rechazados"):
        alertas.append({
            "nivel": "ALTO" if ch_res.get("multas_impagas") else "MEDIO",
            "detalle": (
                f"{ch_res['total_cheques_rechazados']} cheque(s) rechazado(s) por "
                f"${ch_res.get('monto_total_pesos')}, con {ch_res.get('multas_impagas')} multa(s) impaga(s)."
            ),
        })

    denominacion = (
        actual.get("denominacion")
        or historico.get("denominacion")
        or cheques.get("denominacion")
    )

    return {
        "identificacion": ident,
        "denominacion": denominacion,
        "alertas": alertas or [{"nivel": "SIN ALERTAS", "detalle": "No se detectaron encuadres especiales ni cheques rechazados."}],
        "situacion_actual": actual,
        "evolucion_24_meses": historico,
        "cheques_rechazados": cheques,
        "_aviso": AVISO,
        "_limite": (
            "Este informe NO acredita deuda tributaria ni patrimonio. Refleja unicamente "
            "financiaciones informadas por entidades del sistema financiero. Para uso en "
            "actuaciones administrativas debe verificarse contra la consulta web oficial "
            "https://www.bcra.gob.ar/situacion-crediticia/ y dejarse constancia de fecha y hora."
        ),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
