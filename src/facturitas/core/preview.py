"""Texto de preview de un comprobante, que se muestra antes de pedir el CAE."""

import textwrap

from facturitas.core import cuit as cuit_mod
from facturitas.core.fechas import mostrar as _fecha
from facturitas.core.models import NOMBRES_COMPROBANTE, Comprobante, Concepto
from facturitas.core.money import formatear_ar


def texto_preview(c: Comprobante, etiqueta_ambiente: str, numero: int) -> str:
    p, r = c.perfil, c.receptor
    nombre_cbte = NOMBRES_COMPROBANTE.get(c.cbte_tipo, f"Comprobante {c.cbte_tipo}")
    doc = (
        cuit_mod.formatear(str(r.doc_nro).zfill(11))
        if r.doc_tipo == 80
        else (f"DNI {r.doc_nro}" if r.doc_tipo == 96 else "sin identificar")
    )
    lineas = [
        "=" * 70,
        f" PREVIEW — {etiqueta_ambiente}",
        "=" * 70,
        f" Emisor:      {p.razon_social} — CUIT {cuit_mod.formatear(p.cuit)} ({p.condicion_iva})",
        f" Comprobante: {nombre_cbte} (cód. {c.cbte_tipo:03d})"
        f" — Pto. de venta {c.punto_venta:05d} — Nro. {numero:08d}",
        f" Fecha:       {_fecha(c.fecha)}",
        f" Receptor:    {r.nombre} — {doc}",
        f"              Condición IVA: {r.condicion_iva or r.condicion_iva_id}"
        f" (código {r.condicion_iva_id})",
    ]
    if r.domicilio:
        lineas.append(f"              Domicilio: {r.domicilio}")
    lineas.append(f" Condición de venta: {c.condicion_venta}")
    if c.cbte_asoc:
        a = c.cbte_asoc
        anulado = NOMBRES_COMPROBANTE.get(a.tipo, f"Comprobante {a.tipo}")
        lineas.append(f" Anula: {anulado} {a.punto_venta:05d}-{a.numero:08d} — Motivo: {c.motivo}")
    if c.concepto in (Concepto.SERVICIOS, Concepto.PRODUCTOS_Y_SERVICIOS):
        assert c.periodo_desde and c.periodo_hasta and c.vto_pago
        lineas.append(
            f" Período facturado: {_fecha(c.periodo_desde)} al {_fecha(c.periodo_hasta)}"
            f" — Vto. de pago: {_fecha(c.vto_pago)}"
        )
    lineas += [
        "-" * 70,
        " Cód  Descripción                    Cant. U.Medida        P.Unit.   Subtotal",
    ]
    for i in c.items:
        # La descripción se parte en varias líneas (no se trunca); los importes van en la primera.
        desc = textwrap.wrap(i.descripcion, 30, break_long_words=True) or [""]
        lineas.append(
            f" {i.codigo:<4} {desc[0]:<30} {i.cantidad:>5} {i.unidad[:14]:<14}"
            f" {formatear_ar(i.precio_unitario):>11} {formatear_ar(i.subtotal):>10}"
        )
        lineas += [f" {'':<4} {resto}" for resto in desc[1:]]
    lineas += [
        "-" * 70,
        f" TOTAL: $ {formatear_ar(c.total)}   (Factura C: sin discriminar IVA)",
        "=" * 70,
    ]
    return "\n".join(lineas)
