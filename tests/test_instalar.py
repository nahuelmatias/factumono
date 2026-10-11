import json
from pathlib import Path

import pytest

from facturitas.mcp import instalar

ENTRADA = {"command": "uv", "args": ["run", "--directory", "C:/repo", "facturitas", "mcp"]}


def test_crea_el_config_si_no_existe(tmp_path: Path) -> None:
    ruta = tmp_path / "Claude" / "claude_desktop_config.json"
    r = instalar.instalar_en(ruta, ENTRADA)
    assert r.backup is None and not r.ya_estaba
    assert json.loads(ruta.read_text(encoding="utf-8")) == {"mcpServers": {"facturitas": ENTRADA}}


def test_conserva_lo_demas_y_hace_backup(tmp_path: Path) -> None:
    ruta = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {"otro": {"command": "x"}}, "preferencia": {"a": 1}}
    ruta.write_text(json.dumps(original), encoding="utf-8")
    r = instalar.instalar_en(ruta, ENTRADA)
    assert r.backup is not None and json.loads(r.backup.read_text(encoding="utf-8")) == original
    nuevo = json.loads(ruta.read_text(encoding="utf-8"))
    assert nuevo["mcpServers"]["otro"] == {"command": "x"}
    assert nuevo["preferencia"] == {"a": 1} and nuevo["mcpServers"]["facturitas"] == ENTRADA


def test_es_idempotente(tmp_path: Path) -> None:
    ruta = tmp_path / "c.json"
    instalar.instalar_en(ruta, ENTRADA)
    r = instalar.instalar_en(ruta, ENTRADA)
    assert r.ya_estaba and r.backup is None
    assert list(tmp_path.glob("*.bak-*")) == []


def test_actualiza_una_entrada_vieja(tmp_path: Path) -> None:
    ruta = tmp_path / "c.json"
    ruta.write_text(json.dumps({"mcpServers": {"facturitas": {"command": "viejo"}}}))
    r = instalar.instalar_en(ruta, ENTRADA)
    assert r.backup is not None
    assert json.loads(ruta.read_text())["mcpServers"]["facturitas"] == ENTRADA


def test_no_toca_un_json_invalido(tmp_path: Path) -> None:
    ruta = tmp_path / "c.json"
    ruta.write_text("{esto no es json", encoding="utf-8")
    with pytest.raises(instalar.ConfigInvalida, match="No lo toqué"):
        instalar.instalar_en(ruta, ENTRADA)
    assert ruta.read_text(encoding="utf-8") == "{esto no es json"


def test_entrada_usa_uv_run_directory_desde_el_repo() -> None:
    e = instalar.entrada_mcp()
    assert e["args"][:2] == ["run", "--directory"] and e["args"][-2:] == ["facturitas", "mcp"]
    assert (Path(e["args"][2]) / "pyproject.toml").is_file()


def test_snippets_para_otros_clientes() -> None:
    s = instalar.snippets(ENTRADA)
    assert "claude mcp add facturitas -- uv run --directory C:/repo facturitas mcp" in s
    assert "gemini mcp add facturitas uv run --directory C:/repo facturitas mcp" in s
    assert '"mcpServers"' in s
