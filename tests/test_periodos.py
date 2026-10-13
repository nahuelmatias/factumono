"""Modo de período por cliente y concepto 1/2/3, en el núcleo, la CLI, el MCP y la GUI."""
# ruff: noqa: E501  (tablas de casos con fechas)

import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from factumono.cli import app as cli
from factumono.core import armado, service
from factumono.core.models import (
    Cliente,
    Comprobante,
    Concepto,
    parsear_concepto,
    parsear_modo_periodo,
)
from factumono.storage.clientes import Clientes
from factumono.storage.historial import Historial, Registro
from tests import test_cli, test_mcp
from tests.test_service import ArcaFalso

runner = CliRunner()
HOY = date(2026, 10, 13)
wsfe_falso = test_cli.wsfe_falso
entorno = test_mcp.entorno


# --- concepto y modos --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1", Concepto.PRODUCTOS), ("productos", Concepto.PRODUCTOS), (1, Concepto.PRODUCTOS),
        ("2", Concepto.SERVICIOS), ("Servicios", Concepto.SERVICIOS), (2, Concepto.SERVICIOS),
        ("3", Concepto.PRODUCTOS_Y_SERVICIOS), ("ambos", Concepto.PRODUCTOS_Y_SERVICIOS),
        ("productos y servicios", Concepto.PRODUCTOS_Y_SERVICIOS),
        (Concepto.SERVICIOS, Concepto.SERVICIOS),
    ],
)  # fmt: skip
def test_concepto_1_2_3(texto: str | int | Concepto, esperado: Concepto) -> None:
    assert parsear_concepto(texto) == esperado


def test_concepto_invalido() -> None:
    with pytest.raises(ValueError, match="1 \\(productos\\), 2 \\(servicios\\) o 3"):
        parsear_concepto("4")


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [("mes", "mes_completo"), ("dia", "dia_emision"), ("Día", "dia_emision"),
     ("rango", "rango"), ("sin", "sin_periodo"), ("auto", "auto")],
)  # fmt: skip
def test_modos_de_periodo(texto: str, esperado: str) -> None:
    assert parsear_modo_periodo(texto) == esperado
    with pytest.raises(ValueError, match="Modo de período inválido"):
        parsear_modo_periodo("quincena")


def test_sin_periodo_solo_vale_para_productos() -> None:
    assert armado.validar_modo_y_concepto("sin", "1") == ("sin_periodo", "productos")
    for concepto in ("2", "3"):
        with pytest.raises(ValueError, match="solo vale para productos"):
            armado.validar_modo_y_concepto("sin", concepto)
    assert armado.validar_modo_y_concepto("rango", "3") == ("rango", "ambos")


# --- resolver el período -----------------------------------------------------------------------


def test_resolver_periodo_por_modo() -> None:
    mes = Concepto.SERVICIOS
    assert armado.resolver_periodo("mes_completo", mes, HOY) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert armado.resolver_periodo("dia_emision", mes, HOY) == (HOY, HOY)
    assert armado.resolver_periodo("auto", mes, HOY) == (date(2026, 10, 1), date(2026, 10, 31))
    # un rango explícito gana sobre el modo
    explicito = (date(2026, 9, 1), date(2026, 9, 15))
    assert armado.resolver_periodo("dia_emision", mes, HOY, *explicito) == explicito
    # los productos no llevan período, sea cual sea el modo
    assert armado.resolver_periodo("mes_completo", Concepto.PRODUCTOS, HOY) is None
    with pytest.raises(ValueError, match="indicá desde y hasta"):
        armado.resolver_periodo("rango", mes, HOY)
    with pytest.raises(ValueError, match="necesitan período"):
        armado.resolver_periodo("sin_periodo", mes, HOY)
    with pytest.raises(ValueError, match="las dos fechas"):
        armado.resolver_periodo("rango", mes, HOY, desde=HOY)
    with pytest.raises(ValueError, match="anterior al inicio"):
        armado.resolver_periodo("rango", mes, HOY, date(2026, 10, 5), date(2026, 10, 1))


