"""Endpoints de ARCA por ambiente.

Homologación es el ambiente por defecto. Producción existe, pero solo se usa si el usuario la
activa a mano (`factumono ambiente usar prod`, ver CLAUDE.md sección 5) y los tests tienen un
guard que hace fallar cualquier pedido a estos hosts (ver `tests/conftest.py`).
"""

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Ambiente:
    nombre: str
    wsaa_url: str
    wsfe_url: str
    es_produccion: bool = False

    @property
    def etiqueta(self) -> str:
        if self.es_produccion:
            return "PRODUCCIÓN (validez fiscal)"
        return "HOMOLOGACIÓN (pruebas, sin validez fiscal)"

    @property
    def titulo(self) -> str:
        return "PRODUCCIÓN" if self.es_produccion else "HOMOLOGACIÓN"

    @property
    def banner(self) -> str:
        """Primera línea de todo preview, respuesta del MCP y salida de la CLI."""
        if self.es_produccion:
            return "⚠️ PRODUCCIÓN — comprobante con validez fiscal"
        return "🧪 HOMOLOGACIÓN (pruebas, sin validez fiscal)"

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(urlparse(u).hostname or "" for u in (self.wsaa_url, self.wsfe_url))


HOMO = Ambiente(
    nombre="homo",
    wsaa_url="https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    wsfe_url="https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
)

PROD = Ambiente(
    nombre="prod",
    wsaa_url="https://wsaa.afip.gov.ar/ws/services/LoginCms",
    wsfe_url="https://servicios1.afip.gov.ar/wsfev1/service.asmx",
    es_produccion=True,
)

_AMBIENTES = {"homo": HOMO, "prod": PROD}
_ALIAS = {
    "homologacion": "homo",
    "homologación": "homo",
    "pruebas": "homo",
    "produccion": "prod",
    "producción": "prod",
}


def normalizar(nombre: str) -> str:
    n = nombre.strip().lower()
    return _ALIAS.get(n, n)


def obtener(nombre: str) -> Ambiente:
    try:
        return _AMBIENTES[normalizar(nombre)]
    except KeyError:
        raise ValueError(f"Ambiente '{nombre}' desconocido. Usá 'homo' o 'prod'.") from None
