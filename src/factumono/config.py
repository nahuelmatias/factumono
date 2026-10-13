"""Configuración local: credenciales y perfiles en un TOML dentro del directorio de datos.

Las credenciales guardan solo la *ruta* al certificado y la clave; nunca el contenido.
"""

import contextlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import platformdirs

from factumono import migracion
from factumono.arca import ambientes
from factumono.core.models import Credencial, Perfil


class ConfigError(Exception):
    pass


def directorio_datos() -> Path:
    """Directorio de datos del usuario (`FACTUMONO_HOME` lo sobreescribe, útil en tests)."""
    base = os.environ.get("FACTUMONO_HOME") or os.environ.get(migracion.variable_de_entorno_vieja())
    if not base:
        migracion.migrar_si_corresponde()  # una vez por proceso; ver migracion.py
    ruta = Path(base) if base else platformdirs.user_data_path("factumono", appauthor=False)
    if not base and not ruta.exists() and migracion.datos_viejo().is_dir():
        return migracion.datos_viejo()  # la migración no pudo: nunca dejar a nadie sin sus datos
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def directorio_cache(ambiente: str = "homo") -> Path:
    """Caché del TA y de los parámetros, separado por ambiente.

    Homologación sigue en `cache/` (donde ya estaba: mover un TA vigente obligaría a pedir otro y
    ARCA lo rechaza); producción va en `cache/prod/`.
    """
    ruta = directorio_datos() / "cache"
    if ambientes.normalizar(ambiente) == "prod":
        ruta = ruta / "prod"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def ruta_historial(ambiente: str = "homo") -> Path:
    """Base del historial de cada ambiente (lo de homologación nunca se mezcla con lo real)."""
    nombre = "historial-prod.sqlite" if ambientes.normalizar(ambiente) == "prod" else None
    return directorio_datos() / (nombre or "historial.sqlite")


def restringir_permisos(ruta: Path) -> None:
    """Deja el archivo accesible solo para el usuario (en Windows es un no-op razonable)."""
    with contextlib.suppress(OSError):
        ruta.chmod(0o600)


@dataclass
class Config:
    credenciales: dict[str, Credencial] = field(default_factory=dict)
    perfiles: dict[str, Perfil] = field(default_factory=dict)
    perfil_default: str = "default"
    carpeta_pdf: str = ""  # vacío: Documentos/Factumono
    ambiente: str = "homo"  # ambiente activo; solo cambia con `factumono ambiente usar`

    def carpeta_base_pdf(self) -> Path:
        if self.carpeta_pdf:
            return Path(self.carpeta_pdf).expanduser()
        return platformdirs.user_documents_path() / "Factumono"

    def perfil(self, nombre: str | None = None) -> Perfil:
        clave = nombre or self.perfil_default
        try:
            return self.perfiles[clave]
        except KeyError:
            raise ConfigError(
                f"No hay un perfil '{clave}'. Configuralo con `factumono init`."
            ) from None

    def perfil_para(self, nombre: str | None, ambiente: str) -> tuple[Perfil, Credencial]:
        """El perfil y la credencial que corresponden a un ambiente.

        En producción el perfil devuelto trae el punto de venta y la credencial de producción.
        """
        amb = ambientes.obtener(ambiente)
        perfil = self.perfil(nombre)
        if not amb.es_produccion:
            return perfil, self.credencial_de(perfil)
        if not perfil.credencial_prod:
            raise ConfigError(
                "Producción todavía no está configurada para este perfil. Registrá el "
                "certificado real y el punto de venta con `factumono init --ambiente prod "
                "--cert ... --key ... --punto-venta N` (ver README, «Pasar a producción»)."
            )
        cred = self.credenciales.get(perfil.credencial_prod)
        if cred is None:
            raise ConfigError(
                f"El perfil usa la credencial de producción '{perfil.credencial_prod}', "
                "que no existe."
            )
        if cred.ambiente != "prod":
            raise ConfigError(
                f"La credencial '{perfil.credencial_prod}' no es de producción: no se usa en prod."
            )
        efectivo = replace(
            perfil, credencial=perfil.credencial_prod, punto_venta=perfil.punto_venta_prod
        )
        return efectivo, cred

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
    if isinstance(v, bool):
        return "true" if v else "false"
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
        ambiente=ambientes.normalizar(raw.get("ambiente", "homo")),
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
    cabecera += f"ambiente = {_toml_valor(cfg.ambiente)}\n"
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