def test_armar_comprobante_con_cada_concepto(comprobante: Comprobante) -> None:
    base = dict(
        perfil=comprobante.perfil, punto_venta=2, cbte_tipo=11, receptor=comprobante.receptor,
        items=comprobante.items, hoy=HOY,
    )  # fmt: skip
    productos = armado.armar_comprobante(concepto="1", **base)  # type: ignore[arg-type]
    assert productos.concepto == Concepto.PRODUCTOS and productos.periodo_desde is None
    assert productos.vto_pago is None
    servicios = armado.armar_comprobante(concepto=2, modo_periodo="dia_emision", **base)  # type: ignore[arg-type]
    assert servicios.concepto == Concepto.SERVICIOS
    assert (servicios.periodo_desde, servicios.periodo_hasta) == (HOY, HOY)
    assert servicios.vto_pago is not None
    ambos = armado.armar_comprobante(concepto="3", vto_dias=10, **base)  # type: ignore[arg-type]
    assert ambos.concepto == Concepto.PRODUCTOS_Y_SERVICIOS
    assert ambos.periodo_desde == date(2026, 10, 1) and ambos.vto_pago == date(2026, 10, 23)


# --- «la de siempre» respeta el modo ---------------------------------------------------------


def _ultima(
    comprobante: Comprobante, desde: date, hasta: date, concepto: Concepto = Concepto.SERVICIOS
) -> Registro:
    c = replace(
        comprobante, concepto=concepto, periodo_desde=desde, periodo_hasta=hasta,
        fecha=date(2026, 9, 28), vto_pago=date(2026, 10, 2),
    )  # fmt: skip
    return service.emitir(
        Historial(), ArcaFalso(), perfil="d", ambiente="homo", c=c, clave=f"{desde}{hasta}"
    ).registro


@pytest.mark.parametrize(
    ("modo", "ultimo", "esperado"),
    [
        # mes completo: el mes siguiente al último, completo, aunque el último fuera un solo día
        ("mes_completo", (date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31))),
        ("mes_completo", (date(2026, 9, 15), date(2026, 9, 15)), (date(2026, 10, 1), date(2026, 10, 31))),
        ("dia_emision", (date(2026, 9, 1), date(2026, 9, 30)), (HOY, HOY)),
        ("rango", (date(2026, 9, 3), date(2026, 9, 17)), (date(2026, 10, 3), date(2026, 10, 17))),
        # auto: como siempre (un solo día conserva su día; el mes completo pasa al siguiente)
        ("auto", (date(2026, 5, 1), date(2026, 5, 1)), (date(2026, 6, 1), date(2026, 6, 1))),
        ("auto", (date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31))),
    ],
)  # fmt: skip
def test_la_de_siempre_segun_el_modo(
    comprobante: Comprobante,
    modo: str,
    ultimo: tuple[date, date],
    esperado: tuple[date, date],
) -> None:
    nueva = armado.repetir_ultima(_ultima(comprobante, *ultimo), hoy=HOY, modo=modo)
    assert (nueva.periodo_desde, nueva.periodo_hasta) == esperado


def test_la_de_siempre_sin_periodo_y_productos(comprobante: Comprobante) -> None:
    servicios = _ultima(comprobante, date(2026, 9, 1), date(2026, 9, 30))
    with pytest.raises(ValueError, match="sin período"):
        armado.repetir_ultima(servicios, hoy=HOY, modo="sin_periodo")
    prod = replace(
        comprobante,
        concepto=Concepto.PRODUCTOS,
        periodo_desde=None,
        periodo_hasta=None,
        vto_pago=None,
    )
    reg = service.emitir(
        Historial(), ArcaFalso(), perfil="d", ambiente="prod", c=prod, clave="p"
    ).registro
    nueva = armado.repetir_ultima(reg, hoy=HOY, modo="sin_periodo")
    assert nueva.periodo_desde is None and nueva.concepto == Concepto.PRODUCTOS


