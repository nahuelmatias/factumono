"""Emisión con historial, idempotencia y reconciliación.

Regla de oro (CLAUDE.md §5): si se corta la comunicación después de mandar `FECAESolicitar`,
*antes de reintentar* se le pregunta a ARCA (`FECompConsultar`) si el comprobante ya quedó
autorizado. El número a usar se guarda en el historial antes de enviar, así que aunque el proceso
muera a mitad de camino se puede reconciliar después.
"""

from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from facturitas.arca.errors import ArcaError, ComprobanteRechazado, EstadoIncierto
from facturitas.arca.wsfe import ComprobanteConsultado, ResultadoCae
from facturitas.core.fechas import de_arca
from facturitas.core.models import (
    NOTA_DE_CREDITO_DE,
    CbteAsociado,
    Comprobante,
    Concepto,
)
from facturitas.storage.historial import (
    AUTORIZADO,
    FALLIDO,
    INCIERTO,
    PENDIENTE,
    RECHAZADO,
    Historial,
    Registro,
)

# Cuántos números posteriores al reservado se revisan al reconciliar (por si otro proceso
# emitió en el medio y nuestro comprobante quedó con un número más alto).
_VENTANA_RECONCILIACION = 50


class WsfeEmision(Protocol):
    def proximo_numero(self, punto_venta: int, cbte_tipo: int) -> int: ...
    def ultimo_autorizado(self, punto_venta: int, cbte_tipo: int) -> int: ...
    def solicitar_cae(self, c: Comprobante, numero: int) -> ResultadoCae: ...
    def consultar(
        self, punto_venta: int, cbte_tipo: int, numero: int
    ) -> ComprobanteConsultado | None: ...


@dataclass(frozen=True)
class Emision:
    registro: Registro
    reutilizado: bool = False  # la clave de idempotencia ya estaba autorizada: no se emitió otro
    reconciliado: bool = False  # se confirmó con ARCA que ya estaba autorizado tras un corte


def coincide(consultado: ComprobanteConsultado, c: Comprobante) -> bool:
    """¿Lo que ARCA tiene registrado es este comprobante?"""
    return (
        consultado.resultado == "A"
        and consultado.doc_tipo == c.receptor.doc_tipo
        and consultado.doc_nro == c.receptor.doc_nro
        and consultado.importe_total == c.total
        and consultado.fecha == c.fecha
        and consultado.concepto == int(c.concepto)
    )


def reconciliar(historial: Historial, wsfe: WsfeEmision, reg: Registro) -> Registro:
    """Resuelve un comprobante pendiente/incierto consultando a ARCA.

    Devuelve el registro `autorizado` (con CAE) si ARCA lo tiene, o `fallido` si confirma que
    no se emitió. Si no se puede consultar (sin conexión) levanta la excepción de red y el
    registro queda como está.
    """
    if reg.estado not in (PENDIENTE, INCIERTO):
        return reg
    if reg.numero is None:
        return historial.actualizar(reg.id, estado=FALLIDO, error="Nunca se llegó a numerar.")
    c = reg.comprobante
    ultimo = wsfe.ultimo_autorizado(reg.punto_venta, reg.cbte_tipo)
    ya_registrados = {
        r.numero
        for r in historial.autorizados(
            reg.cuit_emisor, reg.ambiente, reg.punto_venta, reg.cbte_tipo
        )
    }
    tope = min(ultimo, reg.numero + _VENTANA_RECONCILIACION)
    for n in range(reg.numero, tope + 1):
        if n in ya_registrados:
            continue  # ese número ya es de otro comprobante nuestro
        encontrado = wsfe.consultar(reg.punto_venta, reg.cbte_tipo, n)
        if encontrado and coincide(encontrado, c):
            return historial.actualizar(
                reg.id,
                estado=AUTORIZADO,
                numero=n,
                cae=encontrado.cae,
                cae_vto=de_arca(encontrado.cae_vto) if encontrado.cae_vto else None,
                error="",
            )
    return historial.actualizar(
        reg.id,
        estado=FALLIDO,
        error="ARCA confirma que este comprobante no fue autorizado: se puede emitir de nuevo.",
    )


def reconciliar_pendientes(
    historial: Historial, wsfe: WsfeEmision, cuit: str, ambiente: str
) -> list[Registro]:
    """Reconcilia todo lo que quedó sin resolver de un emisor. Devuelve los registros resueltos."""
    return [reconciliar(historial, wsfe, r) for r in historial.sin_resolver(cuit, ambiente)]


