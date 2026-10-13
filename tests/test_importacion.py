"""Importar facturas anteriores: lectura de PDF y «Mis Comprobantes», plan y guardado.

Los PDF de prueba los genera Factumono (mismo diseño que los de ARCA) con datos ficticios. Los
reales de `referencia_privada/` solo sirven para validar a mano y nunca están en el repo.
"""

# ruff: noqa: E501  (tablas de datos y CSV de ejemplo)

import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from reportlab.pdfgen.canvas import Canvas
from typer.testing import CliRunner

from factumono import config
from factumono.app.server import COOKIE
from factumono.cli import app as cli
from factumono.contexto import Contexto
from factumono.core import armado, importacion
from factumono.core.lectura import (
    FacturaLeida,
    NoReconocido,
    leer_mis_comprobantes,
    leer_pdf,
)
from factumono.core.models import (
    Comprobante,
    Concepto,
    DocTipo,
    Item,
    Receptor,
)
from factumono.pdf.render import renderizar
from factumono.pdf.salida import guardar_pdf
from factumono.storage.historial import Historial
from tests import test_app, test_cli, test_mcp

runner = CliRunner()
wsfe_falso = test_cli.wsfe_falso
entorno = test_mcp.entorno
mundo = test_app.mundo
HOY = date(2026, 10, 12)


# --- armado de PDF ficticios -------------------------------------------------------------------


def hacer_pdf(
    destino: Path,
    c: Comprobante,
    numero: int = 7,
    cae: str = "70000000000007",
    cae_vto: date = date(2026, 10, 22),
) -> Path:
    destino.write_bytes(renderizar(c, numero, cae, cae_vto, homologacion=False))
    return destino


def con_items(c: Comprobante, *items: Item) -> Comprobante:
    return replace(c, items=tuple(items))


# --- lectura de PDF ---------------------------------------------------------------------------


def test_lee_un_pdf_completo_y_valida_el_qr(comprobante: Comprobante, tmp_path: Path) -> None:
    f = leer_pdf(hacer_pdf(tmp_path / "a.pdf", comprobante))
    assert isinstance(f, FacturaLeida)
    assert (f.origen, f.cbte_tipo, f.letra, f.punto_venta, f.numero) == ("factumono", 11, "C", 2, 7)
    assert f.emisor_cuit == "20000000001" and f.emisor_nombre == "EMISOR FICTICIO"
    assert f.emisor_condicion_iva == "Responsable Monotributo" and f.emisor_iibb == "Exento"
    assert f.emisor_inicio_actividades == date(2020, 1, 1)
    assert f.emisor_domicilio == "Calle Falsa 123 - Ciudad de Buenos Aires"
    assert (f.receptor_doc_tipo, f.receptor_doc_nro) == (80, 30000000007)
    assert f.receptor_nombre == "RECEPTOR FICTICIO SRL"
    assert f.receptor_condicion_iva == "IVA Responsable Inscripto"
    assert f.concepto == Concepto.SERVICIOS and f.condicion_venta == "Contado"
    assert (f.periodo_desde, f.periodo_hasta, f.vto_pago) == (
        date(2026, 10, 1), date(2026, 10, 31), date(2026, 10, 14),
    )  # fmt: skip
    assert f.fecha == date(2026, 10, 10) and f.total == Decimal("1234.56")
    assert (f.cae, f.cae_vto) == ("70000000000007", date(2026, 10, 22))
    (item,) = f.items
    assert (item.descripcion, item.cantidad, item.unidad) == (
        "Servicios de prueba", Decimal(1), "otras unidades",
    )  # fmt: skip
    assert (item.precio_unitario, item.subtotal, item.codigo) == (
        Decimal("1234.56"), Decimal("1234.56"), "1",
    )  # fmt: skip
    # el QR se decodificó y coincide con el texto
    assert f.qr and f.qr["nroCmp"] == 7 and f.qr["codAut"] == 70000000000007
    assert not f.inconsistente and f.advertencias == []
    assert f.es_externo_completo


def test_lee_bonificacion_descripcion_larga_y_varios_items(
    comprobante: Comprobante, tmp_path: Path
) -> None:
    larga = (
        "Desarrollo a medida con una descripción bastante larga que tiene que envolver en líneas"
    )
    c = con_items(
        comprobante,
        Item(larga, Decimal("2.5"), Decimal(1000), "horas", "A1", Decimal(10)),
        Item("Soporte", Decimal(1), Decimal(500), "unidades", "B2"),
    )
    f = leer_pdf(hacer_pdf(tmp_path / "b.pdf", c))
    assert isinstance(f, FacturaLeida) and f.advertencias == []
    a, b = f.items
    assert a.descripcion == larga and a.codigo == "A1" and a.unidad == "horas"
    assert (a.cantidad, a.precio_unitario, a.bonificacion_pct) == (
        Decimal("2.5"), Decimal(1000), Decimal(10),
    )  # fmt: skip
    assert (a.bonificacion_importe, a.subtotal) == (Decimal(250), Decimal(2250))
    assert (b.descripcion, b.subtotal) == ("Soporte", Decimal(500)) and f.total == Decimal(2750)