# --- guardado de clientes (y bases de versiones anteriores) --------------------------------


def test_el_cliente_guarda_modo_condicion_y_plantilla() -> None:
    c = Clientes()
    c.guardar("p", Cliente("a", "A", 80, 30000000007, "Consumidor Final", modo_periodo="rango",
                           concepto="ambos", condicion_venta="Cuenta corriente", plantilla="[]"))  # fmt: skip
    g = c.buscar("p", "a")
    assert g and (g.modo_periodo, g.concepto, g.condicion_venta, g.plantilla) == (
        "rango", "ambos", "Cuenta corriente", "[]",
    )  # fmt: skip


def test_una_base_vieja_se_actualiza_y_sus_clientes_quedan_en_modo_auto(tmp_path: Path) -> None:
    ruta = tmp_path / "vieja.sqlite"
    con = sqlite3.connect(ruta)
    con.executescript(
        """CREATE TABLE clientes (perfil TEXT NOT NULL, alias TEXT NOT NULL, nombre TEXT NOT NULL,
        doc_tipo INTEGER NOT NULL, doc_nro TEXT NOT NULL, condicion_iva TEXT NOT NULL,
        domicilio TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        descripcion TEXT NOT NULL DEFAULT '', unidad TEXT NOT NULL DEFAULT 'otras unidades',
        concepto TEXT NOT NULL DEFAULT 'servicios', vto_dias INTEGER NOT NULL DEFAULT 4,
        PRIMARY KEY (perfil, alias));
        INSERT INTO clientes (perfil, alias, nombre, doc_tipo, doc_nro, condicion_iva)
        VALUES ('p', 'seba', 'SEBA', 96, '123', 'Consumidor Final');"""
    )
    con.commit()
    con.close()
    seba = Clientes(ruta).buscar("p", "seba")
    assert seba and seba.modo_periodo == "auto" and seba.condicion_venta == "Contado"
    Clientes(ruta)  # abrirla otra vez no rompe


# --- CLI -----------------------------------------------------------------------------------


