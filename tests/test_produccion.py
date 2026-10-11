"""Soporte de producción: separación por ambiente, activación con chequeos y guard de tests."""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from facturitas import config
from facturitas.arca import ambientes
from facturitas.arca.wsfe import PuntoVenta
from facturitas.cli import app as cli
from facturitas.contexto import Contexto, construir
from facturitas.core import service
from facturitas.core.diagnostico import evaluar_punto_venta
from facturitas.core.models import Comprobante, Credencial, Perfil
from facturitas.pdf.salida import ruta_pdf
from facturitas.storage.historial import Historial
from tests.conftest import ProduccionBloqueada, es_host_de_produccion
from tests.test_cli import WsaaFalso, WsfeFalso
from tests.test_service import ArcaFalso

runner = CliRunner()


# --- guard -----------------------------------------------------------------------------------


def test_el_guard_bloquea_los_hosts_de_produccion(sin_produccion: list[str]) -> None:
    for url in (ambientes.PROD.wsaa_url, ambientes.PROD.wsfe_url):
        assert es_host_de_produccion(httpx.URL(url).host)
        with pytest.raises(ProduccionBloqueada):
            httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))).post(url)
    assert len(sin_produccion) == 2
    sin_produccion.clear()  # esperado en este test: no hacer fallar el teardown


def test_el_guard_deja_pasar_homologacion_y_otros_hosts() -> None:
    for url in (ambientes.HOMO.wsaa_url, ambientes.HOMO.wsfe_url, "https://example.com/"):
        assert not es_host_de_produccion(httpx.URL(url).host)
    assert es_host_de_produccion("wsaa.afip.gov.ar")
    assert es_host_de_produccion("algo.nuevo.arca.gob.ar")


def test_el_guard_corta_tambien_la_resolucion_de_dns(sin_produccion: list[str]) -> None:
    import socket

    with pytest.raises(ProduccionBloqueada):
        socket.getaddrinfo("servicios1.afip.gov.ar", 443)
    sin_produccion.clear()


# --- ambientes -------------------------------------------------------------------------------


def test_los_avisos_de_ambiente() -> None:
    assert ambientes.PROD.banner == "⚠️ PRODUCCIÓN — comprobante con validez fiscal"
    assert ambientes.HOMO.banner.startswith("🧪 HOMOLOGACIÓN")
    assert ambientes.obtener("producción") is ambientes.PROD
    with pytest.raises(ValueError):
        ambientes.obtener("otro")


# --- config / contexto -----------------------------------------------------------------------


@pytest.fixture
def cfg_con_prod(perfil: Perfil, credencial: Credencial) -> config.Config:
    prod = replace(credencial, ambiente="prod")
    p = replace(perfil, credencial_prod="default-prod", punto_venta_prod=7, punto_venta=1)
    config.guardar(config.Config({"default": credencial, "default-prod": prod}, {"default": p}))
    return config.cargar()


def test_config_por_defecto_es_homologacion_y_persiste_prod(cfg_con_prod: config.Config) -> None:
    assert cfg_con_prod.ambiente == "homo"
    p = cfg_con_prod.perfil()
    assert (p.credencial_prod, p.punto_venta_prod, p.aviso_duplicados) == ("default-prod", 7, True)
    cfg_con_prod.ambiente = "prod"
    config.guardar(cfg_con_prod)
    assert config.cargar().ambiente == "prod"


def test_perfil_para_usa_credencial_y_punto_de_venta_de_cada_ambiente(
    cfg_con_prod: config.Config,
) -> None:
    p, c = cfg_con_prod.perfil_para(None, "homo")
    assert (p.punto_venta, c.ambiente) == (1, "homo")
    p, c = cfg_con_prod.perfil_para(None, "prod")
    assert (p.punto_venta, c.ambiente, p.credencial) == (7, "prod", "default-prod")


def test_prod_sin_configurar_da_un_error_claro(perfil: Perfil, credencial: Credencial) -> None:
    cfg = config.Config({"default": credencial}, {"default": perfil})
    with pytest.raises(config.ConfigError, match="init --ambiente prod"):
        cfg.perfil_para(None, "prod")


def test_una_credencial_de_homo_no_sirve_en_prod(cfg_con_prod: config.Config) -> None:
    cfg_con_prod.credenciales["default-prod"] = replace(
        cfg_con_prod.credenciales["default-prod"], ambiente="homo"
    )
    with pytest.raises(config.ConfigError, match="no es de producción"):
        cfg_con_prod.perfil_para(None, "prod")


