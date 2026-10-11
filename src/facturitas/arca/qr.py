"""QR del comprobante (RG 4892): URL con el JSON del comprobante en base64.

Referencia: especificación "Código QR para comprobantes electrónicos" de ARCA. La URL vigente
es `https://www.arca.gob.ar/fe/qr/?p=<base64(JSON)>`.
"""

import base64
import json
from datetime import date
from decimal import Decimal

from facturitas.core.money import redondear

URL_QR = "https://www.arca.gob.ar/fe/qr/?p="


def _numero(d: Decimal, decimales: int = 2) -> int | float:
    """Entero si no tiene decimales (como en el ejemplo oficial); si no, float redondeado."""
    r = d.quantize(Decimal(1).scaleb(-decimales))
    return int(r) if r == r.to_integral_value() else float(r)


def datos_qr(
    *,
    fecha: date,
    cuit: str,
    punto_venta: int,
    cbte_tipo: int,
    numero: int,
    importe: Decimal,
    doc_tipo: int,
    doc_nro: int,
    cae: str,
    moneda: str = "PES",
    cotizacion: Decimal = Decimal(1),
) -> dict[str, object]:
    return {
        "ver": 1,
        "fecha": fecha.isoformat(),
        "cuit": int(cuit),
        "ptoVta": punto_venta,
        "tipoCmp": cbte_tipo,
        "nroCmp": numero,
        "importe": _numero(redondear(importe)),
        "moneda": moneda,
        "ctz": _numero(cotizacion, 6),
        "tipoDocRec": doc_tipo,
        "nroDocRec": doc_nro,
        "tipoCodAut": "E",
        "codAut": int(cae),
    }


def url_qr(**kwargs: object) -> str:
    datos = datos_qr(**kwargs)  # type: ignore[arg-type]
    crudo = json.dumps(datos, separators=(",", ":")).encode("utf-8")
    return URL_QR + base64.b64encode(crudo).decode("ascii")