def test_cli_cliente_con_modo_y_concepto(wsfe_falso: test_cli.WsfeFalso) -> None:
    r = runner.invoke(
        cli.app,
        ["cliente", "agregar", "acme", "--nombre", "ACME", "--cuit", "30000000007",
         "--condicion-iva", "Consumidor Final", "--descripcion", "Mantenimiento",
         "--concepto", "3", "--modo-periodo", "dia"],
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    listado = runner.invoke(cli.app, ["cliente", "listar"]).output
    assert "ambos" in listado and "día de emisión" in listado
    r = runner.invoke(
        cli.app,
        ["cliente", "agregar", "malo", "--nombre", "M", "--cuit", "30000000007",
         "--condicion-iva", "Consumidor Final", "--concepto", "2", "--modo-periodo", "sin"],
    )  # fmt: skip
    assert r.exit_code == 1 and "solo vale para productos" in r.output
    assert runner.invoke(cli.app, ["cliente", "agregar", "x", "--nombre", "X", "--cuit", "30000000007",
                                   "--condicion-iva", "Consumidor Final", "--concepto", "9"]).exit_code == 1  # fmt: skip
    r = runner.invoke(
        cli.app, ["cliente", "editar", "acme", "--modo-periodo", "rango", "--concepto", "2"]
    )
    assert (
        r.exit_code == 0
        and "rango de fechas" in runner.invoke(cli.app, ["cliente", "listar"]).output
    )


def test_cli_emitir_usa_el_modo_del_cliente(wsfe_falso: test_cli.WsfeFalso) -> None:
    runner.invoke(
        cli.app,
        ["cliente", "agregar", "acme", "--nombre", "ACME", "--cuit", "30000000007",
         "--condicion-iva", "Consumidor Final", "--descripcion", "Mantenimiento",
         "--modo-periodo", "dia"],
    )  # fmt: skip
    r = runner.invoke(cli.app, ["emitir", "--cliente", "acme", "--precio", "100", "--yes"])
    assert r.exit_code == 0, r.output
    c = wsfe_falso.autorizados[1]
    assert c.periodo_desde == c.periodo_hasta == c.fecha  # día de emisión
    # un modo explícito en la línea de comandos gana sobre el del cliente
    r = runner.invoke(
        cli.app,
        ["emitir", "--cliente", "acme", "--precio", "100", "--modo-periodo", "mes", "--yes"],
    )
    c2 = wsfe_falso.autorizados[2]
    assert c2.periodo_desde == c2.fecha.replace(day=1)


def test_cli_emitir_productos_y_servicios(wsfe_falso: test_cli.WsfeFalso) -> None:
    r = runner.invoke(
        cli.app,
        [
            "emitir",
            "--descripcion",
            "Equipo + instalación",
            "--precio",
            "500",
            "--concepto",
            "3",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "Productos y servicios" in r.output
    c = wsfe_falso.autorizados[1]
    assert c.concepto == Concepto.PRODUCTOS_Y_SERVICIOS and c.periodo_desde and c.vto_pago
    r = runner.invoke(
        cli.app, ["emitir", "--descripcion", "Pan", "--precio", "5", "--concepto", "1", "--yes"]
    )
    assert wsfe_falso.autorizados[2].periodo_desde is None


# --- MCP ---------------------------------------------------------------------------------------


def test_mcp_cliente_con_modo_y_la_de_siempre(entorno: test_mcp.Entorno) -> None:
    h = entorno.h
    assert "✅" in h.guardar_cliente(
        "taller", "TALLER SRL", "IVA Responsable Inscripto", cuit="30000000007",
        descripcion="Mantenimiento", concepto="3", modo_periodo="dia",
    )  # fmt: skip
    prep = h.preparar_comprobante(cliente="taller", importe="100")
    assert (
        "Productos y servicios" in prep
        and "13/10/2026 al 13/10/2026" in prep
        or "al 10/10/2026" in prep
    )
    bid = test_mcp._id(prep)
    h.emitir_comprobante(bid, True)
    siempre = h.preparar_comprobante(cliente="taller", repetir_ultima=True)
    assert (
        "Es la de siempre" in siempre and "10/10/2026 al 10/10/2026" in siempre
    )  # día de emisión (hoy)
    # reemplazar sin repetir el modo no lo pierde
    h.guardar_cliente("taller", "TALLER SRL", "IVA Responsable Inscripto", cuit="30000000007",
                      concepto="3", reemplazar=True)  # fmt: skip
    guardado = entorno.h._contexto(None).clientes.buscar("default", "taller")
    assert guardado is not None and guardado.modo_periodo == "dia_emision"


def test_mcp_concepto_sin_periodo_para_servicios_es_un_error_claro(
    entorno: test_mcp.Entorno,
) -> None:
    r = entorno.h.guardar_cliente(
        "x", "X", "Consumidor Final", cuit="30000000007", concepto="2", modo_periodo="sin"
    )
    assert "❌" in r and "solo vale para productos" in r
    r = entorno.h.preparar_comprobante(cliente="seba", importe="10", modo_periodo="sin")
    assert "❌" in r and "necesitan período" in r
    ok = entorno.h.preparar_comprobante(cliente="seba", importe="10", concepto="1")
    assert "Productos" in ok and "Período facturado" not in ok


def test_mcp_modo_rango_pide_las_fechas(entorno: test_mcp.Entorno) -> None:
    h = entorno.h
    r = h.preparar_comprobante(cliente="seba", importe="10", modo_periodo="rango")
    assert "❌" in r and "desde y hasta" in r
    ok = h.preparar_comprobante(
        cliente="seba", importe="10", modo_periodo="rango", periodo="03/09/2026 al 17/09/2026"
    )
    assert "03/09/2026 al 17/09/2026" in ok
