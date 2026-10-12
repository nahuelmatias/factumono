import base64
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from facturitas import __version__, config
from facturitas.arca.qr import URL_QR, url_qr
from facturitas.core.models import CbteAsociado, Comprobante, Concepto, Item
from facturitas.pdf.render import renderizar
from facturitas.pdf.salida import guardar_pdf, nombre_archivo, ruta_pdf
from facturitas.storage.historial import AUTORIZADO, Historial
from tests.conftest import CUIT_EMISOR, CUIT_RECEPTOR

CAE = "70417054367476"
VTO = date(2026, 10, 20)


def textos(pdf: bytes) -> list[str]:
    return [p.extract_text() for p in PdfReader(BytesIO(pdf)).pages]


def test_qr_sigue_la_especificacion_oficial() -> None:
    # Ejemplo de la especificación del QR (RG 4892)
    url = url_qr(
        fecha=date(2020, 10, 13),
        cuit="30000000007",
        punto_venta=10,
        cbte_tipo=1,
        numero=94,
        importe=Decimal("12100"),
        doc_tipo=80,
        doc_nro=20000000001,
        cae=CAE,
        moneda="DOL",
        cotizacion=Decimal(65),
    )
    assert url.startswith("https://www.arca.gob.ar/fe/qr/?p=")
    assert json.loads(base64.b64decode(url.removeprefix(URL_QR))) == {
        "ver": 1,
        "fecha": "2020-10-13",
        "cuit": 30000000007,
        "ptoVta": 10,
        "tipoCmp": 1,
        "nroCmp": 94,
        "importe": 12100,
        "moneda": "DOL",
        "ctz": 65,
        "tipoDocRec": 80,
        "nroDocRec": 20000000001,
        "tipoCodAut": "E",
        "codAut": 70417054367476,
    }


def test_qr_importe_con_decimales() -> None:
    url = url_qr(
        fecha=date(2026, 10, 10),
        cuit=CUIT_EMISOR,
        punto_venta=1,
        cbte_tipo=11,
        numero=1,
        importe=Decimal("1234.50"),
        doc_tipo=99,
        doc_nro=0,
        cae=CAE,
    )
    datos = json.loads(base64.b64decode(url.removeprefix(URL_QR)))
    assert datos["importe"] == 1234.5 and datos["ctz"] == 1 and datos["nroDocRec"] == 0


def test_pdf_tiene_original_duplicado_y_triplicado(comprobante: Comprobante) -> None:
    pdf = renderizar(comprobante, 42, CAE, VTO, homologacion=False)
    paginas = textos(pdf)
    assert len(paginas) == 3
    for pagina, copia in zip(paginas, ("ORIGINAL", "DUPLICADO", "TRIPLICADO"), strict=True):
        assert copia in pagina
        assert "COD. 011" in pagina and "FACTURA" in pagina
        assert "00002" in pagina and "00000042" in pagina  # pto. de venta y número
        assert CAE in pagina and "20/10/2026" in pagina  # CAE y vencimiento en DD/MM/AAAA
        assert "Período Facturado Desde:" in pagina and "01/10/2026" in pagina
        assert "RECEPTOR FICTICIO SRL" in pagina and "Pág. 1/1" in pagina
        assert "1234,56" in pagina  # importes al estilo ARCA: sin miles y con coma
        assert "HOMOLOGACIÓN" not in pagina


def test_pdf_de_homologacion_lleva_marca(comprobante: Comprobante) -> None:
    pdf = renderizar(comprobante, 1, CAE, VTO, homologacion=True)
    assert "HOMOLOGACIÓN" in textos(pdf)[0]


def test_pdf_de_productos_no_muestra_periodo(comprobante: Comprobante) -> None:
    c = replace(
        comprobante,
        concepto=Concepto.PRODUCTOS,
        periodo_desde=None,
        periodo_hasta=None,
        vto_pago=None,
        items=(
            Item("Facturas", Decimal(1), Decimal("5000"), "docena", "P1"),
            Item("Pan", Decimal("0.5"), Decimal("3000"), "kg", "P2"),
        ),
    )
    pagina = textos(renderizar(c, 1, CAE, VTO, homologacion=False))[0]
    assert "Período Facturado" not in pagina
    assert "docena" in pagina and "0,50" in pagina and "6500,00" in pagina


