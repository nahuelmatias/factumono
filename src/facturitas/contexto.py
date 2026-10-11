"""Contexto de trabajo de un perfil: credencial, clientes ARCA, historial y almacenes locales."""

from dataclasses import dataclass, field

from facturitas import config
from facturitas.arca import ambientes
from facturitas.arca.wsaa import Wsaa
from facturitas.arca.wsfe import Wsfe
from facturitas.core.models import Credencial, Perfil
from facturitas.storage.borradores import Borradores
from facturitas.storage.clientes import Clientes
from facturitas.storage.historial import Historial


@dataclass
class Contexto:
    perfil: Perfil
    credencial: Credencial
    wsaa: Wsaa
    wsfe: Wsfe
    etiqueta: str
    perfil_nombre: str = "default"
    ambiente: str = "homo"
    historial: Historial | None = None
    clientes: Clientes = field(default_factory=Clientes)
    borradores: Borradores = field(default_factory=Borradores)

    @property
    def es_produccion(self) -> bool:
        return self.ambiente == "prod"

    @property
    def aviso_ambiente(self) -> str:
        """Primera línea de toda respuesta del MCP: el ambiente, bien visible."""
        if self.es_produccion:
            return "⚠️ PRODUCCIÓN (comprobantes con validez fiscal)"
        return "🧪 HOMOLOGACIÓN (pruebas, sin validez fiscal)"


def construir(nombre_perfil: str | None) -> Contexto:
    cfg = config.cargar()
    perfil = cfg.perfil(nombre_perfil)
    cred = cfg.credencial_de(perfil)
    amb = ambientes.obtener(cred.ambiente)  # por ahora solo existe homologación
    cache = config.directorio_cache()
    wsaa = Wsaa(cred, amb, cache)
    return Contexto(
        perfil,
        cred,
        wsaa,
        Wsfe(perfil.cuit, wsaa, amb, cache),
        amb.etiqueta,
        perfil_nombre=nombre_perfil or cfg.perfil_default,
        ambiente=amb.nombre,
        historial=Historial(),
    )
