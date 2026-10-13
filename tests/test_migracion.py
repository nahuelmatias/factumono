"""Migración desde el nombre anterior: sin pérdida, idempotente, con aviso de una sola vez."""

import json
from pathlib import Path

import pytest

from factumono import config, migracion
from factumono.mcp import instalar
from factumono.migracion import Rutas, aviso_pendiente, marcar_avisado, migrar

VIEJO = migracion.NOMBRE_ANTERIOR


@pytest.fixture
def rutas(tmp_path: Path) -> Rutas:
    return Rutas(
        datos_viejo=tmp_path / "Local" / VIEJO,
        datos_nuevo=tmp_path / "Local" / "factumono",
        certs_viejo=tmp_path / "home" / f".{VIEJO}",
        certs_nuevo=tmp_path / "home" / ".factumono",
        pdf_viejo=tmp_path / "Docs" / VIEJO.capitalize(),
        pdf_nuevo=tmp_path / "Docs" / "Factumono",
    )


def _instalacion_vieja(r: Rutas, carpeta_pdf: str | None = None) -> None:
    """Deja un directorio de datos «de antes» con config, bases, clave y PDF."""
    (r.datos_viejo / "cache").mkdir(parents=True)
    (r.certs_viejo / "prod").mkdir(parents=True)
    (r.certs_viejo / "prod" / "x.key").write_text("CLAVE-PRIVADA")
    (r.certs_viejo / "prod" / "x.crt").write_text("CERT")
    (r.datos_viejo / "historial.sqlite").write_bytes(b"BASE-HOMO")
    (r.datos_viejo / "historial-prod.sqlite").write_bytes(b"BASE-PROD-IMPORTANTE")
    (r.datos_viejo / "cache" / "ta-prod.json").write_text("{}")
    key = str(r.certs_viejo / "prod" / "x.key").replace("\\", "\\\\")
    crt = str(r.certs_viejo / "prod" / "x.crt").replace("\\", "\\\\")
    cabecera = f'perfil_default = "default"\ncarpeta_pdf = "{carpeta_pdf}"\n' if carpeta_pdf else ""
    (r.datos_viejo / "config.toml").write_text(
        f'{cabecera}ambiente = "prod"\n[credenciales.default-prod]\n'
        f'key = "{key}"\ncert = "{crt}"\n',
        encoding="utf-8",
    )
    (r.pdf_viejo / "default").mkdir(parents=True)
    (r.pdf_viejo / "default" / "factura.pdf").write_bytes(b"%PDF")


def test_migra_todo_sin_perder_nada(rutas: Rutas) -> None:
    _instalacion_vieja(rutas)
    res = migrar(rutas)
    assert not res.errores and len(res.movido) == 3
    # los datos están en el lugar nuevo, byte a byte
    assert (rutas.datos_nuevo / "historial-prod.sqlite").read_bytes() == b"BASE-PROD-IMPORTANTE"
    assert (rutas.datos_nuevo / "historial.sqlite").read_bytes() == b"BASE-HOMO"
    assert (rutas.datos_nuevo / "cache" / "ta-prod.json").is_file()
    assert (rutas.certs_nuevo / "prod" / "x.key").read_text() == "CLAVE-PRIVADA"
    assert (rutas.pdf_nuevo / "default" / "factura.pdf").read_bytes() == b"%PDF"
    # y no quedó una segunda copia de la clave ni de las bases
    assert not rutas.datos_viejo.exists() and not rutas.certs_viejo.exists()
    assert not rutas.pdf_viejo.exists()


def test_las_rutas_de_la_config_siguen_apuntando_a_la_clave(rutas: Rutas) -> None:
    _instalacion_vieja(rutas)
    migrar(rutas)
    texto = (rutas.datos_nuevo / "config.toml").read_text(encoding="utf-8")
    assert VIEJO not in texto
    assert str(rutas.certs_nuevo).replace("\\", "\\\\") in texto


def test_es_idempotente_y_no_pisa_lo_nuevo(rutas: Rutas) -> None:
    _instalacion_vieja(rutas)
    migrar(rutas)
    (rutas.datos_nuevo / "historial-prod.sqlite").write_bytes(b"CAMBIO-POSTERIOR")
    segunda = migrar(rutas)
    assert not segunda.hubo_cambios
    assert (rutas.datos_nuevo / "historial-prod.sqlite").read_bytes() == b"CAMBIO-POSTERIOR"


