"""Importes con Decimal: parseo, redondeo y formato."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CENTAVOS = Decimal("0.01")


def redondear(valor: Decimal) -> Decimal:
    """Redondea a 2 decimales (mitad hacia arriba)."""
    return valor.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


def parsear(texto: str) -> Decimal:
    """Interpreta un importe escrito como `1500`, `1500.50`, `1500,50` o `1.234,56`.

    Con coma, la coma es el decimal y los puntos son miles. Sin coma, un solo punto es decimal
    (`1500.50`) y varios puntos son separadores de miles (`1.234.567`).
    """
    t = texto.strip().replace("$", "").replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        valor = Decimal(t)
    except InvalidOperation:
        raise ValueError(f"Importe inválido: {texto!r}") from None
    if not valor.is_finite():
        raise ValueError(f"Importe inválido: {texto!r}")
    return valor


def formatear_ar(valor: Decimal) -> str:
    """1234567.5 -> 1.234.567,50 (formato es-AR)."""
    entero, _, dec = f"{redondear(valor):,.2f}".partition(".")
    return f"{entero.replace(',', '.')},{dec}"
