"""Armado de comprobantes a partir de clientes guardados, períodos y 'la de siempre'.

Lo comparten el CLI y el MCP, para que "facturale a Acciona por septiembre" arme exactamente lo
mismo en los dos lados.
"""

import calendar
import re
import unicodedata
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from factumono.core.models import (
    CONCEPTO_TEXTO,
    Cliente,
    Comprobante,
    Concepto,
    Item,
    Perfil,
    Receptor,
    parsear_concepto,
    parsear_modo_periodo,
)
from factumono.storage.historial import Historial, Registro

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}  # fmt: skip


class _ResuelveCondicion(Protocol):
    def resolver_condicion_iva(self, texto: str, clase: str = "C") -> "_Condicion": ...


class _Condicion(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def descripcion(self) -> str: ...


def fin_de_mes(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def mes_completo(año: int, mes: int) -> tuple[date, date]:
    d = date(año, mes, 1)
    return d, fin_de_mes(d)


def _sin_acentos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def parsear_fecha(texto: str) -> date:
    texto = texto.strip()
    for sep, orden in (("/", "dmy"), ("-", "ymd")):
        partes = texto.split(sep)
        if len(partes) == 3 and all(p.isdigit() for p in partes):
            d = dict(zip(orden, map(int, partes), strict=True))
            return date(d["y"], d["m"], d["d"])
    raise ValueError(f"Fecha inválida: {texto!r}. Usá DD/MM/AAAA.")


def parsear_periodo(texto: str, hoy: date) -> tuple[date, date]:
    """'septiembre', 'septiembre 2026', '09/2026', '2026-09' o 'DD/MM/AAAA al DD/MM/AAAA'.

    Un mes sin año es el más reciente que no esté en el futuro (en octubre, "septiembre" es el
    de este año y "diciembre" el del año pasado).
    """
    t = _sin_acentos(texto.lower()).strip()
    rango = re.split(r"\s+(?:al|a|hasta)\s+|\s*(?:–|—)\s*", t)
    if len(rango) == 2 and rango[0] and rango[1]:
        desde, hasta = parsear_fecha(rango[0]), parsear_fecha(rango[1])
        if desde > hasta:
            raise ValueError("El período 'desde' no puede ser posterior al 'hasta'.")
        return desde, hasta
    if m := re.fullmatch(r"(\d{1,2})/(\d{4})", t):
        return mes_completo(int(m[2]), int(m[1]))
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", t):
        return mes_completo(int(m[1]), int(m[2]))
    if m := re.fullmatch(r"([a-z]+)(?:\s+(?:de\s+)?(\d{4}))?", t):
        mes = MESES.get(m[1])
        if mes:
            if m[2]:
                return mes_completo(int(m[2]), mes)
            año = hoy.year if mes <= hoy.month else hoy.year - 1
            return mes_completo(año, mes)
    raise ValueError(
        f"No entiendo el período {texto!r}. Ejemplos: 'septiembre 2026', '09/2026' o "
        "'01/09/2026 al 30/09/2026'."
    )


def sumar_mes(d: date, meses: int = 1) -> date:
    n = d.year * 12 + d.month - 1 + meses
    año, mes = divmod(n, 12)
    return date(año, mes + 1, min(d.day, calendar.monthrange(año, mes + 1)[1]))


def periodo_siguiente(desde: date, hasta: date) -> tuple[date, date]:
    """Corre el período un mes. Un mes completo da el mes completo siguiente; cualquier otro
    (por ejemplo un solo día) conserva sus días corridos un mes."""
    if desde.day == 1 and hasta == fin_de_mes(desde):
        año, mes0 = divmod(desde.year * 12 + desde.month, 12)  # mes siguiente (mes0 es base 0)
        return mes_completo(año, mes0 + 1)
    return sumar_mes(desde), sumar_mes(hasta)


def resolver_periodo(
    modo: str,
    concepto: Concepto,
    hoy: date,
    desde: date | None = None,
    hasta: date | None = None,
) -> tuple[date, date] | None:
    """El período facturado según el modo del cliente (o el que se indique explícitamente).

    - Productos: no llevan período (None).
    - Si se indican desde y hasta, ese rango gana sobre el modo.
    - mes_completo / auto: el mes en curso. dia_emision: solo el día de emisión.
    - rango: hay que indicar desde y hasta. sin_periodo: no corresponde a servicios.
    """
    if concepto == Concepto.PRODUCTOS:
        return None
    if (desde is None) != (hasta is None):
        raise ValueError("Indicá las dos fechas del período (desde y hasta), o ninguna.")
    if desde is not None and hasta is not None:
        if hasta < desde:
            raise ValueError("El fin del período no puede ser anterior al inicio.")
        return desde, hasta
    modo = parsear_modo_periodo(modo)
    if modo == "dia_emision":
        return hoy, hoy
    if modo == "rango":
        raise ValueError("Este cliente se factura por rango de fechas: indicá desde y hasta.")
    if modo == "sin_periodo":
        raise ValueError(
            "Los servicios necesitan período (ARCA lo exige). Este cliente está en «sin período»: "
            "cambialo a mes completo, día de emisión o rango, o facturá productos."
        )
    return hoy.replace(day=1), fin_de_mes(hoy)  # mes_completo y auto


def validar_modo_y_concepto(modo: str, concepto: "str | int | Concepto") -> tuple[str, str]:
    """Normaliza (modo, concepto) de un cliente y rechaza la combinación imposible: sin período
    solo vale para productos (ARCA exige período en servicios y en productos y servicios)."""
    m = parsear_modo_periodo(modo)
    c = parsear_concepto(concepto)
    if m == "sin_periodo" and c != Concepto.PRODUCTOS:
        raise ValueError(
            "«Sin período» solo vale para productos: los servicios (y productos y servicios) "
            "necesitan período."
        )
    return m, CONCEPTO_TEXTO[c]


def receptor_de_cliente(wsfe: _ResuelveCondicion, c: Cliente, clase: str = "C") -> Receptor:
    cond = wsfe.resolver_condicion_iva(c.condicion_iva, clase)
    return Receptor(
        doc_tipo=c.doc_tipo,
        doc_nro=c.doc_nro,
        nombre=c.nombre,
        condicion_iva_id=cond.id,
        condicion_iva=cond.descripcion,
        domicilio=c.domicilio,
    )


def armar_comprobante(
    *,
    perfil: Perfil,
    punto_venta: int,
    cbte_tipo: int,
    receptor: Receptor,
    items: tuple[Item, ...],
    concepto: "str | int | Concepto",
    hoy: date,
    desde: date | None = None,
    hasta: date | None = None,
    vto_pago: date | None = None,
    vto_dias: int = 4,
    condicion_venta: str = "Contado",
    modo_periodo: str = "mes_completo",
) -> Comprobante:
    conc = parsear_concepto(concepto)
    c = Comprobante(
        perfil=perfil,
        punto_venta=punto_venta,
        cbte_tipo=cbte_tipo,
        concepto=conc,
        receptor=receptor,
        items=items,
        fecha=hoy,
        condicion_venta=condicion_venta,
    )
    periodo = resolver_periodo(modo_periodo, conc, hoy, desde, hasta)
    if periodo:  # servicios (2) y productos y servicios (3): período y vencimiento obligatorios
        c = replace(
            c,
            periodo_desde=periodo[0],
            periodo_hasta=periodo[1],
            vto_pago=vto_pago or hoy + timedelta(days=vto_dias),
        )
    c.validar()
    return c


def item_de_cliente(c: Cliente, importe: Decimal, descripcion: str | None = None) -> Item:
    """Un ítem con los defaults del cliente: 1 × unidad × descripción habitual."""
    desc = descripcion or c.descripcion
    if not desc:
        raise ValueError(
            f"El cliente '{c.alias}' no tiene una descripción habitual: indicá la descripción."
        )
    return Item(descripcion=desc, cantidad=Decimal(1), precio_unitario=importe, unidad=c.unidad)


def ultima_factura(historial: Historial, cuit: str, ambiente: str, c: Cliente) -> Registro:
    r = historial.ultima_factura_a(cuit, ambiente, c.doc_tipo, c.doc_nro)
    if r is None:
        raise ValueError(
            f"No hay ninguna factura anterior a {c.nombre} en este ambiente para repetir. "
            "Indicá el importe y la descripción."
        )
    return r


def _periodo_de_la_de_siempre(modo: str, o: Comprobante, hoy: date) -> tuple[date, date]:
    assert o.periodo_desde and o.periodo_hasta
    if modo == "dia_emision":
        return hoy, hoy
    if modo == "rango":
        return sumar_mes(o.periodo_desde), sumar_mes(o.periodo_hasta)
    if modo == "mes_completo":
        # el mes siguiente al último facturado, siempre completo (aunque la última no lo fuera)
        siguiente = sumar_mes(o.periodo_hasta.replace(day=1))
        return mes_completo(siguiente.year, siguiente.month)
    if modo == "sin_periodo":
        raise ValueError(
            "La última factura es de servicios (lleva período) pero este cliente está en «sin "
            "período». Cambiá el modo del cliente."
        )
    return periodo_siguiente(o.periodo_desde, o.periodo_hasta)  # auto


def repetir_ultima(
    ultima: Registro,
    *,
    hoy: date,
    importe: Decimal | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    vto_dias: int = 4,
    modo: str = "auto",
) -> Comprobante:
    """La misma factura que `ultima`, con el período corrido (o el indicado) y fecha de hoy.

    El período nuevo depende del modo del cliente: mes completo → el mes siguiente al último
    facturado; día de emisión → hoy; rango → el mismo rango un mes después; sin período → ninguno
    (solo productos); auto → se deduce de la última factura.
    """
    o = ultima.comprobante
    items = o.items
    if importe is not None:
        if len(items) != 1 or items[0].cantidad != 1:
            raise ValueError(
                "La última factura tiene varios ítems o cantidades: no puedo cambiarle solo el "
                "importe. Indicá los ítems."
            )
        items = (replace(items[0], precio_unitario=importe),)
    if o.concepto == Concepto.PRODUCTOS or not (o.periodo_desde and o.periodo_hasta):
        return replace(o, items=items, fecha=hoy, extras={})
    if (desde is None) != (hasta is None):
        raise ValueError("Indicá las dos fechas del período (desde y hasta), o ninguna.")
    if desde is None or hasta is None:
        desde, hasta = _periodo_de_la_de_siempre(parsear_modo_periodo(modo), o, hoy)
    espera = (o.vto_pago - o.fecha).days if o.vto_pago else vto_dias
    return replace(
        o,
        items=items,
        fecha=hoy,
        periodo_desde=desde,
        periodo_hasta=hasta,
        vto_pago=hoy + timedelta(days=max(espera, 0)),
        extras={},
    )
