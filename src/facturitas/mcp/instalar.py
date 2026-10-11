"""`facturitas mcp instalar`: agrega Facturitas al claude_desktop_config.json (con backup)."""

import glob
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

NOMBRE = "facturitas"


class ConfigInvalida(ValueError):
    pass


def _raiz_del_repo() -> Path | None:
    raiz = Path(__file__).resolve().parents[3]
    if (raiz / "pyproject.toml").is_file() and (raiz / "src" / "facturitas").is_dir():
        return raiz
    return None


def entrada_mcp() -> dict[str, Any]:
    """Cómo lanza el cliente de IA el servidor: `uv run --directory <repo> facturitas mcp` si se
    corre desde el repo, o el ejecutable `facturitas` si está instalado."""
    raiz = _raiz_del_repo()
    if raiz:
        return {
            "command": shutil.which("uv") or "uv",
            "args": ["run", "--directory", str(raiz), "facturitas", "mcp"],
        }
    return {"command": shutil.which("facturitas") or "facturitas", "args": ["mcp"]}


def rutas_claude_desktop() -> list[Path]:
    """Ubicaciones posibles del config de Claude Desktop (existentes primero)."""
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        # La versión de Microsoft Store guarda el config en una carpeta virtualizada.
        store = [
            Path(p) / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
            for p in glob.glob(str(local / "Packages" / "Claude_*"))
        ]
        candidatas = [appdata / "Claude" / "claude_desktop_config.json", *store]
    elif sys.platform == "darwin":
        candidatas = [
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        ]
    else:
        candidatas = [Path.home() / ".config" / "Claude" / "claude_desktop_config.json"]
    return sorted(candidatas, key=lambda p: not p.parent.exists())


@dataclass(frozen=True)
class Resultado:
    ruta: Path
    backup: Path | None
    ya_estaba: bool


def instalar_en(ruta: Path, entrada: dict[str, Any], *, escribir: bool = True) -> Resultado:
    """Agrega/actualiza `mcpServers.facturitas` sin tocar el resto. Backup previo si existía."""
    datos: dict[str, Any] = {}
    existia = ruta.is_file()
    if existia:
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            raise ConfigInvalida(
                f"{ruta} no es un JSON válido ({e}). No lo toqué: corregilo y reintentá."
            ) from None
        if not isinstance(datos, dict):
            raise ConfigInvalida(f"{ruta} no tiene el formato esperado. No lo toqué.")
    servidores = datos.setdefault("mcpServers", {})
    if not isinstance(servidores, dict):
        raise ConfigInvalida(f"'mcpServers' en {ruta} no tiene el formato esperado.")
    ya_estaba = servidores.get(NOMBRE) == entrada
    if ya_estaba or not escribir:
        return Resultado(ruta, None, ya_estaba)
    backup = None
    if existia:
        backup = ruta.with_name(f"{ruta.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(ruta, backup)
    servidores[NOMBRE] = entrada
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return Resultado(ruta, backup, False)


def snippets(entrada: dict[str, Any]) -> str:
    cmd = " ".join([_q(entrada["command"]), *map(_q, entrada["args"])])
    json_entrada = json.dumps({"mcpServers": {NOMBRE: entrada}}, indent=2, ensure_ascii=False)
    return (
        "Claude Code:\n"
        f"  claude mcp add {NOMBRE} -- {cmd}\n\n"
        "Gemini CLI:\n"
        f"  gemini mcp add {NOMBRE} {cmd}\n"
        "  (o agregá esto a ~/.gemini/settings.json)\n\n"
        "Otros clientes (Cursor, VS Code, ...) — bloque `mcpServers`:\n"
        + "\n".join("  " + linea for linea in json_entrada.splitlines())
    )


def _q(texto: str) -> str:
    return f'"{texto}"' if " " in texto else texto