def test_cache_e_historial_separados_por_ambiente(cfg_con_prod: config.Config) -> None:
    assert config.directorio_cache("prod") != config.directorio_cache("homo")
    assert config.ruta_historial("prod") != config.ruta_historial("homo")
    homo, prod = construir(None, "homo"), construir(None, "prod")
    assert homo.historial and prod.historial
    assert homo.historial.ruta != prod.historial.ruta
    assert (prod.ambiente, prod.perfil.punto_venta) == ("prod", 7)
    assert prod.aviso_ambiente == ambientes.PROD.banner
    assert prod.wsaa._amb is ambientes.PROD


def test_lo_emitido_en_un_ambiente_no_aparece_en_el_otro(
    cfg_con_prod: config.Config, comprobante: Comprobante
) -> None:
    homo, prod = construir(None, "homo"), construir(None, "prod")
    assert homo.historial and prod.historial
    service.emitir(
        homo.historial, ArcaFalso(), perfil="default", ambiente="homo", c=comprobante, clave="k"
    )
    assert len(homo.historial.listar()) == 1
    assert prod.historial.listar() == []


# --- PDF -------------------------------------------------------------------------------------


def test_los_pdf_de_prod_van_sin_subcarpeta_ni_marca(
    comprobante: Comprobante, tmp_path: Path
) -> None:
    h = Historial()
    cfg = config.Config(carpeta_pdf=str(tmp_path / "pdf"))
    prod = service.emitir(
        h, ArcaFalso(), perfil="matias", ambiente="prod", c=comprobante, clave="p"
    )
    homo = service.emitir(
        h, ArcaFalso(), perfil="matias", ambiente="homo", c=comprobante, clave="h"
    )
    assert prod.registro.es_homologacion is False
    assert ruta_pdf(cfg, prod.registro).parent == tmp_path / "pdf" / "matias"
    assert ruta_pdf(cfg, homo.registro).parent == tmp_path / "pdf" / "matias" / "homologacion"


# --- estado / ambiente usar ------------------------------------------------------------------


def _pv(nro: int = 7, tipo: str = "Factura Electrónica - Monotributo - Web Services") -> PuntoVenta:
    return PuntoVenta(nro, tipo, False, "NULL")


def test_evaluar_punto_venta_en_produccion() -> None:
    assert evaluar_punto_venta([_pv()], 7, True) == ([], [])
    problemas, _ = evaluar_punto_venta([_pv(2)], 7, True)
    assert "no figura entre los de web services" in problemas[0]
    assert "ningún punto de venta" in evaluar_punto_venta([], 7, True)[0][0]
    assert "Falta definir" in evaluar_punto_venta([_pv()], None, True)[0][0]
    assert "bloqueado" in evaluar_punto_venta([PuntoVenta(7, "x", True, "NULL")], 7, True)[0][0]
    assert "de baja" in evaluar_punto_venta([PuntoVenta(7, "x", False, "20260101")], 7, True)[0][0]
    # en homologación no es un problema, solo un aviso (o nada si la lista viene vacía)
    assert evaluar_punto_venta([], 7, False) == ([], [])
    assert evaluar_punto_venta([_pv(2)], 7, False)[0] == []


@pytest.fixture
def prod_listo(monkeypatch: pytest.MonkeyPatch, cfg_con_prod: config.Config) -> WsfeFalso:
    """Config con prod registrada y un ARCA falso en lugar de la red."""
    falso = WsfeFalso(puntos=[_pv(7), _pv(3, "Comprobantes en línea")])

    def ctx(nombre: str | None, ambiente: str | None = None) -> Contexto:
        amb = ambiente or config.cargar().ambiente
        real = construir(nombre, amb)
        cli._banner_prod(amb)  # igual que el `_contexto` real
        return Contexto(
            real.perfil, real.credencial, WsaaFalso(), falso,  # type: ignore[arg-type]
            real.etiqueta, perfil_nombre="default", ambiente=amb, historial=real.historial,
        )  # fmt: skip

    monkeypatch.setattr(cli, "_contexto", ctx)
    monkeypatch.setattr(cli, "construir", ctx)
    return falso


def _homo_probada(comprobante: Comprobante) -> None:
    service.emitir(
        Historial(config.ruta_historial("homo")),
        ArcaFalso(),
        perfil="default",
        ambiente="homo",
        c=comprobante,
        clave="prueba",
    )