def test_lee_una_factura_de_muchos_items_en_varias_hojas(
    comprobante: Comprobante, tmp_path: Path
) -> None:
    items = [
        Item(f"Ítem {i}", Decimal(1), Decimal(10 + i), "unidades", str(i)) for i in range(1, 36)
    ]
    f = leer_pdf(hacer_pdf(tmp_path / "m.pdf", con_items(comprobante, *items)))
    assert isinstance(f, FacturaLeida)
    assert len(f.items) == 35 and f.total == sum((i.subtotal for i in f.items), Decimal(0))
    assert f.items[-1].descripcion == "Ítem 35" and f.advertencias == []


def test_productos_no_llevan_periodo(comprobante: Comprobante, tmp_path: Path) -> None:
    c = replace(
        con_items(
            comprobante,
            Item("Pan", Decimal("0.5"), Decimal(3000), "kg"),
            Item("Facturas", Decimal(1), Decimal(5000), "docena"),
        ),
        concepto=Concepto.PRODUCTOS, periodo_desde=None, periodo_hasta=None, vto_pago=None,
    )  # fmt: skip
    f = leer_pdf(hacer_pdf(tmp_path / "p.pdf", c))
    assert isinstance(f, FacturaLeida)
    assert f.concepto == Concepto.PRODUCTOS and f.periodo_desde is None and f.vto_pago is None
    assert f.receptor_condicion_iva == "IVA Responsable Inscripto"  # sin la fila de período
    assert [(i.descripcion, i.unidad) for i in f.items] == [("Pan", "kg"), ("Facturas", "docena")]


def test_consumidor_final_sin_identificar_toma_el_documento_del_qr(
    comprobante: Comprobante, tmp_path: Path
) -> None:
    cf = Receptor(int(DocTipo.CONSUMIDOR_FINAL), 0, "Consumidor Final", 5, "Consumidor Final")
    f = leer_pdf(hacer_pdf(tmp_path / "cf.pdf", replace(comprobante, receptor=cf)))
    assert isinstance(f, FacturaLeida)
    assert f.receptor_doc_tipo == 99 and f.receptor_doc_nro == 0 and not f.inconsistente


