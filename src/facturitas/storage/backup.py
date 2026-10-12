"""Backup y restauración de los datos locales (sirve también como export/import entre PCs).

Entra: las bases SQLite (homologación y producción; los clientes viven en la base principal) y la
configuración. **Nunca** entra la clave privada, el `.crt` ni el caché de tickets de acceso: la
config solo guarda *rutas* a esos archivos. Todo lo que se restaura pasa por una lista blanca de
nombres, así que un zip armado a mano no puede escribir fuera del directorio de datos.
"""

import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from facturitas import __version__, config

BASES = ("historial.sqlite", "historial-prod.sqlite")
CONFIG = "config.toml"
MANIFIESTO = "manifiesto.json"
PERMITIDOS = (*BASES, CONFIG)
_PROHIBIDAS = (".key", ".crt", ".pem", ".pfx", ".p12", ".csr")


class BackupError(ValueError):
    pass


@dataclass(frozen=True)
class ResultadoBackup:
    ruta: Path
    archivos: tuple[str, ...]


def carpeta_por_defecto() -> Path:
    return config.cargar().carpeta_base_pdf() / "_backups"


def _copia_consistente(origen: Path, destino: Path) -> None:
    """Copia una base SQLite aunque esté en uso (API de backup de SQLite)."""
    src, dst = sqlite3.connect(origen), sqlite3.connect(destino)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def crear_backup(destino: Path | None = None, ahora: datetime | None = None) -> ResultadoBackup:
    ahora = ahora or datetime.now()
    nombre = f"facturitas-backup-{ahora:%Y%m%d-%H%M%S}.zip"
    if destino is None:
        destino = carpeta_por_defecto()
    ruta = destino / nombre if (destino.is_dir() or destino.suffix.lower() != ".zip") else destino
    ruta.parent.mkdir(parents=True, exist_ok=True)
    datos = config.directorio_datos()
    incluidos: list[str] = []
    with (
        tempfile.TemporaryDirectory() as tmp,
        zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as z,
    ):
        for base in BASES:
            origen = datos / base
            if origen.is_file():
                copia = Path(tmp) / base
                _copia_consistente(origen, copia)
                z.write(copia, base)
                incluidos.append(base)
        if (datos / CONFIG).is_file():
            z.write(datos / CONFIG, CONFIG)
            incluidos.append(CONFIG)
        manifiesto = {
            "facturitas": __version__,
            "fecha": ahora.isoformat(timespec="seconds"),
            "archivos": incluidos,
        }
        z.writestr(MANIFIESTO, json.dumps(manifiesto, indent=2))
    config.restringir_permisos(ruta)
    return ResultadoBackup(ruta, tuple(incluidos))


def leer_manifiesto(zip_path: Path) -> dict[str, object]:
    try:
        with zipfile.ZipFile(zip_path) as z:
            nombres = z.namelist()
            for n in nombres:
                if n.lower().endswith(_PROHIBIDAS):
                    raise BackupError(f"El zip trae un archivo de claves ({n}): no lo restauro.")
                if n != MANIFIESTO and n not in PERMITIDOS:
                    raise BackupError(f"El zip trae un archivo inesperado ({n}): no lo restauro.")
            if MANIFIESTO not in nombres:
                raise BackupError("Ese zip no es un backup de Facturitas (falta el manifiesto).")
            datos = json.loads(z.read(MANIFIESTO))
    except zipfile.BadZipFile:
        raise BackupError(f"{zip_path} no es un zip válido.") from None
    except (json.JSONDecodeError, OSError) as e:
        raise BackupError(f"No se pudo leer el backup: {e}") from None
    if not isinstance(datos, dict):
        raise BackupError("El manifiesto del backup es inválido.")
    return datos


def restaurar(zip_path: Path, *, con_config: bool = False) -> tuple[list[str], Path | None]:
    """Restaura las bases (y la config, solo si se pide). Antes guarda un backup de lo actual.

    La config tiene rutas al certificado y la clave de la PC de origen: por eso no se restaura
    salvo `con_config=True` (por ejemplo, para recuperar la misma PC). Devuelve (restaurados,
    backup_previo).
    """
    leer_manifiesto(zip_path)  # valida antes de tocar nada
    datos = config.directorio_datos()
    previo: Path | None = None
    if any((datos / n).is_file() for n in PERMITIDOS):
        previo = crear_backup(datos / "backups-previos").ruta
    restaurados: list[str] = []
    with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory() as tmp:
        for nombre in z.namelist():
            if nombre == MANIFIESTO or (nombre == CONFIG and not con_config):
                continue
            temporal = Path(tmp) / nombre
            temporal.write_bytes(z.read(nombre))
            shutil.copyfile(temporal, datos / f"{nombre}.nuevo")
            (datos / f"{nombre}.nuevo").replace(datos / nombre)
            config.restringir_permisos(datos / nombre)
            restaurados.append(nombre)
    return restaurados, previo