def test_estado_en_prod_lista_puntos_de_venta_con_tipo_y_marca_el_configurado(
    prod_listo: WsfeFalso,
) -> None:
    r = runner.invoke(cli.app, ["estado", "--ambiente", "prod"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("⚠️ PRODUCCIÓN — comprobante con validez fiscal")
    assert "00007  Factura Electrónica - Monotributo - Web Services   ← configurado" in r.output
    assert "00003  Comprobantes en línea" in r.output
    assert "Producción lista" in r.output
    assert "el ambiente activo es HOMOLOGACIÓN" in r.output  # solo un chequeo


def test_estado_en_prod_falla_si_el_punto_de_venta_no_es_de_web_services(
    prod_listo: WsfeFalso,
) -> None:
    prod_listo.puntos = [_pv(3, "Comprobantes en línea")]
    r = runner.invoke(cli.app, ["estado", "--ambiente", "prod"])
    assert r.exit_code == 1
    assert "✗" in r.output and "(7) no figura entre los de web services" in r.output


def test_no_se_activa_prod_si_estado_no_da_ok(
    prod_listo: WsfeFalso, comprobante: Comprobante
) -> None:
    _homo_probada(comprobante)
    prod_listo.puntos = []
    r = runner.invoke(cli.app, ["ambiente", "usar", "prod", "--yes"])
    assert r.exit_code == 1 and "No se activó producción" in r.output
    assert config.cargar().ambiente == "homo"


def test_no_se_activa_prod_sin_una_emision_de_prueba_en_homo(prod_listo: WsfeFalso) -> None:
    r = runner.invoke(cli.app, ["ambiente", "usar", "prod", "--yes"])
    assert r.exit_code == 1 and "emisión de prueba autorizada en homologación" in r.output
    assert config.cargar().ambiente == "homo"


def test_se_activa_prod_con_todo_ok_y_se_puede_volver_a_homo(
    prod_listo: WsfeFalso, comprobante: Comprobante
) -> None:
    _homo_probada(comprobante)
    # pregunta antes de activar: si decimos que no, no cambia
    r = runner.invoke(cli.app, ["ambiente", "usar", "prod"], input="n\n")
    assert "Cancelado" in r.output and config.cargar().ambiente == "homo"
    r = runner.invoke(cli.app, ["ambiente", "usar", "prod"], input="s\n")
    assert r.exit_code == 0, r.output
    assert "Ambiente activo: PRODUCCIÓN" in r.output and config.cargar().ambiente == "prod"
    assert runner.invoke(cli.app, ["ambiente"]).output.startswith("⚠️ PRODUCCIÓN")
    r = runner.invoke(cli.app, ["ambiente", "usar", "homo"])
    assert r.exit_code == 0 and config.cargar().ambiente == "homo"


def test_config_ambiente_es_equivalente(prod_listo: WsfeFalso, comprobante: Comprobante) -> None:
    _homo_probada(comprobante)
    r = runner.invoke(cli.app, ["config", "--ambiente", "prod"], input="s\n")
    assert r.exit_code == 0, r.output
    assert config.cargar().ambiente == "prod"


def test_toda_salida_de_la_cli_en_prod_empieza_con_el_aviso(prod_listo: WsfeFalso) -> None:
    cfg = config.cargar()
    cfg.ambiente = "prod"
    config.guardar(cfg)
    for args in (["historial"], ["config"], ["ambiente"], ["estado", "--offline"]):
        r = runner.invoke(cli.app, args)
        assert r.output.startswith("⚠️ PRODUCCIÓN — comprobante con validez fiscal"), args


def test_init_prod_registra_credencial_y_punto_de_venta_sin_activar(
    cfg_con_prod: config.Config, credencial: Credencial
) -> None:
    cfg_con_prod.perfiles["default"] = replace(
        cfg_con_prod.perfil(), credencial_prod="", punto_venta_prod=None, punto_venta=1
    )
    cfg_con_prod.credenciales.pop("default-prod")
    config.guardar(cfg_con_prod)
    r = runner.invoke(
        cli.app,
        ["init", "--ambiente", "prod", "--cert", credencial.cert, "--key", credencial.key,
         "--punto-venta", "2"],
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    cfg = config.cargar()
    p = cfg.perfil()
    assert (p.credencial_prod, p.punto_venta_prod, p.punto_venta) == ("default-prod", 2, 1)
    assert cfg.credenciales["default-prod"].ambiente == "prod"
    assert cfg.ambiente == "homo" and "NO está activa" in r.output


def test_init_prod_exige_el_perfil_de_homologacion() -> None:
    r = runner.invoke(cli.app, ["init", "--ambiente", "prod", "--punto-venta", "2"])
    assert r.exit_code == 1 and "Primero configurá el perfil" in r.output
