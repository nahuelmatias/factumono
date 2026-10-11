"""Configuración local: credenciales y perfiles en un TOML dentro del directorio de datos.

Las credenciales guardan solo la *ruta* al certificado y la clave; nunca el contenido.
"""

import contextlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import platformdirs

from facturitas.core.models import Credencial, Perfil


class ConfigError(Exception):
    pass


def directorio_datos() -> Path:
    """Directorio de datos del usuario (`FACTURITAS_HOME` lo sobreescribe, útil en tests)."""
    base = os.environ.get("FACTURITAS_HOME")
    ruta = Path(base) if base else platformdirs.user_data_path("facturitas", appauthor=False)
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def directorio_cache() -> Path:
    ruta = directorio_datos() / "cache"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def restringir_permisos(ruta: Path) -> None:
    """Deja el archivo accesible solo para el usuario (en Windows es un no-op razonable)."""
    with contextlib.suppress(OSError):
        ruta.chmod(0o600)


@dataclass
class Config:
    credenciales: dict[str, Credencial] = field(default_factory=dict)
    perfiles: dict[str, Perfil] = field(default_factory=dict)
    perfil_default: str = "default"
    carpeta_pdf: str = ""  # vacío: Documentos/Facturitas

    def carpeta_base_pdf(self) -> Path:
        if self.carpeta_pdf:
            return Path(self.carpeta_pdf).expanduser()
        return platformdirs.user_documents_path() / "Facturitas"

    def perfil(self, nombre: str | None = None) -> Perfil:
        clave = nombre or self.perfil_default
        try:
            return self.perfiles[clave]
        except KeyError:
            raise ConfigError(
                f"No hay un perfil '{clave}'. Configuralo con `facturitas init`."
            ) from None

    def credencial_de(self, perfil: Perfil) -> Credencial:
        try:
            return self.credenciales[perfil.credencial]
        except KeyError:
            raise ConfigError(
                f"El perfil usa la credencial '{perfil.credencial}', que no existe."
            ) from None


def ruta_config() -> Path:
    return directorio_datos() / "config.toml"


def _toml_valor(v: object) -> str:
    if isinstance(v, int):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _tabla(nombre: str, datos: dict[str, Any]) -> str:
    lineas = [f"[{nombre}]"]
    lineas += [f"{k} = {_toml_valor(v)}" for k, v in datos.items() if v not in (None, "")]
    return "\n".join(lineas) + "\n"


def cargar() -> Config:
    ruta = ruta_config()
    if not ruta.exists():
        return Config()
    with ruta.open("rb") as f:
        raw = tomllib.load(f)
    cfg = Config(
        perfil_default=raw.get("perfil_default", "default"),
        carpeta_pdf=raw.get("carpeta_pdf", ""),
    )
    for nombre, d in raw.get("credenciales", {}).items():
        cfg.credenciales[nombre] = Credencial(**d)
    for nombre, d in raw.get("perfiles", {}).items():
        datos = dict(d)
        if "inicio_actividades" in datos:
            datos["inicio_actividades"] = date.fromisoformat(datos["inicio_actividades"])
        cfg.perfiles[nombre] = Perfil(**datos)
    return cfg


def guardar(cfg: Config) -> None:
    cabecera = f"perfil_default = {_toml_valor(cfg.perfil_default)}\n"
    if cfg.carpeta_pdf:
        cabecera += f"carpeta_pdf = {_toml_valor(cfg.carpeta_pdf)}\n"
    partes = [cabecera]
    for nombre, c in cfg.credenciales.items():
        partes.append(_tabla(f"credenciales.{nombre}", asdict(c)))
    for nombre, p in cfg.perfiles.items():
        d = asdict(p)
        if p.inicio_actividades:
            d["inicio_actividades"] = p.inicio_actividades.isoformat()
        partes.append(_tabla(f"perfiles.{nombre}", d))
    ruta = ruta_config()
    ruta.write_text("\n".join(partes), encoding="utf-8")
    restringir_permisos(ruta)
