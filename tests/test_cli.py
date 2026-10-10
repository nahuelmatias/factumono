from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from facturitas import __version__, config
from facturitas.arca.wsfe import CondicionIva, ResultadoCae
from facturitas.cli import app as cli
from facturitas.core.models import Comprobante, Credencial, Perfil

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
class WsfeFalso:
    emitidos: list[Comprobante] = field(default_factory=list)

    def resolver_condicion_iva(self, texto: str, clase: str = "C") -> CondicionIva:
        return CondicionIva(5, "Consumidor Final", "C")

    def proximo_numero(self, pv: int, tipo: int) -> int:
        return 42

    def emitir(self, c: Comprobante) -> ResultadoCae:
        self.emitidos.append(c)
        return ResultadoCae("A", "70417054367476", "20261020", 42)


@pytest.fixture
def wsfe_falso(
    monkeypatch: pytest.MonkeyPatch, perfil: Perfil, credencial: Credencial
) -> WsfeFalso:
    falso = WsfeFalso()
    ctx: Any = cli.Contexto(perfil, credencial, None, falso, "HOMOLOGACIÓN (pruebas)")  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_contexto", lambda nombre: ctx)
    return falso


ARGS_EMITIR = ["emitir", "--descripcion", "Servicios de prueba", "--precio", "1500"]


def test_emitir_muestra_preview_y_no_emite_si_decimos_que_no(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, ARGS_EMITIR, input="n\n")
    assert "PREVIEW" in r.output
    assert "1.500,00" in r.output
    assert "Cancelado" in r.output
    assert wsfe_falso.emitidos == []


def test_emitir_con_confirmacion(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, ARGS_EMITIR, input="s\n")
    assert r.exit_code == 0
    assert "70417054367476" in r.output
    assert len(wsfe_falso.emitidos) == 1


def test_emitir_yes_igual_muestra_el_preview(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, [*ARGS_EMITIR, "--yes"])
    assert "PREVIEW" in r.output
    assert len(wsfe_falso.emitidos) == 1
    c = wsfe_falso.emitidos[0]
    assert c.receptor.doc_tipo == 99  # sin CUIT: consumidor final sin identificar
    assert c.vto_pago and c.periodo_desde and c.periodo_hasta


def test_emitir_con_cuit_invalido_no_emite(wsfe_falso: WsfeFalso) -> None:
    r = runner.invoke(cli.app, [*ARGS_EMITIR, "--receptor-cuit", "30000000008", "--yes"])
    assert r.exit_code == 1
    assert "verificador" in r.output
    assert wsfe_falso.emitidos == []


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
