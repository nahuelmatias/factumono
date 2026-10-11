from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from facturitas import __version__, config
from facturitas.arca.wsaa import Ticket
from facturitas.arca.wsfe import CondicionIva, PuntoVenta
from facturitas.cli import app as cli
from facturitas.contexto import Contexto
from facturitas.core.models import Comprobante, Credencial, Perfil
from facturitas.storage.historial import Historial
from tests.test_service import ArcaFalso

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_sin_argumentos_muestra_la_ayuda() -> None:
    result = runner.invoke(cli.app, [])
    assert "Comandos" in result.output
    assert "emitir" in result.output
    assert "Missing command" not in result.output


@dataclass
class WsfeFalso(ArcaFalso):
    """El ARCA falso de test_service, con lo que además usa el CLI."""

    puntos: list[PuntoVenta] = field(default_factory=list)

    def resolver_condicion_iva(self, texto: str, clase: str = "C") -> CondicionIva:
        return CondicionIva(5, "Consumidor Final", "C")

    def condiciones_iva_receptor(self, clase: str = "C") -> list[CondicionIva]:
        return [
            CondicionIva(5, "Consumidor Final", "C"),
            CondicionIva(1, "IVA Responsable Inscripto", "C"),
        ]

    def dummy(self) -> dict[str, str]:
        return {"AppServer": "OK", "DbServer": "OK", "AuthServer": "OK"}

    def puntos_venta(self) -> list[PuntoVenta]:
        return self.puntos


class WsaaFalso:
    def ticket(self, servicio: str = "wsfe") -> Ticket:
        return Ticket("t", "s", datetime(2026, 10, 11, 6, 43, tzinfo=UTC))


@pytest.fixture
def wsfe_falso(
    monkeypatch: pytest.MonkeyPatch, perfil: Perfil, credencial: Credencial, tmp_path: Path
) -> WsfeFalso:
    falso = WsfeFalso()
    config.guardar(
        config.Config(
            {"default": credencial}, {"default": perfil}, carpeta_pdf=str(tmp_path / "pdf")
        )
    )
    ctx: Any = Contexto(
        perfil,
        credencial,
        WsaaFalso(),  # type: ignore[arg-type]
        falso,  # type: ignore[arg-type]
        "HOMOLOGACIÓN (pruebas)",
        perfil_nombre="default",
        ambiente="homo",
        historial=Historial(),
    )
    monkeypatch.setattr(cli, "_contexto", lambda nombre, ambiente=None: ctx)
    return falso


ARGS_EMITIR = ["emitir", "--descripcion", "Servicios de prueba", "--precio", "1500"]


def test_emitir_muestra_preview_y_no_emite_si_decimos_que_no(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, ARGS_EMITIR, input="n\n")
    assert "PREVIEW" in r.output
    assert "1.500,00" in r.output
    assert "Cancelado" in r.output
    assert wsfe_falso.autorizados == {}


def test_emitir_con_confirmacion(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, ARGS_EMITIR, input="s\n")
    assert r.exit_code == 0
    assert "70000000000001" in r.output
    assert len(wsfe_falso.autorizados) == 1


def test_emitir_yes_igual_muestra_el_preview(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, [*ARGS_EMITIR, "--yes"])
    assert "PREVIEW" in r.output
    assert len(wsfe_falso.autorizados) == 1
    c = wsfe_falso.autorizados[1]
    assert c.receptor.doc_tipo == 99  # sin CUIT: consumidor final sin identificar
    assert c.vto_pago and c.periodo_desde and c.periodo_hasta


def test_emitir_con_cuit_invalido_no_emite(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, [*ARGS_EMITIR, "--receptor-cuit", "30000000008", "--yes"])
    assert r.exit_code == 1
    assert "verificador" in r.output
    assert wsfe_falso.autorizados == {}


def test_estado_offline_no_toca_la_red(perfil: Perfil, credencial: Credencial) -> None:
    config.guardar(config.Config({"default": credencial}, {"default": perfil}))
    r = runner.invoke(cli.app, ["estado", "--offline"])
    assert r.exit_code == 0
    assert "HOMOLOGACIÓN" in r.output
    assert "offline" in r.output


def test_init_guarda_perfil(tmp_path: Path, credencial: Credencial) -> None:
    r = runner.invoke(
        cli.app,
        [
            "init",
            "--cuit",
            "20000000001",
            "--cert",
            credencial.cert,
            "--key",
            credencial.key,
            "--razon-social",
            "EMISOR FICTICIO",
            "--condicion-iva",
            "Responsable Monotributo",
            "--domicilio",
            "Calle Falsa 123",
            "--punto-venta",
            "2",
            "--nombre-fantasia",
            "La Esquina",
        ],
    )
    assert r.exit_code == 0, r.output
    p = config.cargar().perfil()
    assert (p.punto_venta, p.nombre_fantasia) == (2, "La Esquina")