def test_si_ya_hay_datos_nuevos_no_se_mezcla_ni_se_toca_lo_viejo(rutas: Rutas) -> None:
    _instalacion_vieja(rutas)
    rutas.datos_nuevo.mkdir(parents=True)
    (rutas.datos_nuevo / "config.toml").write_text('ambiente = "homo"\n')
    migrar(rutas)
    assert (rutas.datos_viejo / "historial-prod.sqlite").is_file()  # lo viejo queda intacto
    assert (rutas.datos_nuevo / "config.toml").read_text() == 'ambiente = "homo"\n'


def test_respeta_una_carpeta_de_pdf_elegida_por_la_persona(rutas: Rutas, tmp_path: Path) -> None:
    propia = tmp_path / "MisFacturas"
    propia.mkdir()
    _instalacion_vieja(rutas, carpeta_pdf=str(propia).replace("\\", "\\\\"))
    res = migrar(rutas)
    assert rutas.pdf_viejo.is_dir() and not rutas.pdf_nuevo.exists()  # no se tocó
    assert not any("PDF" in m for m in res.movido)


def test_si_no_se_puede_mover_se_conserva_y_se_sigue_usando(
    rutas: Rutas, monkeypatch: pytest.MonkeyPatch
) -> None:
    _instalacion_vieja(rutas)

    def falla(self: Path, destino: Path) -> Path:  # simula un archivo en uso
        raise PermissionError("en uso")

    monkeypatch.setattr(Path, "rename", falla)
    res = migrar(rutas)
    monkeypatch.undo()
    # los datos se copiaron (el original queda como respaldo) y nada se perdió
    assert (rutas.datos_nuevo / "historial-prod.sqlite").read_bytes() == b"BASE-PROD-IMPORTANTE"
    assert (rutas.datos_viejo / "historial-prod.sqlite").is_file()
    assert (rutas.certs_viejo / "prod" / "x.key").is_file()  # la clave sigue donde estaba
    assert any("siguen en" in c for c in res.conservado_en_su_lugar)
    # la carpeta de PDF que no se pudo mover queda fijada en la config
    texto = (rutas.datos_nuevo / "config.toml").read_text(encoding="utf-8")
    assert str(rutas.pdf_viejo).replace("\\", "\\\\") in texto


def test_avisa_una_sola_vez(rutas: Rutas) -> None:
    _instalacion_vieja(rutas)
    migrar(rutas)
    aviso = aviso_pendiente(rutas.datos_nuevo)
    assert aviso and "Factumono" in aviso and "mcp instalar" in aviso
    marcar_avisado(rutas.datos_nuevo)
    assert aviso_pendiente(rutas.datos_nuevo) is None
    assert json.loads((rutas.datos_nuevo / "migracion.json").read_text())["avisado"] is True


def test_sin_instalacion_vieja_no_hace_nada(rutas: Rutas) -> None:
    res = migrar(rutas)
    assert not res.hubo_cambios and not rutas.datos_nuevo.exists()
    assert aviso_pendiente(rutas.datos_nuevo) is None


def test_con_directorio_fijado_por_entorno_no_se_migra(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[int] = []
    monkeypatch.setattr(migracion, "migrar", lambda *a, **k: llamadas.append(1))
    monkeypatch.setattr(migracion, "_hecho", False)
    migracion.migrar_si_corresponde()  # FACTUMONO_HOME está puesto por los tests
    assert not llamadas
    assert config.directorio_datos().is_dir()


def test_el_entorno_viejo_sigue_funcionando_como_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACTUMONO_HOME")
    monkeypatch.setenv(migracion.variable_de_entorno_vieja(), str(tmp_path / "alias"))
    assert config.directorio_datos() == tmp_path / "alias"


# --- mcp instalar reemplaza la entrada con el nombre anterior --------------------------------


def test_mcp_instalar_reemplaza_la_entrada_vieja(tmp_path: Path) -> None:
    ruta = tmp_path / "claude_desktop_config.json"
    previo = {"mcpServers": {VIEJO: {"command": "uv", "args": []}, "otro": {"command": "x"}}}
    ruta.write_text(json.dumps(previo), encoding="utf-8")
    entrada = {"command": "C:/x/factumono.exe", "args": ["mcp"]}
    r = instalar.instalar_en(ruta, entrada)
    datos = json.loads(ruta.read_text(encoding="utf-8"))["mcpServers"]
    assert r.reemplazo_viejo and not r.ya_estaba and r.backup is not None
    assert VIEJO not in datos and datos["factumono"] == entrada and "otro" in datos
    # una segunda vez ya está todo en orden
    assert instalar.instalar_en(ruta, entrada).ya_estaba
