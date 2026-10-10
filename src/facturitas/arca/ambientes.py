"""Endpoints de ARCA por ambiente.

En M1 solo existe homologación. Producción se agrega recién cuando el usuario lo habilite a
mano (ver CLAUDE.md, sección 5): no hay ninguna URL de producción en el código a propósito.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Ambiente:
    nombre: str
    wsaa_url: str
    wsfe_url: str
    es_produccion: bool = False

    @property
    def etiqueta(self) -> str:
        return "HOMOLOGACIÓN (pruebas, sin validez fiscal)"


HOMO = Ambiente(
    nombre="homo",
    wsaa_url="https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    wsfe_url="https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
)

_AMBIENTES = {"homo": HOMO}


def obtener(nombre: str) -> Ambiente:
    try:
        return _AMBIENTES[nombre]
    except KeyError:
        raise ValueError(
            f"Ambiente '{nombre}' no disponible. Por ahora solo existe 'homo' (homologación)."
        ) from None
