"""Fechas: ARCA las manda como AAAAMMDD; en pantalla siempre se muestran como DD/MM/AAAA."""

from datetime import date, datetime


def de_arca(texto: str) -> date:
    """'20261020' -> date(2026, 10, 20)."""
    return datetime.strptime(texto.strip(), "%Y%m%d").date()


def a_arca(d: date) -> str:
    return d.strftime("%Y%m%d")


def mostrar(d: date | str | None) -> str:
    """DD/MM/AAAA. Acepta un `date` o un texto AAAAMMDD / AAAA-MM-DD (devuelve '' si no hay)."""
    if d is None or d == "":
        return ""
    if isinstance(d, str):
        d = date.fromisoformat(d) if "-" in d else de_arca(d)
    return d.strftime("%d/%m/%Y")
