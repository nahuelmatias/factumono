"""Chequeo de estado de un ambiente: lo que usan `estado`, `ambiente usar` y la tool `estado_arca`.

Para activar producción tiene que dar todo OK: FEDummy, autenticación y que el punto de venta
configurado esté en `FEParamGetPtosVenta` (que solo lista los de web services), sin bloqueo ni baja.
"""

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from facturitas.arca.errors import ArcaError
from facturitas.arca.wsfe import PuntoVenta
from facturitas.contexto import Contexto


@dataclass
class Diagnostico:
    dummy: dict[str, str] | None = None
    dummy_ok: bool = False
    ticket_vence: datetime | None = None
    pvs: list[PuntoVenta] = field(default_factory=list)
    pv_configurado: int | None = None
    # Lo que impide usar producción. En homologación solo informa (la emisión funciona igual).
    problemas: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas


def esta_dado_de_baja(pv: PuntoVenta) -> bool:
    return bool(pv.fecha_baja) and pv.fecha_baja.upper() != "NULL"


def evaluar_punto_venta(
    pvs: list[PuntoVenta], configurado: int | None, produccion: bool
) -> tuple[list[str], list[str]]:
    """(problemas, avisos) sobre el punto de venta configurado. En homologación todo es aviso."""
    salida: list[str] = []
    if not configurado:
        opcion = " --ambiente prod" if produccion else ""
        salida.append(
            f"Falta definir el punto de venta: `facturitas init{opcion} --punto-venta N`."
        )
    elif not pvs and not produccion:
        pass  # en homologación la lista suele venir vacía y no impide emitir
    elif not pvs:
        salida.append(
            "ARCA no lista ningún punto de venta de web services para este CUIT. Hace falta "
            "uno de tipo «Factura Electrónica - ... - Web Services» (ARCA > ABM de puntos de "
            "venta)."
        )
    else:
        pv = next((p for p in pvs if p.nro == configurado), None)
        if pv is None:
            salida.append(
                f"El punto de venta configurado ({configurado}) no figura entre los de web "
                "services: se creó de otro tipo (p. ej. «Comprobantes en línea») o no existe."
            )
        elif pv.bloqueado:
            salida.append(f"El punto de venta {configurado} está bloqueado.")
        elif esta_dado_de_baja(pv):
            salida.append(f"El punto de venta {configurado} está dado de baja ({pv.fecha_baja}).")
    return ([], salida) if not produccion else (salida, [])


def diagnosticar(ctx: Contexto) -> Diagnostico:
    """Consulta FEDummy, autenticación y puntos de venta sin lanzar errores de ARCA o de red."""
    d = Diagnostico(pv_configurado=ctx.perfil.punto_venta)
    try:
        d.dummy = ctx.wsfe.dummy()
        d.dummy_ok = all(v == "OK" for v in d.dummy.values())
        if not d.dummy_ok:
            d.problemas.append(f"Los servidores de ARCA no responden bien: {d.dummy}.")
    except (ArcaError, httpx.HTTPError) as e:
        d.problemas.append(f"No se pudo consultar FEDummy: {e}")
        return d
    try:
        d.ticket_vence = ctx.wsaa.ticket("wsfe").vencimiento
    except (ArcaError, httpx.HTTPError) as e:
        d.problemas.append(f"No se pudo autenticar con ARCA: {e}")
        return d
    try:
        d.pvs = ctx.wsfe.puntos_venta()
    except (ArcaError, httpx.HTTPError) as e:
        d.problemas.append(f"No se pudieron listar los puntos de venta: {e}")
        return d
    problemas, avisos = evaluar_punto_venta(d.pvs, d.pv_configurado, ctx.es_produccion)
    d.problemas += problemas
    d.avisos += avisos
    return d
