"""Migración automática desde el nombre anterior del proyecto (sin pérdida de datos).

El proyecto se llamó de otra forma antes de «Factumono». La primera vez que corre esta versión se
mueven, sin perder nada, los datos que había con el nombre anterior:

- el directorio de datos (config, bases de homologación y producción, caché de tickets, clientes);
- la carpeta de certificados y claves de `~/.<nombre anterior>/` (y las rutas de la config);
- la carpeta de PDF por defecto de Documentos (solo si la persona no eligió otra).

Siempre se **mueve** (renombrar es atómico en el mismo disco): una sola copia de las claves y de
las bases. Si no se puede (archivo en uso, OneDrive), se copia o se sigue usando la carpeta vieja:
nunca se deja a la persona sin sus datos. Es idempotente, y deja un `migracion.json` para avisar
**una sola vez** (la CLI o la GUI, la primera que corra).

Los nombres anteriores se arman por partes a propósito: no queremos el nombre viejo escrito en el
código ni en los textos del producto, solo acá.
"""

import contextlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import platformdirs

_VIEJO = "factu" + "ritas"  # nombre anterior (ver el docstring)
NOMBRE_ANTERIOR = _VIEJO
_NUEVO = "factumono"
MARCA = "migracion.json"


def variable_de_entorno_vieja() -> str:
    return f"{_VIEJO.upper()}_HOME"


def datos_viejo() -> Path:
    return platformdirs.user_data_path(_VIEJO, appauthor=False)


def datos_nuevo() -> Path:
    return platformdirs.user_data_path(_NUEVO, appauthor=False)


def certificados_viejo() -> Path:
    return Path.home() / f".{_VIEJO}"


def certificados_nuevo() -> Path:
    return Path.home() / f".{_NUEVO}"


def pdf_viejo() -> Path:
    return platformdirs.user_documents_path() / _VIEJO.capitalize()


def pdf_nuevo() -> Path:
    return platformdirs.user_documents_path() / _NUEVO.capitalize()


@dataclass
class Rutas:
    datos_viejo: Path
    datos_nuevo: Path
    certs_viejo: Path
    certs_nuevo: Path
    pdf_viejo: Path
    pdf_nuevo: Path

    @classmethod
    def reales(cls) -> "Rutas":
        return cls(
            datos_viejo(), datos_nuevo(), certificados_viejo(), certificados_nuevo(),
            pdf_viejo(), pdf_nuevo(),
        )  # fmt: skip


@dataclass
class Resultado:
    movido: list[str] = field(default_factory=list)
    conservado_en_su_lugar: list[str] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)

    @property
    def hubo_cambios(self) -> bool:
        return bool(self.movido or self.conservado_en_su_lugar)


def _vacio(ruta: Path) -> bool:
    return not ruta.exists() or (ruta.is_dir() and not any(ruta.iterdir()))