def test_init_rechaza_clave_que_no_corresponde(tmp_path: Path, credencial: Credencial) -> None:
    otra = tmp_path / "otra.key"
    otra.write_text(Path(credencial.key).read_text().replace("A", "B", 1))
    r = runner.invoke(
        cli.app,
        [
            "init",
            "--cuit",
            "20000000001",
            "--cert",
            credencial.cert,
            "--key",
            str(otra),
            "--razon-social",
            "X",
            "--condicion-iva",
            "Responsable Monotributo",
            "--domicilio",
            "Y",
        ],
    )
    assert r.exit_code != 0


def test_emitir_muestra_fechas_ar_y_guarda_el_pdf(wsfe_falso: WsfeFalso, tmp_path: Path) -> None:
    r = runner.invoke(cli.app, [*ARGS_EMITIR, "--yes"])
    assert r.exit_code == 0, r.output
    assert "Vto. CAE: 20/10/2026" in r.output  # nunca AAAAMMDD
    assert "20261020" not in r.output
    pdfs = list((tmp_path / "pdf" / "default" / "homologacion").glob("*.pdf"))
    assert [p.name for p in pdfs] == ["20000000001_011_00002_00000001.pdf"]


def test_preview_parte_la_descripcion_en_vez_de_truncarla(wsfe_falso: WsfeFalso) -> None:
    larga = "Servicios de consultoría en innovación tecnológica y transformación digital"
    r = runner.invoke(cli.app, ["emitir", "--descripcion", larga, "--precio", "10", "--yes"])
    assert "Servicios de consultoría en" in r.output
    assert "transformación digital" in r.output  # la cola no se perdió


def test_aviso_de_duplicado_es_informativo_y_no_pide_confirmacion_extra(
    wsfe_falso: WsfeFalso,
) -> None:
    args = [*ARGS_EMITIR, "--receptor-cuit", "30000000007", "--condicion-iva", "5"]
    assert runner.invoke(cli.app, [*args, "--yes"]).exit_code == 0
    # segunda vez: avisa, pero con una sola confirmación emite
    r = runner.invoke(cli.app, args, input="s\n")
    assert "ya emitiste algo parecido" in r.output and "Es solo un aviso" in r.output
    assert "Parece un duplicado" not in r.output
    assert r.exit_code == 0 and len(wsfe_falso.autorizados) == 2


def test_el_aviso_de_duplicado_se_puede_desactivar_por_perfil(wsfe_falso: WsfeFalso) -> None:
    ctx = cli._contexto(None)
    ctx.perfil = replace(ctx.perfil, aviso_duplicados=False)
    args = [*ARGS_EMITIR, "--receptor-cuit", "30000000007", "--condicion-iva", "5", "--yes"]
    runner.invoke(cli.app, args)
    r = runner.invoke(cli.app, args)
    assert "ya emitiste algo parecido" not in r.output
    assert len(wsfe_falso.autorizados) == 2


def test_corte_de_red_al_emitir_no_duplica(wsfe_falso: WsfeFalso) -> None:
    wsfe_falso.corte = "despues"
    r = runner.invoke(cli.app, [*ARGS_EMITIR, "--yes"])
    assert r.exit_code == 0, r.output
    assert "no se emitió dos veces" in r.output
    assert len(wsfe_falso.autorizados) == 1


def test_historial_marca_homologacion(wsfe_falso: WsfeFalso) -> None:
    runner.invoke(cli.app, [*ARGS_EMITIR, "--yes"])
    r = runner.invoke(cli.app, ["historial"])
    assert "HOMO" in r.output and "FC 00002-00000001" in r.output
    assert "autorizado" in r.output and "1.500,00" in r.output
    assert "Todavía no hay" in runner.invoke(cli.app, ["historial", "--buscar", "nadie"]).output


def test_historial_vacio(wsfe_falso: WsfeFalso) -> None:
    assert "Todavía no hay" in runner.invoke(cli.app, ["historial"]).output


def test_pdf_reimprime(wsfe_falso: WsfeFalso, tmp_path: Path) -> None:
    runner.invoke(cli.app, [*ARGS_EMITIR, "--yes"])
    salida = tmp_path / "otra"
    r = runner.invoke(cli.app, ["pdf", "1", "--carpeta", str(salida)])
    assert r.exit_code == 0, r.output
    assert (salida / "default" / "homologacion" / "20000000001_011_00002_00000001.pdf").is_file()
    assert runner.invoke(cli.app, ["pdf", "99"]).exit_code == 1


def test_nota_de_credito(wsfe_falso: WsfeFalso) -> None:
    runner.invoke(cli.app, [*ARGS_EMITIR, "--yes"])
    r = runner.invoke(cli.app, ["nota-credito", "1", "--motivo", "Error", "--yes"])
    assert r.exit_code == 0, r.output
    assert "Nota de Crédito C" in r.output and "Comprobante autorizado" in r.output
    assert wsfe_falso.autorizados[2].cbte_tipo == 13
    # anular de nuevo avisa y pide confirmación extra
    r = runner.invoke(cli.app, ["nota-credito", "1", "--motivo", "Otra"], input="s\nn\n")
    assert "ya tiene una nota de crédito" in r.output and "Cancelado" in r.output


