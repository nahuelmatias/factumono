"""Contexto de trabajo de un perfil: credencial, clientes ARCA, historial y almacenes locales."""

from dataclasses import dataclass, field

from factumono import config
from factumono.arca import ambientes
from factumono.arca.wsaa import Wsaa
from factumono.arca.wsfe import Wsfe
from factumono.core.models import Comprobante, Credencial, Perfil
from factumono.storage.borradores import Borradores
from factumono.storage.clientes import Clientes
from factumono.storage.historial import Historial, Registro


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
        """Primera línea de todo preview y respuesta: el ambiente, bien visible."""
        return ambientes.obtener(self.ambiente).banner

    @property
    def nombre_ambiente(self) -> str:
        return ambientes.obtener(self.ambiente).titulo

    def posibles_duplicados(self, c: Comprobante) -> list[Registro]:
        """Comprobantes parecidos ya emitidos (solo informativo); vacío si el perfil lo apagó."""
        if not self.perfil.aviso_duplicados or self.historial is None:
            return []
        return self.historial.posibles_duplicados(self.perfil.cuit, self.ambiente, c)


def construir(nombre_perfil: str | None, ambiente: str | None = None) -> Contexto:
    """Contexto de un perfil en el ambiente activo (o en `ambiente`, para chequear prod antes
    de activarla)."""
    cfg = config.cargar()
    amb = ambientes.obtener(ambiente or cfg.ambiente)
    perfil, cred = cfg.perfil_para(nombre_perfil, amb.nombre)
    cache = config.directorio_cache(amb.nombre)
    wsaa = Wsaa(cred, amb, cache)
    return Contexto(
        perfil,
        cred,
        wsaa,
        Wsfe(perfil.cuit, wsaa, amb, cache),
        amb.etiqueta,
        perfil_nombre=nombre_perfil or cfg.perfil_default,
        ambiente=amb.nombre,
        historial=Historial(config.ruta_historial(amb.nombre)),
    )
