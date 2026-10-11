"""Comprobante <-> dict JSON (para guardarlo completo en el historial y reimprimirlo después)."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any

from facturitas.core.models import (
    CbteAsociado,
    Comprobante,
    Concepto,
    Item,
    Perfil,
    Receptor,
)


def _fecha(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _de_fecha(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def a_dict(c: Comprobante) -> dict[str, Any]:
    perfil = asdict(c.perfil)
    perfil["inicio_actividades"] = _fecha(c.perfil.inicio_actividades)
    asoc = None
    if c.cbte_asoc:
        asoc = asdict(c.cbte_asoc)
        asoc["fecha"] = _fecha(c.cbte_asoc.fecha)
    return {
        "perfil": perfil,
        "punto_venta": c.punto_venta,
        "cbte_tipo": c.cbte_tipo,
        "concepto": int(c.concepto),
        "receptor": asdict(c.receptor),
        "items": [
            {
                "descripcion": i.descripcion,
                "cantidad": str(i.cantidad),
                "precio_unitario": str(i.precio_unitario),
                "unidad": i.unidad,
                "codigo": i.codigo,
                "bonificacion_pct": str(i.bonificacion_pct),
            }
            for i in c.items
        ],
        "fecha": c.fecha.isoformat(),
        "periodo_desde": _fecha(c.periodo_desde),
        "periodo_hasta": _fecha(c.periodo_hasta),
        "vto_pago": _fecha(c.vto_pago),
        "condicion_venta": c.condicion_venta,
        "moneda": c.moneda,
        "cotizacion": str(c.cotizacion),
        "extras": dict(c.extras),
        "cbte_asoc": asoc,
        "motivo": c.motivo,
    }


def de_dict(d: dict[str, Any]) -> Comprobante:
    p = dict(d["perfil"])
    p["inicio_actividades"] = _de_fecha(p.get("inicio_actividades"))
    asoc = None
    if d.get("cbte_asoc"):
        a = dict(d["cbte_asoc"])
        a["fecha"] = _de_fecha(a.get("fecha"))
        asoc = CbteAsociado(**a)
    return Comprobante(
        perfil=Perfil(**p),
        punto_venta=d["punto_venta"],
        cbte_tipo=d["cbte_tipo"],
        concepto=Concepto(d["concepto"]),
        receptor=Receptor(**d["receptor"]),
        items=tuple(
            Item(
                descripcion=i["descripcion"],
                cantidad=Decimal(i["cantidad"]),
                precio_unitario=Decimal(i["precio_unitario"]),
                unidad=i["unidad"],
                codigo=i["codigo"],
                bonificacion_pct=Decimal(i["bonificacion_pct"]),
            )
            for i in d["items"]
        ),
        fecha=date.fromisoformat(d["fecha"]),
        periodo_desde=_de_fecha(d.get("periodo_desde")),
        periodo_hasta=_de_fecha(d.get("periodo_hasta")),
        vto_pago=_de_fecha(d.get("vto_pago")),
        condicion_venta=d.get("condicion_venta", "Contado"),
        moneda=d.get("moneda", "PES"),
        cotizacion=Decimal(d.get("cotizacion", "1")),
        extras=dict(d.get("extras", {})),
        cbte_asoc=asoc,
        motivo=d.get("motivo", ""),
    )