def emitir(
    historial: Historial,
    wsfe: WsfeEmision,
    *,
    perfil: str,
    ambiente: str,
    c: Comprobante,
    clave: str,
) -> Emision:
    """Pide el CAE con idempotencia. Llamar solo después de que el usuario confirmó el preview.

    - Misma `clave` ya autorizada: devuelve ese comprobante, no emite otro.
    - Misma `clave` pendiente/incierta: reconcilia con ARCA antes de volver a enviar.
    - Antes de numerar, reconcilia cualquier otro comprobante sin resolver del mismo emisor.
    """
    c.validar()
    previo = historial.por_clave(clave)
    if previo:
        if (previo.cuit_emisor, previo.ambiente) != (c.perfil.cuit, ambiente):
            raise ValueError("Esa clave de idempotencia ya se usó con otro emisor o ambiente.")
        if previo.estado == AUTORIZADO:
            return Emision(previo, reutilizado=True)
        previo = reconciliar(historial, wsfe, previo)
        if previo.estado == AUTORIZADO:
            return Emision(previo, reconciliado=True)
        reg = historial.actualizar(previo.id, comprobante=c, estado=PENDIENTE, error="")
    else:
        asociado = None
        if c.cbte_asoc:
            a = c.cbte_asoc
            asociado = historial.por_numero(
                c.perfil.cuit, ambiente, a.punto_venta, a.tipo, a.numero
            )
        reg = historial.crear(
            perfil=perfil,
            ambiente=ambiente,
            c=c,
            clave=clave,
            asociado_id=asociado.id if asociado else None,
        )

    try:
        for otro in historial.sin_resolver(c.perfil.cuit, ambiente):
            if otro.id != reg.id:
                reconciliar(historial, wsfe, otro)
        numero = wsfe.proximo_numero(c.punto_venta, c.cbte_tipo)
    except Exception as e:
        historial.actualizar(reg.id, estado=FALLIDO, error=f"No se llegó a enviar: {e}")
        raise
    reg = historial.actualizar(reg.id, numero=numero, estado=PENDIENTE)

    try:
        r = wsfe.solicitar_cae(c, numero)
    except EstadoIncierto as e:
        reg = historial.actualizar(reg.id, estado=INCIERTO, error=str(e))
        try:
            reg = reconciliar(historial, wsfe, reg)
        except Exception as e2:  # sin conexión todavía: queda incierto
            raise EstadoIncierto(
                f"{e}\nTampoco se pudo consultar a ARCA ({e2}). El comprobante {numero} quedó "
                "marcado como INCIERTO: cuando haya conexión corré `facturitas reconciliar`. "
                "No lo emitas de nuevo antes."
            ) from None
        if reg.estado == AUTORIZADO:
            return Emision(reg, reconciliado=True)
        raise ArcaError(
            "Se cortó la comunicación al pedir el CAE, pero ARCA confirma que el comprobante "
            f"{numero} NO se emitió. Podés volver a emitirlo sin riesgo de duplicarlo."
        ) from None
    except ComprobanteRechazado as e:
        historial.actualizar(reg.id, estado=RECHAZADO, error=str(e))
        raise
    except ArcaError as e:
        historial.actualizar(reg.id, estado=RECHAZADO, error=str(e))
        raise

    reg = historial.actualizar(
        reg.id,
        estado=AUTORIZADO,
        numero=r.numero,
        cae=r.cae,
        cae_vto=de_arca(r.cae_vto) if r.cae_vto else None,
        observaciones=r.observaciones,
        error="",
    )
    return Emision(reg)


def nota_de_credito(
    historial: Historial, original: Registro, motivo: str, hoy: date | None = None
) -> Comprobante:
    """Arma la nota de crédito que anula por completo una factura autorizada."""
    hoy = hoy or date.today()
    if original.estado != AUTORIZADO or original.numero is None:
        raise ValueError("Solo se puede anular un comprobante autorizado.")
    if original.cbte_tipo not in NOTA_DE_CREDITO_DE:
        raise ValueError("Ese comprobante no es una factura: no se anula con una nota de crédito.")
    if not motivo.strip():
        raise ValueError("Indicá el motivo de la nota de crédito.")
    o = original.comprobante
    nc = replace(
        o,
        cbte_tipo=NOTA_DE_CREDITO_DE[original.cbte_tipo],
        fecha=hoy,
        cbte_asoc=CbteAsociado(
            tipo=original.cbte_tipo,
            punto_venta=original.punto_venta,
            numero=original.numero,
            cuit=original.cuit_emisor,
            fecha=original.fecha,
        ),
        motivo=motivo.strip(),
        extras={},
    )
    if nc.concepto in (Concepto.SERVICIOS, Concepto.PRODUCTOS_Y_SERVICIOS):
        nc = replace(nc, vto_pago=hoy)
    return nc
