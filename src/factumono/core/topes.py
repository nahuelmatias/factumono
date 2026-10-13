"""Topes de facturación del monotributo (por categoría) y cuánto lleva facturado un perfil.

Los topes NO están hardcodeados: viven en un archivo de datos (`data/topes_monotributo.json`) con
la fecha de vigencia y la fuente. Si hay un `topes_monotributo.json` en el directorio de datos del
usuario, ese gana: así se actualizan sin esperar una versión nueva (ARCA los ajusta en febrero y
agosto).
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib import resources
from pathlib import Path

from factumono import config

NOMBRE_ARCHIVO = "topes_monotributo.json"
MESES_DE_VIGENCIA = 6  # pasado este tiempo se avisa que conviene revisar los topes


class TopesError(ValueError):
    pass


@dataclass(frozen=True)
class Topes:
    vigencia_desde: date
    fuente: str
    verificado: date | None
    categorias: dict[str, Decimal]
    origen: str  # «incluido» o la ruta del archivo del usuario

    def tope(self, categoria: str) -> Decimal:
        c = categoria.strip().upper()
        try:
            return self.categorias[c]
        except KeyError:
            raise TopesError(
                f"No conozco la categoría «{categoria}». Las categorías son "
                f"{', '.join(self.categorias)}."
            ) from None

    def desactualizados(self, hoy: date) -> bool:
        """¿Pasaron más de seis meses desde la vigencia? (ARCA actualiza dos veces por año)."""
        m = self.vigencia_desde.month - 1 + MESES_DE_VIGENCIA
        limite = date(self.vigencia_desde.year + m // 12, m % 12 + 1, 1)
        return hoy >= limite


def _interpretar(datos: dict[str, object], origen: str) -> Topes:
    try:
        categorias = {str(k).upper(): Decimal(str(v)) for k, v in dict(datos["categorias"]).items()}  # type: ignore[call-overload]
        verificado = datos.get("verificado")
        return Topes(
            vigencia_desde=date.fromisoformat(str(datos["vigencia_desde"])),
            fuente=str(datos.get("fuente", "")),
            verificado=date.fromisoformat(str(verificado)) if verificado else None,
            categorias=categorias,
            origen=origen,
        )
    except (KeyError, ValueError, TypeError, ArithmeticError) as e:
        raise TopesError(f"El archivo de topes ({origen}) no es válido: {e}") from None


def cargar(ruta_usuario: Path | None = None) -> Topes:
    ruta = ruta_usuario or config.directorio_datos() / NOMBRE_ARCHIVO
    if ruta.is_file():
        try:
            return _interpretar(json.loads(ruta.read_text(encoding="utf-8")), str(ruta))
        except json.JSONDecodeError as e:
            raise TopesError(f"El archivo de topes ({ruta}) no es un JSON válido: {e}") from None
    texto = resources.files("factumono.data").joinpath(NOMBRE_ARCHIVO).read_text("utf-8")
    return _interpretar(json.loads(texto), "incluido")


@dataclass(frozen=True)
class Avance:
    """Cuánto lleva facturado un monotributista contra el tope de su categoría."""

    categoria: str
    tope: Decimal
    facturado: Decimal
    desde: date
    hasta: date

    @property
    def porcentaje(self) -> Decimal:
        return (self.facturado / self.tope * 100) if self.tope else Decimal(0)

    @property
    def disponible(self) -> Decimal:
        return self.tope - self.facturado

    @property
    def nivel(self) -> str:
        """`ok` (hasta 70 %), `atencion` (70-90 %), `peligro` (más de 90 %), `excedido`."""
        p = self.porcentaje
        if p > 100:
            return "excedido"
        if p > 90:
            return "peligro"
        if p > 70:
            return "atencion"
        return "ok"


def avance(topes: Topes, categoria: str, facturado: Decimal, desde: date, hasta: date) -> Avance:
    return Avance(categoria.strip().upper(), topes.tope(categoria), facturado, desde, hasta)
