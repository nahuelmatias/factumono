"""Historial multi-PC: detectar y completar lo que se emitió fuera de esta PC.

La numeración nunca se pisa (siempre se consulta `FECompUltimoAutorizado`), pero si se emite desde
otra PC u otro sistema con el mismo punto de venta, el historial local queda incompleto. Acá se
compara lo que tiene ARCA con lo que tiene el historial y se completa con `FECompConsultar`
(ARCA solo guarda los totales: ítems y datos del receptor no se pueden recuperar).
"""

from dataclasses import dataclass
from decimal import Decimal

from factumono.contexto import Contexto
from factumono.core.fechas import de_arca
from factumono.core.models import (
    NOTA_DE_CREDITO_DE,
    Comprobante,
    Concepto,
    DocTipo,
    Item,
    Receptor,
)
from factumono.storage.historial import AUTORIZADO, Registro

DESCRIPCION_RECUPERADO = "Comprobante recuperado de ARCA (sin detalle de ítems)"


@dataclass(frozen=True)
class Brecha:
    """Números que ARCA tiene autorizados y el historial local no (de un tipo y punto de venta)."""

    punto_venta: int
    cbte_tipo: int
    ultimo_arca: int
    faltantes: tuple[int, ...]

    @property
    def cantidad(self) -> int:
        return len(self.faltantes)


def tipos_a_vigilar(ctx: Contexto) -> list[int]:
    """La factura del perfil y la nota de crédito que la anula."""
    factura = ctx.perfil.cbte_tipo_default
    return [factura, NOTA_DE_CREDITO_DE[factura]]


def brechas(ctx: Contexto) -> list[Brecha]:
    """Brechas del punto de venta del perfil (vacío si no hay nada afuera)."""
    assert ctx.historial is not None
    pv = ctx.perfil.punto_venta
    if not pv:
        return []
    salida = []
    for tipo in tipos_a_vigilar(ctx):
        ultimo = ctx.wsfe.ultimo_autorizado(pv, tipo)
        locales = {
            r.numero
            for r in ctx.historial.autorizados(ctx.perfil.cuit, ctx.ambiente, pv, tipo)
            if r.numero is not None
        }
        faltan = tuple(n for n in range(1, ultimo + 1) if n not in locales)
        if faltan:
            salida.append(Brecha(pv, tipo, ultimo, faltan))
    return salida


def total_faltantes(bs: list[Brecha]) -> int:
    return sum(b.cantidad for b in bs)


def recuperar_numero(
    ctx: Contexto,
    punto_venta: int,
    tipo: int,
    numero: int,
    *,
    descripcion: str = DESCRIPCION_RECUPERADO,
    receptor_nombre: str | None = None,
) -> Registro | None:
    """Trae de ARCA un comprobante y lo registra en el historial. None si ARCA no lo tiene."""
    assert ctx.historial is not None
    r = ctx.wsfe.consultar(punto_venta, tipo, numero)
    if r is None or r.resultado != "A":
        return None
    condiciones = {c.id: c.descripcion for c in ctx.wsfe.condiciones_iva_receptor("C")}
    cond_id = r.condicion_iva_id or 5
    es_cf = r.doc_tipo == int(DocTipo.CONSUMIDOR_FINAL)
    c = Comprobante(
        perfil=ctx.perfil,
        punto_venta=punto_venta,
        cbte_tipo=tipo,
        concepto=Concepto(r.concepto),
        receptor=Receptor(
            doc_tipo=r.doc_tipo,
            doc_nro=r.doc_nro,
            nombre=receptor_nombre or ("Consumidor Final" if es_cf else "(sin datos)"),
            condicion_iva_id=cond_id,
            condicion_iva=condiciones.get(cond_id, ""),
        ),
        items=(
            Item(
                descripcion=descripcion,
                cantidad=Decimal(1),
                precio_unitario=r.importe_total,
                unidad="otras unidades",
            ),
        ),
        fecha=r.fecha,
        periodo_desde=r.periodo_desde,
        periodo_hasta=r.periodo_hasta,
        vto_pago=r.vto_pago,
    )
    reg = ctx.historial.crear(
        perfil=ctx.perfil_nombre,
        ambiente=ctx.ambiente,
        c=c,
        clave=f"recuperado-{ctx.ambiente}-{ctx.perfil.cuit}-{punto_venta}-{tipo}-{numero}",
        estado=AUTORIZADO,
        numero=numero,
        recuperado=True,
    )
    return ctx.historial.actualizar(
        reg.id, cae=r.cae, cae_vto=de_arca(r.cae_vto) if r.cae_vto else None
    )


def recuperar_brechas(ctx: Contexto, bs: list[Brecha]) -> tuple[list[Registro], list[int]]:
    """Completa el historial con todo lo que falta. Devuelve (recuperados, no_encontrados)."""
    recuperados: list[Registro] = []
    no_encontrados: list[int] = []
    for b in bs:
        for n in b.faltantes:
            reg = recuperar_numero(ctx, b.punto_venta, b.cbte_tipo, n)
            if reg is None:
                no_encontrados.append(n)
            else:
                recuperados.append(reg)
    return recuperados, no_encontrados