def test_recuperar_y_reconciliar(wsfe_falso: WsfeFalso, comprobante: Comprobante) -> None:
    wsfe_falso.autorizados[1] = comprobante  # emitido antes de existir el historial
    r = runner.invoke(cli.app, ["recuperar", "1", "2"])
    assert "recuperado" in r.output and "ARCA no lo tiene" in r.output
    assert "FC 00002-00000001" in runner.invoke(cli.app, ["historial"]).output
    assert "ya estaba en el historial" in runner.invoke(cli.app, ["recuperar", "1"]).output
    assert "No hay nada sin resolver" in runner.invoke(cli.app, ["reconciliar"]).output


def test_estado_en_homologacion_sin_puntos_de_venta_es_informativo(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, ["estado"])
    assert r.exit_code == 0, r.output
    assert "ℹ Sin puntos de venta listados" in r.output
    assert "✗" not in r.output  # la ✗ queda para producción
    assert "init --punto-venta" not in r.output  # ya hay uno configurado (el 2)


def test_estado_sugiere_definir_punto_de_venta_solo_si_falta(
    wsfe_falso: WsfeFalso, perfil: Perfil, credencial: Credencial
) -> None:
    sin_pv = Perfil(**{**perfil.__dict__, "punto_venta": None})
    config.guardar(config.Config({"default": credencial}, {"default": sin_pv}))
    cli._contexto(None).perfil = sin_pv
    r = runner.invoke(cli.app, ["estado"])
    assert "init --punto-venta" in r.output


def test_config_carpeta_pdf(tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["config", "--carpeta-pdf", str(tmp_path / "x")])
    assert r.exit_code == 0 and str(tmp_path / "x") in r.output
    assert config.cargar().carpeta_pdf == str(tmp_path / "x")


def test_cliente_agregar_listar_editar_borrar(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(
        cli.app,
        ["cliente", "agregar", "acciona", "--nombre", "ACCIONA IT S.R.L", "--cuit", "30710581467",
         "--condicion-iva", "IVA Responsable Inscripto", "--descripcion", "Servicios de innovación",
         "--vto-dias", "0"],
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    listado = runner.invoke(cli.app, ["cliente", "listar"]).output
    assert "ACCIONA IT S.R.L" in listado and "30-71058146-7" in listado and "vto. 0 días" in listado
    r = runner.invoke(cli.app, ["cliente", "editar", "acciona", "--email", "a@b.com"])
    assert r.exit_code == 0 and "a@b.com" in runner.invoke(cli.app, ["cliente", "listar"]).output
    repetido = ["cliente", "agregar", "acciona", "--nombre", "X", "--cuit", "30000000007"]
    assert runner.invoke(cli.app, [*repetido, "--condicion-iva", "Consumidor Final"]).exit_code == 1
    assert runner.invoke(cli.app, ["cliente", "borrar", "acciona"], input="n\n").exit_code == 0
    assert "acciona" in runner.invoke(cli.app, ["cliente", "listar"]).output
    assert runner.invoke(cli.app, ["cliente", "borrar", "acciona", "--yes"]).exit_code == 0
    assert "No hay clientes" in runner.invoke(cli.app, ["cliente", "listar"]).output


def test_emitir_con_cliente_y_repetir_ultima(wsfe_falso: WsfeFalso) -> None:
    runner.invoke(
        cli.app,
        ["cliente", "agregar", "seba", "--nombre", "MARTIN SEBASTIAN PABLO",
         "--cuit", "20354267277", "--condicion-iva", "Consumidor Final",
         "--descripcion", "Servicios informáticos"],
    )  # fmt: skip
    r = runner.invoke(
        cli.app,
        ["emitir", "--cliente", "seba", "--precio", "1500", "--periodo", "09/2026", "--yes"],
    )
    assert r.exit_code == 0, r.output
    assert "Servicios informáticos" in r.output and "01/09/2026 al 30/09/2026" in r.output
    r = runner.invoke(cli.app, ["emitir", "--cliente", "seba", "--repetir-ultima", "--yes"])
    assert r.exit_code == 0, r.output
    assert "Período corrido a 01/10/2026 al 31/10/2026" in r.output
    assert len(wsfe_falso.autorizados) == 2
    assert "necesita --cliente" in runner.invoke(cli.app, ["emitir", "--repetir-ultima"]).output


def test_mcp_instalar_solo_mostrar_no_modifica_nada(tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["mcp", "instalar", "--solo-mostrar"])
    assert r.exit_code == 0 and "claude mcp add facturitas" in r.output
    ruta = tmp_path / "cfg.json"
    r = runner.invoke(cli.app, ["mcp", "instalar", "--config", str(ruta)])
    assert r.exit_code == 0 and "configurado" in r.output and ruta.is_file()