def test_si_el_qr_no_coincide_con_el_texto_se_avisa_y_no_se_registra(
    comprobante: Comprobante, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factumono.arca import qr
    from factumono.pdf import render

    def qr_adulterado(**kw: object) -> str:
        kw["importe"] = Decimal("999999")
        return qr.url_qr(**kw)

    monkeypatch.setattr(render, "url_qr", qr_adulterado)
    f = leer_pdf(hacer_pdf(tmp_path / "x.pdf", comprobante))
    assert isinstance(f, FacturaLeida) and f.inconsistente and not f.es_externo_completo
    assert any("El QR no coincide con el texto" in a and "importe" in a for a in f.advertencias)


def test_los_items_que_no_cierran_con_el_total_avisan(
    comprobante: Comprobante, tmp_path: Path
) -> None:
    f = leer_pdf(hacer_pdf(tmp_path / "a.pdf", comprobante))
    assert isinstance(f, FacturaLeida)
    f.items.append(replace(f.items[0], subtotal=Decimal(1)))
    from factumono.core import lectura

    f.advertencias.clear()
    lectura._controlar(f)
    assert any("suman" in a for a in f.advertencias)


def test_un_pdf_que_no_es_de_arca_devuelve_el_texto_crudo(tmp_path: Path) -> None:
    ruta = tmp_path / "otro.pdf"
    c = Canvas(str(ruta))
    c.drawString(72, 700, "Presupuesto 123 para Fulano")
    c.drawString(72, 680, "Total: 5000 pesos")
    c.save()
    r = leer_pdf(ruta)
    assert isinstance(r, NoReconocido)
    assert "Presupuesto 123" in r.texto and "diseño de un comprobante de ARCA" in r.motivo


def test_un_pdf_que_es_una_imagen_no_se_reconoce(tmp_path: Path) -> None:
    from PIL import Image

    ruta = tmp_path / "foto.pdf"
    Image.new("RGB", (400, 600), "white").save(ruta, "PDF")
    r = leer_pdf(ruta)
    assert isinstance(r, NoReconocido) and "no tiene texto" in r.motivo and r.texto == ""


def test_un_archivo_que_no_es_pdf_no_se_reconoce(tmp_path: Path) -> None:
    ruta = tmp_path / "falso.pdf"
    ruta.write_text("no soy un pdf")
    r = leer_pdf(ruta)
    assert isinstance(r, NoReconocido) and "PDF válido" in r.motivo


# --- carpetas y tipos de archivo ---------------------------------------------------------------


def test_leer_rutas_con_carpeta_y_extension_desconocida(
    comprobante: Comprobante, tmp_path: Path
) -> None:
    (tmp_path / "sub").mkdir()
    hacer_pdf(tmp_path / "uno.pdf", comprobante, numero=1)
    hacer_pdf(tmp_path / "sub" / "dos.pdf", comprobante, numero=2, cae="70000000000002")
    lecturas, mis = importacion.leer_rutas([tmp_path])
    assert mis is None and sorted(
        (x.numero or 0) for x in lecturas if isinstance(x, FacturaLeida)
    ) == [
        1,
        2,
    ]
    (tmp_path / "nota.docx").write_text("x")
    with pytest.raises(ValueError, match="No sé importar"):
        importacion.leer_rutas([tmp_path / "nota.docx"])
    with pytest.raises(ValueError, match="No existe"):
        importacion.leer_rutas([tmp_path / "nada.pdf"])


# --- plan y guardado ---------------------------------------------------------------------------


def _ctx(entorno: test_mcp.Entorno) -> Contexto:
    return entorno.h._contexto(None)


def test_el_plan_no_escribe_nada_y_aplicar_crea_cliente_plantilla_e_historico(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    pdf = hacer_pdf(tmp_path / "f.pdf", comprobante)
    lecturas, _ = importacion.leer_rutas([pdf])
    plan = importacion.planear(ctx, lecturas)
    (a,) = plan.archivos
    assert a.cliente and a.cliente.accion == "crear" and a.cliente.cliente.alias == "receptor"
    assert a.comprobante and a.comprobante.accion == "registrar"
    assert ctx.clientes.buscar("default", "receptor") is None  # planear no guardó nada
    assert "se CREA el cliente" in plan.texto() and "QR: leído y coincide" in plan.texto()

    res = importacion.aplicar(ctx, plan)
    assert res.clientes_creados == ["receptor"] and len(res.comprobantes) == 1
    cli = ctx.clientes.buscar("default", "receptor")
    assert cli and cli.modo_periodo == "mes_completo" and cli.concepto == "servicios"
    assert (cli.descripcion, cli.unidad, cli.vto_dias) == (
        "Servicios de prueba",
        "otras unidades",
        4,
    )
    assert cli.condicion_iva == "IVA Responsable Inscripto" and "Calle" not in cli.domicilio
    assert [i.descripcion for i in armado.items_de_plantilla(cli)] == ["Servicios de prueba"]
    # el comprobante quedó como histórico EXTERNO y cuenta para el tope
    assert ctx.historial
    reg = ctx.historial.por_numero("20000000001", "homo", 2, 11, 7)
    assert reg and reg.externo and reg.estado == "autorizado" and reg.cae == "70000000000007"
    assert reg.cae_vto == date(2026, 10, 22) and not reg.recuperado
    assert ctx.historial.resumen("20000000001", "homo", date(2026, 10, 1), date(2026, 10, 31))[:2] == (
        1, Decimal("1234.56"),
    )  # fmt: skip


def test_importar_dos_veces_no_duplica(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    lecturas, _ = importacion.leer_rutas([hacer_pdf(tmp_path / "f.pdf", comprobante)])
    importacion.aplicar(ctx, importacion.planear(ctx, lecturas))
    plan = importacion.planear(ctx, lecturas)
    (a,) = plan.archivos
    assert a.cliente and a.cliente.accion == "sin_cambios"
    assert a.comprobante and a.comprobante.accion == "ya_estaba"
    assert not plan.hay_algo_para_guardar
    res = importacion.aplicar(ctx, plan)
    assert res.comprobantes == [] and len(ctx.clientes.listar("default")) == 3  # acciona, seba + 1


def test_un_cliente_que_ya_existe_se_actualiza_por_documento(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    existente = ctx.clientes.buscar("default", "acciona")
    assert existente and existente.doc_nro == 30710581467
    receptor = replace(comprobante.receptor, doc_nro=30710581467, nombre="ACCIONA IT S.R.L")
    c = con_items(
        replace(comprobante, receptor=receptor),
        Item("Servicios nuevos", Decimal(2), Decimal(500), "horas", "9"),
    )
    lecturas, _ = importacion.leer_rutas(
        [hacer_pdf(tmp_path / "a.pdf", c, numero=9, cae="70000000000009")]
    )
    plan = importacion.planear(ctx, lecturas)
    (a,) = plan.archivos
    assert a.cliente and a.cliente.accion == "actualizar" and a.cliente.cliente.alias == "acciona"
    assert "plantilla de ítems actualizado" in a.cliente.cambios
    importacion.aplicar(ctx, plan)
    cli = ctx.clientes.buscar("default", "acciona")
    assert cli and cli.email == existente.email  # lo que no viene en la factura se conserva
    assert cli.descripcion == "Servicios nuevos" and cli.unidad == "horas"
    (item,) = armado.items_de_plantilla(cli)
    assert (item.cantidad, item.precio_unitario, item.codigo) == (Decimal(2), Decimal(500), "9")


def test_el_ultimo_pdf_de_cada_cliente_manda_aunque_se_importen_desordenados(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    viejo = con_items(
        replace(comprobante, fecha=date(2026, 8, 5)), Item("Viejo", Decimal(1), Decimal(1), "u")
    )
    nuevo = con_items(
        replace(comprobante, fecha=date(2026, 9, 5)), Item("Nuevo", Decimal(1), Decimal(2), "u")
    )
    hacer_pdf(tmp_path / "1-nuevo.pdf", nuevo, numero=2, cae="70000000000002")
    hacer_pdf(tmp_path / "2-viejo.pdf", viejo, numero=1, cae="70000000000001")
    lecturas, _ = importacion.leer_rutas([tmp_path])
    importacion.aplicar(ctx, importacion.planear(ctx, lecturas))
    cli = ctx.clientes.buscar("default", "receptor")
    assert cli and cli.descripcion == "Nuevo"


def test_si_el_emisor_no_es_el_perfil_no_se_importa(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    ajeno = replace(comprobante, perfil=replace(comprobante.perfil, cuit="27000000009"))
    lecturas, _ = importacion.leer_rutas([hacer_pdf(tmp_path / "x.pdf", ajeno)])
    plan = importacion.planear(ctx, lecturas)
    (a,) = plan.archivos
    assert a.cliente is None and a.comprobante is None
    assert any("no es el de este perfil" in x for x in a.avisos) and not plan.hay_algo_para_guardar


def test_consumidor_final_se_registra_pero_no_crea_cliente(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    cf = Receptor(int(DocTipo.CONSUMIDOR_FINAL), 0, "Consumidor Final", 5, "Consumidor Final")
    lecturas, _ = importacion.leer_rutas(
        [hacer_pdf(tmp_path / "c.pdf", replace(comprobante, receptor=cf))]
    )
    plan = importacion.planear(ctx, lecturas)
    (a,) = plan.archivos
    assert a.cliente is None and a.comprobante and a.comprobante.accion == "registrar"
    assert any("Consumidor final sin identificar" in x for x in a.avisos)


def test_sin_historico_solo_toca_clientes(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    lecturas, _ = importacion.leer_rutas([hacer_pdf(tmp_path / "f.pdf", comprobante)])
    plan = importacion.planear(ctx, lecturas, registrar_historico=False)
    assert plan.archivos[0].comprobante is None
    assert importacion.aplicar(ctx, plan).comprobantes == []


def test_un_qr_inconsistente_no_se_registra_pero_el_cliente_si(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    f = leer_pdf(hacer_pdf(tmp_path / "a.pdf", comprobante))
    assert isinstance(f, FacturaLeida)
    f.inconsistente = True
    plan = importacion.planear(ctx, [f])
    assert plan.archivos[0].comprobante and plan.archivos[0].comprobante.accion == "omitir"
    assert "revisá el PDF a mano" in plan.archivos[0].comprobante.motivo


def test_un_externo_no_se_reimprime_con_el_formato_de_factumono(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    lecturas, _ = importacion.leer_rutas([hacer_pdf(tmp_path / "f.pdf", comprobante)])
    (reg,) = importacion.aplicar(ctx, importacion.planear(ctx, lecturas)).comprobantes
    with pytest.raises(ValueError, match="no la emitió Factumono"):
        guardar_pdf(config.Config(carpeta_pdf=str(tmp_path / "out")), reg)


def test_la_de_siempre_y_facturar_sin_importe_usan_lo_importado(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    lecturas, _ = importacion.leer_rutas([hacer_pdf(tmp_path / "f.pdf", comprobante)])
    importacion.aplicar(ctx, importacion.planear(ctx, lecturas))
    siempre = entorno.h.preparar_comprobante(cliente="receptor", repetir_ultima=True)
    assert "Es la de siempre" in siempre and "01/11/2026 al 30/11/2026" in siempre
    assert "$ 1.234,56" in siempre
    plantilla = entorno.h.preparar_comprobante(cliente="receptor")  # sin importe: usa la plantilla
    assert "Uso la plantilla" in plantilla and "Servicios de prueba" in plantilla


# --- «Mis Comprobantes» -----------------------------------------------------------------------

CSV_MIS = (
    "Fecha de Emisión;Tipo de Comprobante;Punto de Venta;Número Desde;Número Hasta;"
    "Cód. Autorización;Tipo Doc. Receptor;Nro. Doc. Receptor;Denominación Receptor;Moneda;Imp. Total\n"
    "2026-05-22;11 - Factura C;2;103;103;86217041783938;80;20354267277;MARTIN SEBASTIAN;PES;1500,00\n"
    "2026-06-10;11 - Factura C;2;104;104;86217041783939;99;0;Consumidor Final;PES;250,50\n"
    "2026-06-11;13 - Nota de Crédito C;2;5;5;86217041783940;80;20354267277;MARTIN SEBASTIAN;PES;1500,00\n"
    "2026-06-12;11 - Factura C;2;105;110;86217041783941;99;0;Consumidor Final;PES;10,00\n"
    "2026-06-13;11 - Factura C;2;111;111;86217041783942;80;30000000007;EXTRANJERA;USD;10,00\n"
)


def test_lee_el_csv_de_mis_comprobantes_y_avisa_lo_que_salta(tmp_path: Path) -> None:
    ruta = tmp_path / "mis.csv"
    ruta.write_text(CSV_MIS, encoding="utf-8")
    lec = leer_mis_comprobantes(ruta)
    assert [(f.punto_venta, f.cbte_tipo, f.numero, f.total) for f in lec.filas] == [
        (2, 11, 103, Decimal("1500.00")), (2, 11, 104, Decimal("250.50")), (2, 13, 5, Decimal("1500.00")),
    ]  # fmt: skip
    assert lec.filas[0].fecha == date(2026, 5, 22) and lec.filas[0].receptor_doc_nro == 20354267277
    assert any("rango de números" in a for a in lec.advertencias)
    assert any("no es pesos" in a for a in lec.advertencias)


def test_lee_el_excel_con_filas_de_titulo_y_otras_codificaciones(tmp_path: Path) -> None:
    libro = Workbook()
    hoja = libro.active
    assert hoja is not None
    hoja.append(["Mis Comprobantes Emitidos - CUIT 20-00000000-1"])
    hoja.append([])
    cab, *filas = (linea.split(";") for linea in CSV_MIS.strip().splitlines())
    hoja.append(cab)
    for fila in filas[:2]:
        hoja.append([date(2026, 5, 22) if i == 0 else v for i, v in enumerate(fila)])
    ruta = tmp_path / "mis.xlsx"
    libro.save(ruta)
    lec = leer_mis_comprobantes(ruta)
    assert lec.cuit_emisor == "20000000001" and len(lec.filas) == 2
    assert lec.filas[0].fecha == date(2026, 5, 22)
    latin = tmp_path / "latin.csv"
    latin.write_bytes(
        CSV_MIS.replace(";", ",")
        .replace("1500,00", "1500.00")
        .replace("250,50", "250.50")
        .encode("latin-1")
    )
    assert len(leer_mis_comprobantes(latin).filas) >= 2


def test_un_archivo_que_no_es_mis_comprobantes_se_rechaza(tmp_path: Path) -> None:
    ruta = tmp_path / "cualquier.csv"
    ruta.write_text("a;b;c\n1;2;3\n")
    with pytest.raises(ValueError, match="No reconozco el archivo"):
        leer_mis_comprobantes(ruta)


def test_mis_comprobantes_se_registran_como_externos_y_no_se_duplican(
    entorno: test_mcp.Entorno, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    ruta = tmp_path / "mis.csv"
    ruta.write_text(CSV_MIS, encoding="utf-8")
    lecturas, mis = importacion.leer_rutas([ruta])
    plan = importacion.planear(ctx, lecturas, mis)
    assert len([m for m in plan.mis_comprobantes if m.accion == "registrar"]) == 3
    res = importacion.aplicar(ctx, plan)
    assert len(res.comprobantes) == 3 and all(r.externo for r in res.comprobantes)
    assert ctx.historial
    # facturas 1500 + 250,50 y una nota de crédito de 1500: neto 250,50
    nf, tf, nn, tn = ctx.historial.resumen(
        "20000000001", "homo", date(2026, 5, 1), date(2026, 6, 30)
    )
    assert (nf, tf, nn, tn) == (2, Decimal("1750.50"), 1, Decimal("1500.00"))
    segundo = importacion.planear(ctx, *importacion.leer_rutas([ruta]))
    assert not segundo.hay_algo_para_guardar
    assert sum(1 for m in segundo.mis_comprobantes if m.accion == "ya_estaba") == 3


def test_mis_comprobantes_de_otro_cuit_no_se_registra(
    entorno: test_mcp.Entorno, tmp_path: Path
) -> None:
    ctx = _ctx(entorno)
    libro = Workbook()
    hoja = libro.active
    assert hoja is not None
    hoja.append(["Emitidos - CUIT 27-00000000-9"])
    cab, *filas = (linea.split(";") for linea in CSV_MIS.strip().splitlines())
    hoja.append(cab)
    hoja.append(filas[0])
    ruta = tmp_path / "ajeno.xlsx"
    libro.save(ruta)
    plan = importacion.planear(ctx, *importacion.leer_rutas([ruta]))
    assert plan.mis_comprobantes == [] and any(
        "no del perfil" in a for a in plan.avisos_mis_comprobantes
    )


def test_los_historicos_externos_cierran_los_huecos_de_numeracion(
    entorno: test_mcp.Entorno, tmp_path: Path
) -> None:
    from factumono.core.sincronia import brechas

    ctx = _ctx(entorno)
    ruta = tmp_path / "mis.csv"
    ruta.write_text(
        CSV_MIS.splitlines()[0] + "\n" + CSV_MIS.splitlines()[1] + "\n", encoding="utf-8"
    )
    entorno.arca.autorizados[103] = Comprobante(
        perfil=ctx.perfil, punto_venta=2, cbte_tipo=11, concepto=Concepto.PRODUCTOS,
        receptor=Receptor(99, 0, "CF", 5), items=(Item("x", Decimal(1), Decimal(1)),), fecha=HOY,
    )  # fmt: skip
    assert brechas(ctx)  # ARCA tiene el 103 y el historial no
    importacion.aplicar(ctx, importacion.planear(ctx, *importacion.leer_rutas([ruta])))
    assert [b.faltantes for b in brechas(ctx)] != [(103,)]


# --- bases de versiones anteriores ---------------------------------------------------------------


def test_una_base_vieja_gana_la_columna_externo(tmp_path: Path) -> None:
    ruta = tmp_path / "vieja.sqlite"
    Historial(ruta)
    con = sqlite3.connect(ruta)
    con.executescript(
        "ALTER TABLE comprobantes DROP COLUMN externo;"
    )  # como la dejaba la versión anterior
    con.close()
    Historial(ruta)  # no rompe: la vuelve a agregar
    con = sqlite3.connect(ruta)
    columnas = {f[1] for f in con.execute("PRAGMA table_info(comprobantes)")}
    con.close()
    assert "externo" in columnas


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_importar_pide_confirmacion_y_guarda(
    wsfe_falso: test_cli.WsfeFalso, comprobante: Comprobante, tmp_path: Path
) -> None:
    pdf = hacer_pdf(tmp_path / "f.pdf", comprobante)
    r = runner.invoke(cli.app, ["importar", str(pdf)], input="n\n")
    assert r.exit_code == 0 and "se CREA el cliente" in r.output and "Cancelado" in r.output
    assert "No hay clientes" in runner.invoke(cli.app, ["cliente", "listar"]).output
    r = runner.invoke(cli.app, ["importar", str(pdf)], input="s\n")
    assert r.exit_code == 0 and "clientes creados: receptor" in r.output
    assert "receptor" in runner.invoke(cli.app, ["cliente", "listar"]).output
    r = runner.invoke(cli.app, ["importar", str(pdf), "--yes"])
    assert "No hay nada para guardar" in r.output


def test_cli_importar_no_reconocido_con_texto(
    wsfe_falso: test_cli.WsfeFalso, tmp_path: Path
) -> None:
    ruta = tmp_path / "otro.pdf"
    c = Canvas(str(ruta))
    c.drawString(72, 700, "Presupuesto para Fulano")
    c.save()
    r = runner.invoke(cli.app, ["importar", str(ruta), "--texto"])
    assert "no reconocido" in r.output and "Presupuesto para Fulano" in r.output
    assert runner.invoke(cli.app, ["importar", str(tmp_path / "x.docx")]).exit_code == 1


def test_cli_emitir_sin_precio_usa_la_plantilla(
    wsfe_falso: test_cli.WsfeFalso, comprobante: Comprobante, tmp_path: Path
) -> None:
    pdf = hacer_pdf(tmp_path / "f.pdf", comprobante)
    runner.invoke(cli.app, ["importar", str(pdf), "--yes"])
    r = runner.invoke(cli.app, ["emitir", "--cliente", "receptor", "--yes"])
    assert r.exit_code == 0 and "Uso la plantilla de «receptor»" in r.output, r.output
    emitido = wsfe_falso.autorizados[max(wsfe_falso.autorizados)]
    assert (
        emitido.total == Decimal("1234.56")
        and emitido.items[0].descripcion == "Servicios de prueba"
    )


# --- MCP ---------------------------------------------------------------------------------------


def _id_importacion(texto: str) -> str:
    return texto.split('importacion_id="')[1].split('"')[0]


def test_mcp_importar_prepara_y_solo_guarda_con_confirmacion(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    h, ctx = entorno.h, _ctx(entorno)
    pdf = hacer_pdf(tmp_path / "f.pdf", comprobante)
    r = h.importar_factura(str(pdf))
    assert r.startswith("🧪") and "Todavía NO se guardó nada" in r and "se CREA el cliente" in r
    assert ctx.clientes.buscar("default", "receptor") is None
    iid = _id_importacion(r)
    assert "🛑" in h.confirmar_importacion(iid)  # sin confirmo no hace nada
    assert "🛑" in h.confirmar_importacion(iid, False)
    assert ctx.clientes.buscar("default", "receptor") is None
    ok = h.confirmar_importacion(iid, True)
    assert "✅" in ok and "clientes creados: receptor" in ok
    assert ctx.clientes.buscar("default", "receptor") is not None
    assert "No existe la importación" in h.confirmar_importacion(iid, True)  # se usa una sola vez


def test_mcp_importar_vencida_o_en_otro_ambiente(
    entorno: test_mcp.Entorno, comprobante: Comprobante, tmp_path: Path
) -> None:
    from datetime import timedelta

    h, ctx = entorno.h, _ctx(entorno)
    iid = _id_importacion(h.importar_factura(str(hacer_pdf(tmp_path / "f.pdf", comprobante))))
    entorno.reloj[0] += timedelta(minutes=16)
    assert "venció" in h.confirmar_importacion(iid, True)
    entorno.reloj[0] -= timedelta(minutes=16)
    iid = _id_importacion(h.importar_factura(str(tmp_path / "f.pdf")))
    ctx.ambiente = "prod"
    assert "Cambió el perfil o el ambiente" in h.confirmar_importacion(iid, True)
    assert ctx.clientes.buscar("default", "receptor") is None


def test_mcp_pdf_no_reconocido_devuelve_el_texto_para_que_lo_interprete_la_ia(
    entorno: test_mcp.Entorno, tmp_path: Path
) -> None:
    ruta = tmp_path / "otro.pdf"
    c = Canvas(str(ruta))
    c.drawString(72, 700, "Factura de un tercero 4455")
    c.save()
    r = entorno.h.importar_factura(str(ruta))
    assert "No reconocido" in r and "guardar_plantilla" in r and "Factura de un tercero 4455" in r
    assert "importacion_id" not in r  # no hay nada para guardar


def test_mcp_guardar_plantilla_con_datos_de_la_ia_pide_confirmacion(
    entorno: test_mcp.Entorno,
) -> None:
    from factumono.mcp.herramientas import ItemPlantilla, PlantillaEntrada

    h, ctx = entorno.h, _ctx(entorno)
    datos = PlantillaEntrada(
        nombre="TALLER DEL SUR SRL", cuit="30000000007", condicion_iva="IVA Responsable Inscripto",
        domicilio="Ruta 3 km 10", concepto=3, periodo_desde="2026-09-01", periodo_hasta="2026-09-30",
        vto_pago="2026-10-05", fecha="2026-10-01",
        items=[ItemPlantilla(descripcion="Repuestos y mano de obra", precio_unitario="1200,50", cantidad=2)],
    )  # fmt: skip
    r = h.guardar_plantilla(datos)
    assert "se CREA el cliente" in r and "no se validaron contra un QR" in r
    assert (
        "no se registra" not in r and "histórico" not in r
    )  # los datos de la IA no van al historial
    assert ctx.clientes.buscar("default", "taller") is None
    assert "✅" in h.confirmar_importacion(_id_importacion(r), True)
    cli = ctx.clientes.buscar("default", "taller")
    assert cli and cli.concepto == "ambos" and cli.modo_periodo == "mes_completo"
    assert ctx.historial and ctx.historial.listar() == []


def test_mcp_guardar_plantilla_valida_los_datos(entorno: test_mcp.Entorno) -> None:
    from factumono.mcp.herramientas import ItemPlantilla, PlantillaEntrada

    base = {"nombre": "X", "items": [ItemPlantilla(descripcion="a", precio_unitario=1)]}
    r = entorno.h.guardar_plantilla(PlantillaEntrada(cuit="30000000008", **base))  # type: ignore[arg-type]
    assert "❌" in r and "verificador" in r
    r = entorno.h.guardar_plantilla(PlantillaEntrada(nombre="X", items=[]))
    assert "❌" in r and "Faltan los ítems" in r


def test_mcp_expone_las_tools_nuevas(entorno: test_mcp.Entorno) -> None:
    import asyncio

    from mcp import Client

    from factumono.mcp.server import crear_servidor

    async def nombres() -> set[str]:
        async with Client(crear_servidor(entorno.h)) as c:
            return {t.name for t in (await c.list_tools()).tools}

    assert {"importar_factura", "guardar_plantilla", "confirmar_importacion"} <= asyncio.run(
        nombres()
    )


# --- GUI ---------------------------------------------------------------------------------------


def test_gui_arrastra_una_factura_vieja_y_la_guarda(
    mundo: test_app.Mundo, comprobante: Comprobante, tmp_path: Path
) -> None:
    pdf = hacer_pdf(tmp_path / "vieja.pdf", comprobante)
    assert "Arrastrá una factura vieja" in mundo.c.get("/importar").text
    r = mundo.c.post("/importar/analizar", files=[("archivos", ("vieja.pdf", pdf.read_bytes()))])
    assert "Esto es lo que detecté" in r.text and "RECEPTOR FICTICIO SRL" in r.text
    assert "Servicios de prueba" in r.text and "QR leído y coincide" in r.text
    assert "crear el cliente" in r.text and "sumar a tu historial" in r.text
    assert mundo.ctx.clientes.buscar("default", "receptor") is None  # todavía nada
    iid = r.text.split('name="importacion_id" value="')[1].split('"')[0]
    ok = mundo.c.post("/importar/confirmar", data={"importacion_id": iid})
    assert "Listo: clientes creados: receptor" in ok.text
    assert mundo.ctx.clientes.buscar("default", "receptor") is not None
    otra_vez = mundo.c.post("/importar/confirmar", data={"importacion_id": iid})
    assert "ya no existe" in otra_vez.text
    # y ahora aparece en «Mis facturas» como importada
    assert "importada" in mundo.c.get("/facturas").text


def test_gui_un_archivo_que_no_se_reconoce_muestra_el_texto(
    mundo: test_app.Mundo, tmp_path: Path
) -> None:
    ruta = tmp_path / "x.pdf"
    c = Canvas(str(ruta))
    c.drawString(72, 700, "Algo que no es de ARCA 777")
    c.save()
    r = mundo.c.post("/importar/analizar", files=[("archivos", ("x.pdf", ruta.read_bytes()))])
    assert "No lo pude leer solo" in r.text and "Algo que no es de ARCA 777" in r.text
    assert "No hay nada para guardar" in r.text and "importacion_id" not in r.text


def test_gui_el_nombre_del_archivo_nunca_es_una_ruta(mundo: test_app.Mundo, tmp_path: Path) -> None:
    r = mundo.c.post(
        "/importar/analizar", files=[("archivos", ("../../../evil.pdf", b"no es un pdf"))]
    )
    assert r.status_code == 200 and "PDF válido" in r.text
    assert not (tmp_path.parent / "evil.pdf").exists()


def test_gui_sin_archivos_y_con_tipo_desconocido(mundo: test_app.Mundo) -> None:
    assert "al menos un archivo" in mundo.c.post("/importar/analizar").text
    r = mundo.c.post("/importar/analizar", files=[("archivos", ("a.docx", b"x"))])
    assert "No sé importar" in r.text


def test_gui_importa_mis_comprobantes(mundo: test_app.Mundo) -> None:
    r = mundo.c.post("/importar/analizar", files=[("archivos", ("mis.csv", CSV_MIS.encode()))])
    assert "Mis Comprobantes" in r.text and "para sumar al historial" in r.text
    iid = r.text.split('name="importacion_id" value="')[1].split('"')[0]
    assert (
        "3 comprobante(s) histórico(s)"
        in mundo.c.post("/importar/confirmar", data={"importacion_id": iid}).text
    )


def test_gui_la_sesion_es_necesaria_para_importar(mundo: test_app.Mundo) -> None:
    sin = TestClient(mundo.c.app)
    assert sin.post("/importar/analizar", files=[("archivos", ("a.pdf", b"x"))]).status_code == 403
    assert COOKIE  # la cookie existe y es la que protege