def _mover(origen: Path, destino: Path, *, copiar_si_falla: bool) -> str:
    """Devuelve 'movido', 'copiado' o 'no' (no se pudo; se sigue usando el origen)."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists() and _vacio(destino):
        destino.rmdir()
    try:
        origen.rename(destino)
        return "movido"
    except OSError:
        if not copiar_si_falla:
            return "no"
    try:
        shutil.copytree(origen, destino, dirs_exist_ok=True)
        return "copiado"
    except OSError:
        return "no"


def _reescribir_rutas(archivo: Path, viejo: Path, nuevo: Path) -> None:
    """Cambia, dentro de un archivo de texto, las rutas que empiezan con `viejo` por `nuevo`.

    Las rutas de Windows se guardan con barras escapadas (TOML/JSON): se contemplan las dos formas.
    """
    if not archivo.is_file():
        return
    texto = archivo.read_text(encoding="utf-8")
    original = texto
    for a, b in (
        (str(viejo), str(nuevo)),
        (str(viejo).replace("\\", "\\\\"), str(nuevo).replace("\\", "\\\\")),
        (viejo.as_posix(), nuevo.as_posix()),
    ):
        texto = texto.replace(a, b)
    if texto != original:
        archivo.write_text(texto, encoding="utf-8")


def _carpeta_pdf_personalizada(config_toml: Path) -> str | None:
    """El valor de `carpeta_pdf` si la persona eligió una (None: se usaba la de por defecto)."""
    if not config_toml.is_file():
        return None
    for linea in config_toml.read_text(encoding="utf-8").splitlines():
        if linea.startswith("carpeta_pdf"):
            return linea.split("=", 1)[1].strip().strip('"')
        if linea.startswith("["):
            break
    return None


def migrar(rutas: Rutas | None = None) -> Resultado:
    r = rutas or Rutas.reales()
    res = Resultado()
    datos_en_uso = r.datos_nuevo

    # 1) Directorio de datos
    if r.datos_viejo.is_dir() and _vacio(r.datos_nuevo):
        estado = _mover(r.datos_viejo, r.datos_nuevo, copiar_si_falla=True)
        if estado == "no":
            datos_en_uso = r.datos_viejo
            res.conservado_en_su_lugar.append(f"datos (siguen en {r.datos_viejo})")
        else:
            res.movido.append("datos (config, bases de homologación y producción, clientes, caché)")
            if estado == "copiado":
                res.conservado_en_su_lugar.append(
                    f"copia de seguridad de los datos en {r.datos_viejo}"
                )

    config_toml = datos_en_uso / "config.toml"

    # 2) Certificados y claves (la config guarda rutas absolutas)
    if r.certs_viejo.is_dir() and not r.certs_nuevo.exists():
        if _mover(r.certs_viejo, r.certs_nuevo, copiar_si_falla=False) == "movido":
            res.movido.append("certificados y claves")
            for archivo in (config_toml, datos_en_uso / "asistente.json"):
                _reescribir_rutas(archivo, r.certs_viejo, r.certs_nuevo)
        else:
            res.conservado_en_su_lugar.append(f"certificados y claves (siguen en {r.certs_viejo})")

    # 3) PDF en la carpeta por defecto (si eligió otra, no se toca)
    if (
        r.pdf_viejo.is_dir()
        and not r.pdf_nuevo.exists()
        and not _carpeta_pdf_personalizada(config_toml)
    ):
        if _mover(r.pdf_viejo, r.pdf_nuevo, copiar_si_falla=False) == "movido":
            res.movido.append("carpeta de PDF")
        elif config_toml.is_file():
            # No se pudo mover (OneDrive, archivo abierto): se sigue usando la vieja, a propósito.
            texto = config_toml.read_text(encoding="utf-8")
            if not texto.startswith("carpeta_pdf") and "\ncarpeta_pdf" not in texto:
                linea = "carpeta_pdf = " + json.dumps(str(r.pdf_viejo), ensure_ascii=False) + "\n"
                config_toml.write_text(_insertar_en_cabecera(texto, linea), encoding="utf-8")
            res.conservado_en_su_lugar.append(f"carpeta de PDF (sigue en {r.pdf_viejo})")

    if res.hubo_cambios and datos_en_uso.is_dir():
        marca = datos_en_uso / MARCA
        marca.write_text(
            json.dumps(
                {
                    "fecha": datetime.now().isoformat(timespec="seconds"),
                    "movido": res.movido,
                    "conservado": res.conservado_en_su_lugar,
                    "avisado": False,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return res


def _insertar_en_cabecera(toml: str, linea: str) -> str:
    """Agrega una clave de nivel superior antes de la primera tabla."""
    i = toml.find("\n[")
    return toml + linea if i == -1 else toml[: i + 1] + linea + toml[i + 1 :]


_hecho = False


def migrar_si_corresponde() -> None:
    """Corre la migración una vez por proceso (no hace nada si hay un directorio de datos fijado
    por variable de entorno: tests e instalaciones portables)."""
    global _hecho
    if _hecho:
        return
    _hecho = True
    if os.environ.get("FACTUMONO_HOME") or os.environ.get(variable_de_entorno_vieja()):
        return
    with contextlib.suppress(OSError):  # nunca impedir el arranque por un problema de migración
        migrar()


# --- el aviso, una sola vez ---------------------------------------------------------------------


def aviso_pendiente(directorio_datos: Path) -> str | None:
    marca = directorio_datos / MARCA
    try:
        datos = json.loads(marca.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if datos.get("avisado"):
        return None
    cosas = ", ".join(datos.get("movido", []) + datos.get("conservado", []))
    return (
        "El programa pasó a llamarse Factumono. Pasé tus datos al nombre nuevo sin perder nada "
        f"({cosas}). Los comandos ahora empiezan con `factumono`; si usás Claude Desktop, "
        "corré `factumono mcp instalar` para actualizar la conexión."
    )


def marcar_avisado(directorio_datos: Path) -> None:
    marca = directorio_datos / MARCA
    try:
        datos = json.loads(marca.read_text(encoding="utf-8"))
        datos["avisado"] = True
        marca.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass
