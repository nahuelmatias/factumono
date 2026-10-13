"""Historial multi-PC (M3.5): brechas contra ARCA, recuperar automático, backup y restaurar."""

import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from factumono import config
from factumono.cli import app as cli
from factumono.contexto import Contexto
from factumono.core import service
from factumono.core.models import Cliente, Comprobante, Credencial, Perfil
from factumono.core.sincronia import brechas, recuperar_brechas
from factumono.mcp.herramientas import Herramientas
from factumono.storage import backup
from factumono.storage.clientes import Clientes
from factumono.storage.historial import Historial
from tests import test_cli
from tests.test_cli import WsaaFalso, WsfeFalso
from tests.test_service import ArcaFalso

runner = CliRunner()
wsfe_falso = test_cli.wsfe_falso  # reutiliza la fixture del CLI


def _ctx(arca: WsfeFalso, perfil: Perfil, credencial: Credencial) -> Contexto:
    return Contexto(
        perfil, credencial, WsaaFalso(), arca,  # type: ignore[arg-type]
        "HOMOLOGACIÓN (pruebas)", perfil_nombre="default", ambiente="homo", historial=Historial(),
    )  # fmt: skip


def _fuera_de_esta_pc(arca: ArcaFalso, c: Comprobante, *numeros: int) -> None:
    """Simula comprobantes que otra PC emitió con el mismo punto de venta."""
    for n in numeros:
        arca.autorizados[n] = c


# --- brechas ---------------------------------------------------------------------------------


def test_sin_brecha_si_el_historial_tiene_todo(
    perfil: Perfil, credencial: Credencial, comprobante: Comprobante
) -> None:
    arca = WsfeFalso()
    ctx = _ctx(arca, perfil, credencial)
    assert ctx.historial
    service.emitir(ctx.historial, arca, perfil="default", ambiente="homo", c=comprobante, clave="a")
    assert brechas(ctx) == []


def test_detecta_lo_emitido_desde_otra_pc_incluso_en_el_medio(
    perfil: Perfil, credencial: Credencial, comprobante: Comprobante
) -> None:
    arca = WsfeFalso()
    ctx = _ctx(arca, perfil, credencial)
    assert ctx.historial
    service.emitir(ctx.historial, arca, perfil="default", ambiente="homo", c=comprobante, clave="a")
    _fuera_de_esta_pc(arca, comprobante, 2, 3)  # la otra PC emitió el 2 y el 3
    (b,) = brechas(ctx)
    assert (b.punto_venta, b.cbte_tipo, b.ultimo_arca, b.faltantes) == (2, 11, 3, (2, 3))
    # esta PC emite el 4: el hueco del medio sigue ahí (no alcanza con mirar el último número)
    service.emitir(ctx.historial, arca, perfil="default", ambiente="homo", c=comprobante, clave="b")
    assert brechas(ctx)[0].faltantes == (2, 3)


def test_recuperar_completa_el_historial(
    perfil: Perfil, credencial: Credencial, comprobante: Comprobante
) -> None:
    arca = WsfeFalso()
    _fuera_de_esta_pc(arca, comprobante, 1, 2)
    ctx = _ctx(arca, perfil, credencial)
    recuperados, faltan = recuperar_brechas(ctx, brechas(ctx))
    assert [r.numero for r in recuperados] == [1, 2] and faltan == []
    assert all(r.cae and r.cae_vto for r in recuperados)
    assert brechas(ctx) == []


# --- CLI -------------------------------------------------------------------------------------

ARGS = ["emitir", "--descripcion", "Servicios de prueba", "--precio", "1500"]


def test_emitir_avisa_y_ofrece_recuperar(
    wsfe_falso: WsfeFalso,
    comprobante: Comprobante,
) -> None:
    _fuera_de_esta_pc(wsfe_falso, comprobante, 1, 2)
    r = runner.invoke(cli.app, ARGS, input="s\ns\n")  # traer lo que falta, y emitir
    assert r.exit_code == 0, r.output
    assert "Hay 2 comprobante(s) emitidos fuera de esta PC" in r.output
    assert "recuperado" in r.output and "Nro. 00000003" in r.output
    assert [x.numero for x in cli._historial(cli._contexto(None)).listar()] == [3, 2, 1]


def test_emitir_se_puede_negar_a_recuperar_y_sigue(
    wsfe_falso: WsfeFalso,
    comprobante: Comprobante,
) -> None:
    _fuera_de_esta_pc(wsfe_falso, comprobante, 1)
    r = runner.invoke(cli.app, ARGS, input="n\ns\n")
    assert r.exit_code == 0 and "recuperado" not in r.output
    assert len(cli._historial(cli._contexto(None)).listar()) == 1


def test_emitir_con_yes_solo_avisa(wsfe_falso: WsfeFalso, comprobante: Comprobante) -> None:
    _fuera_de_esta_pc(wsfe_falso, comprobante, 1)
    r = runner.invoke(cli.app, [*ARGS, "--yes"])
    assert r.exit_code == 0 and "factumono recuperar" in r.output
    assert len(cli._historial(cli._contexto(None)).listar()) == 1  # no recuperó nada solo


def test_recuperar_sin_numeros_completa_todo(
    wsfe_falso: WsfeFalso,
    comprobante: Comprobante,
) -> None:
    _fuera_de_esta_pc(wsfe_falso, comprobante, 1, 2, 3)
    r = runner.invoke(cli.app, ["recuperar"])
    assert r.exit_code == 0 and r.output.count("recuperado") == 3
    assert "completo" in runner.invoke(cli.app, ["recuperar"]).output


def test_estado_avisa_de_la_brecha(wsfe_falso: WsfeFalso, comprobante: Comprobante) -> None:
    _fuera_de_esta_pc(wsfe_falso, comprobante, 1)
    assert "emitidos fuera de esta PC" in runner.invoke(cli.app, ["estado"]).output


# --- MCP -------------------------------------------------------------------------------------


def test_mcp_avisa_en_el_preview_y_recupera_sin_emitir(
    perfil: Perfil, credencial: Credencial, comprobante: Comprobante, tmp_path: Path
) -> None:
    config.guardar(config.Config({"default": credencial}, {"default": perfil}))
    arca = WsfeFalso()
    _fuera_de_esta_pc(arca, comprobante, 1, 2)
    ctx = _ctx(arca, perfil, credencial)
    ctx.clientes.guardar(
        "default",
        Cliente("seba", "SEBA", 96, 12345678, "Consumidor Final", descripcion="Servicios"),
    )
    h = Herramientas(lambda nombre: ctx, lambda: datetime(2026, 10, 10, 15, 0))
    prep = h.preparar_comprobante(cliente="seba", importe="100", periodo="septiembre")
    assert "2 comprobante(s) emitidos fuera de esta PC" in prep
    assert "recuperar_comprobantes" in prep
    n_antes = len(arca.autorizados)
    r = h.recuperar_comprobantes()
    assert "Recuperé 2" in r and r.startswith("🧪")
    assert len(arca.autorizados) == n_antes  # no emitió nada
    assert "completo" in h.recuperar_comprobantes()


# --- backup / restaurar ----------------------------------------------------------------------


@pytest.fixture
def datos(comprobante: Comprobante, perfil: Perfil, credencial: Credencial) -> Path:
    """Un directorio de datos con historial homo y prod, clientes, config, TA y claves."""
    config.guardar(config.Config({"default": credencial}, {"default": perfil}))
    for amb, ruta in (
        ("homo", config.ruta_historial("homo")),
        ("prod", config.ruta_historial("prod")),
    ):
        h = Historial(ruta)
        service.emitir(h, ArcaFalso(), perfil="default", ambiente=amb, c=comprobante, clave=amb)
    Clientes().guardar("default", Cliente("acciona", "ACCIONA", 80, 30710581467, "RI"))
    d = config.directorio_datos()
    (config.directorio_cache() / "ta-homo-wsfe-x.json").write_text('{"token": "SECRETO"}')
    (d / "factumono-prod.key").write_text("-----BEGIN PRIVATE KEY-----")
    (d / "factumono-prod.crt").write_text("-----BEGIN CERTIFICATE-----")
    return d


AHORA = datetime(2026, 10, 11, 15, 30, 5)


def test_backup_incluye_bases_clientes_y_config_con_fecha_en_el_nombre(
    datos: Path, tmp_path: Path
) -> None:
    r = backup.crear_backup(tmp_path / "bk", AHORA)
    assert r.ruta.name == "factumono-backup-20261011-153005.zip"
    assert set(r.archivos) == {"historial.sqlite", "historial-prod.sqlite", "config.toml"}
    with zipfile.ZipFile(r.ruta) as z:
        assert set(z.namelist()) == {*r.archivos, "manifiesto.json"}
        assert json.loads(z.read("manifiesto.json"))["fecha"] == "2026-10-11T15:30:05"


def test_backup_nunca_incluye_claves_certificados_ni_tickets(datos: Path, tmp_path: Path) -> None:
    r = backup.crear_backup(tmp_path / "bk", AHORA)
    with zipfile.ZipFile(r.ruta) as z:
        nombres = " ".join(z.namelist()).lower()
        contenido = b"".join(z.read(n) for n in z.namelist())
    for prohibido in (".key", ".crt", ".pem", "ta-homo"):
        assert prohibido not in nombres
    assert b"PRIVATE KEY" not in contenido and b"SECRETO" not in contenido


def test_backup_a_un_archivo_zip_concreto(datos: Path, tmp_path: Path) -> None:
    destino = tmp_path / "mio.zip"
    assert backup.crear_backup(destino, AHORA).ruta == destino and destino.is_file()


def test_restaurar_recupera_historial_y_clientes(datos: Path, tmp_path: Path) -> None:
    bk = backup.crear_backup(tmp_path / "bk", AHORA).ruta
    for base in backup.BASES:
        (datos / base).unlink()
    restaurados, previo = backup.restaurar(bk)
    assert set(restaurados) == set(backup.BASES)  # la config no se toca por defecto
    assert previo is not None and previo.is_file()  # copia de seguridad de lo que había
    assert len(Historial(config.ruta_historial("prod")).listar()) == 1
    assert len(Historial().listar()) == 1
    assert Clientes().buscar("default", "acciona") is not None


def test_restaurar_con_config_y_sin_config(datos: Path, tmp_path: Path) -> None:
    bk = backup.crear_backup(tmp_path / "bk", AHORA).ruta
    (datos / "config.toml").write_text('perfil_default = "otro"\n')
    backup.restaurar(bk)
    assert "otro" in (datos / "config.toml").read_text()
    assert "config.toml" in backup.restaurar(bk, con_config=True)[0]
    assert "otro" not in (datos / "config.toml").read_text()


def test_restaurar_rechaza_zips_raros(tmp_path: Path) -> None:
    malo = tmp_path / "malo.zip"
    with zipfile.ZipFile(malo, "w") as z:
        z.writestr("manifiesto.json", "{}")
        z.writestr("../afuera.txt", "x")
    with pytest.raises(backup.BackupError, match="inesperado"):
        backup.restaurar(malo)
    con_clave = tmp_path / "clave.zip"
    with zipfile.ZipFile(con_clave, "w") as z:
        z.writestr("manifiesto.json", "{}")
        z.writestr("factumono.key", "x")
    with pytest.raises(backup.BackupError, match="claves"):
        backup.restaurar(con_clave)
    sin_manifiesto = tmp_path / "sm.zip"
    with zipfile.ZipFile(sin_manifiesto, "w") as z:
        z.writestr("historial.sqlite", "x")
    with pytest.raises(backup.BackupError, match="manifiesto"):
        backup.restaurar(sin_manifiesto)
    (tmp_path / "no.zip").write_text("no soy un zip")
    with pytest.raises(backup.BackupError, match="zip válido"):
        backup.restaurar(tmp_path / "no.zip")


def test_la_copia_es_consistente_aunque_la_base_este_abierta(datos: Path, tmp_path: Path) -> None:
    con = sqlite3.connect(config.ruta_historial("homo"))
    try:
        con.execute("BEGIN")  # una conexión abierta no impide el backup
        r = backup.crear_backup(tmp_path / "bk", AHORA)
    finally:
        con.close()
    assert "historial.sqlite" in r.archivos


def test_comandos_backup_y_restaurar(datos: Path, tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["backup", str(tmp_path / "salida")])
    assert r.exit_code == 0, r.output
    assert "No incluye la clave privada" in r.output
    (zip_,) = (tmp_path / "salida").glob("factumono-backup-*.zip")
    r = runner.invoke(cli.app, ["restaurar", str(zip_)], input="n\n")
    assert "Cancelado" in r.output
    r = runner.invoke(cli.app, ["restaurar", str(zip_), "--yes"])
    assert r.exit_code == 0 and "Restaurado" in r.output and "La config no se tocó" in r.output
    assert runner.invoke(cli.app, ["restaurar", str(tmp_path / "x.zip"), "--yes"]).exit_code == 1
