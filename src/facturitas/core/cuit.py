"""Validación y formato de CUIT/CUIL."""

import re

_PESOS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def digito_verificador(base: str) -> int | None:
    """Calcula el dígito verificador de los primeros 10 dígitos. None si no existe uno válido."""
    suma = sum(int(d) * p for d, p in zip(base, _PESOS, strict=True))
    dv = 11 - (suma % 11)
    if dv == 11:
        return 0
    if dv == 10:
        return None
    return dv


def normalizar(texto: str) -> str:
    """Devuelve el CUIT solo con dígitos. Lanza ValueError si no es un CUIT válido."""
    limpio = re.sub(r"[\s-]", "", texto)
    if not re.fullmatch(r"\d{11}", limpio):
        raise ValueError(f"El CUIT debe tener 11 dígitos: {texto!r}")
    if digito_verificador(limpio[:10]) != int(limpio[10]):
        raise ValueError(f"El CUIT {texto!r} tiene un dígito verificador incorrecto")
    return limpio


def es_valido(texto: str) -> bool:
    try:
        normalizar(texto)
    except ValueError:
        return False
    return True


def formatear(cuit: str) -> str:
    """20358047867 -> 20-35804786-7."""
    return f"{cuit[:2]}-{cuit[2:10]}-{cuit[10]}"