def test_pdf_con_muchos_items_pagina_cada_copia(comprobante: Comprobante) -> None:
    items = tuple(
        Item(f"Producto número {n} " + "largo " * 8, Decimal(1), Decimal(10)) for n in range(40)
    )
    c = replace(comprobante, items=items)
    paginas = textos(renderizar(c, 1, CAE, VTO, homologacion=False))
    por_copia = len(paginas) // 3
    assert por_copia > 1 and len(paginas) == por_copia * 3
    assert f"Pág. 1/{por_copia}" in paginas[0]
    assert "Importe Total" not in paginas[0]  # los totales van solo en la última hoja
    assert "Importe Total" in paginas[por_copia - 1] and "400,00" in paginas[por_copia - 1]


def test_pdf_de_nota_de_credito(comprobante: Comprobante) -> None:
    nc = replace(
        comprobante,
        cbte_tipo=13,
        cbte_asoc=CbteAsociado(11, 2, 41, CUIT_EMISOR, date(2026, 10, 1)),
        motivo="Error de facturación",
    )
    pagina = textos(renderizar(nc, 5, CAE, VTO, homologacion=False))[0]
    assert "NOTA DE CRÉDITO" in pagina and "COD. 013" in pagina
    assert "Factura C 00002-00000041" in pagina and "Error de facturación" in pagina


def test_receptor_con_dni_y_consumidor_final(comprobante: Comprobante) -> None:
    receptor = replace(comprobante.receptor, doc_tipo=96, doc_nro=12345678)
    dni = replace(comprobante, receptor=receptor)
    assert "DNI:" in textos(renderizar(dni, 1, CAE, VTO, homologacion=False))[0]
    cf = replace(comprobante, receptor=replace(receptor, doc_tipo=99, doc_nro=0))
    pagina = textos(renderizar(cf, 1, CAE, VTO, homologacion=False))[0]
    assert "DNI:" not in pagina and CUIT_RECEPTOR not in pagina


def test_nombre_de_archivo_igual_al_de_arca() -> None:
    assert nombre_archivo("20000000001", 11, 1, 40) == "20000000001_011_00001_00000040.pdf"
    assert nombre_archivo("20000000001", 13, 12, 7) == "20000000001_013_00012_00000007.pdf"


def test_guardar_pdf_en_carpeta_configurable(tmp_path: Path, comprobante: Comprobante) -> None:
    destino = tmp_path / "mis facturas"
    cfg = config.Config(carpeta_pdf=str(destino))
    config.guardar(cfg)
    assert config.cargar().carpeta_base_pdf() == destino
    h = Historial()
    reg = h.crear(
        perfil="matias", ambiente="homo", c=comprobante, clave="k", estado=AUTORIZADO, numero=42
    )
    reg = h.actualizar(reg.id, cae=CAE, cae_vto=VTO)
    ruta = guardar_pdf(cfg, reg)
    # Homologación en subcarpeta aparte: su numeración choca con la de los comprobantes reales
    assert ruta == destino / "matias" / "homologacion" / f"{CUIT_EMISOR}_011_00002_00000042.pdf"
    assert ruta.read_bytes().startswith(b"%PDF")
    prod = h.actualizar(reg.id, ambiente="prod")
    assert ruta_pdf(cfg, prod) == destino / "matias" / f"{CUIT_EMISOR}_011_00002_00000042.pdf"


def test_carpeta_por_defecto_es_documentos_facturitas() -> None:
    base = config.Config().carpeta_base_pdf()
    assert base.name == "Facturitas" and base.parent.name.lower() in ("documents", "documentos")


def test_metadata_del_pdf(comprobante: Comprobante) -> None:
    meta = PdfReader(BytesIO(renderizar(comprobante, 1, CAE, VTO, homologacion=False))).metadata
    assert meta is not None
    assert meta.author == "EMISOR FICTICIO"
    assert meta.creator == f"Facturitas {__version__}" == meta.producer


def test_el_qr_sigue_estrictamente_la_especificacion_y_no_lleva_items() -> None:
    """RG 4892: solo estos campos. Los ítems viven en el PDF y el preview, no en el QR."""
    import base64
    import inspect
    import json
    from datetime import date
    from decimal import Decimal

    from facturitas.arca import qr

    url = qr.url_qr(
        fecha=date(2026, 10, 12), cuit="20000000001", punto_venta=2, cbte_tipo=11, numero=1,
        importe=Decimal("1234.56"), doc_tipo=80, doc_nro=30000000007, cae="70000000000001",
    )  # fmt: skip
    datos = json.loads(base64.b64decode(url.removeprefix(qr.URL_QR)))
    assert set(datos) == {
        "ver", "fecha", "cuit", "ptoVta", "tipoCmp", "nroCmp", "importe", "moneda", "ctz",
        "tipoDocRec", "nroDocRec", "tipoCodAut", "codAut",
    }  # fmt: skip
    assert "items" not in inspect.signature(qr.datos_qr).parameters
