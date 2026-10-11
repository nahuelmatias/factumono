"""Parseo de respuestas SOAP REALES de homologación (grabadas con tools/grabar_fixtures.py).

Están sanitizadas: CUIT ficticios, sin token ni sign. Complementan a las sintéticas.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from facturitas.arca.ambientes import HOMO
from facturitas.arca.errors import ComprobanteRechazado
from facturitas.arca.wsaa import Wsaa
from facturitas.arca.wsfe import Wsfe, construir_detalle_factura_c
from facturitas.core.models import CbteAsociado, Comprobante, Credencial
from tests.conftest import CUIT_EMISOR, fixture

AHORA = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def wsfe(credencial: Credencial, tmp_path: Path) -> Wsfe:
    with respx.mock:
        respx.post(HOMO.wsaa_url).mock(
            return_value=httpx.Response(200, text=fixture("wsaa_ok.xml"))
        )
        wsaa = Wsaa(credencial, HOMO, tmp_path, ahora=lambda: AHORA)
        wsaa.ticket()
    return Wsfe(CUIT_EMISOR, wsaa, HOMO, tmp_path)


def _responder(nombre: str) -> None:
    respx.post(HOMO.wsfe_url).mock(return_value=httpx.Response(200, text=fixture(f"real/{nombre}")))


@respx.mock
def test_real_dummy(wsfe: Wsfe) -> None:
    _responder("fedummy.xml")
    assert wsfe.dummy() == {"AppServer": "OK", "DbServer": "OK", "AuthServer": "OK"}


@respx.mock
def test_real_ultimo_autorizado(wsfe: Wsfe) -> None:
    _responder("ultimo.xml")
    assert wsfe.ultimo_autorizado(1, 11) == 3


@respx.mock
def test_real_sin_puntos_de_venta_en_homologacion(wsfe: Wsfe) -> None:
    _responder("ptosventa.xml")  # 602 "Sin Resultados": un estado, no un error
    assert wsfe.puntos_venta() == []


@respx.mock
def test_real_condiciones_iva(wsfe: Wsfe) -> None:
    _responder("condiciones.xml")
    por_id = {c.id: c.descripcion for c in wsfe.condiciones_iva_receptor("C")}
    assert por_id[1] == "IVA Responsable Inscripto"
    assert por_id[6] == "Responsable Monotributo"
    assert wsfe.resolver_condicion_iva("consumidor final").id == 5


@respx.mock
def test_real_consultar_existente(wsfe: Wsfe) -> None:
    _responder("consulta_existente.xml")
    c = wsfe.consultar(1, 11, 4)
    assert c is not None
    assert (c.numero, c.doc_tipo, c.doc_nro) == (4, 80, 30000000007)
    assert c.importe_total == Decimal("1234.56")
    assert (c.resultado, c.cae, c.cae_vto, c.condicion_iva_id) == (
        "A",
        "86410983342603",
        "20261020",
        1,
    )
    assert (c.fecha, c.periodo_desde, c.vto_pago) == (
        date(2026, 10, 10),
        date(2026, 10, 1),
        date(2026, 10, 10),
    )


@respx.mock
def test_real_consultar_inexistente_devuelve_none(wsfe: Wsfe) -> None:
    _responder("consulta_inexistente.xml")
    assert wsfe.consultar(1, 11, 100003) is None


@respx.mock
def test_real_cae_ok(wsfe: Wsfe, comprobante: Comprobante) -> None:
    _responder("cae_ok.xml")
    r = wsfe.solicitar_cae(comprobante, 4)
    assert (r.numero, r.cae, r.cae_vto, r.resultado) == (4, "86410983342603", "20261020", "A")


@respx.mock
def test_real_nota_de_credito_ok(wsfe: Wsfe, comprobante: Comprobante) -> None:
    _responder("nc_ok.xml")
    r = wsfe.solicitar_cae(comprobante, 1)
    assert (r.numero, r.cae) == (1, "86410983342616")


@respx.mock
def test_real_rechazo_por_condicion_iva_invalida(wsfe: Wsfe, comprobante: Comprobante) -> None:
    _responder("cae_rechazado.xml")
    with pytest.raises(ComprobanteRechazado, match="10242"):
        wsfe.solicitar_cae(comprobante, 4)


def test_detalle_de_nota_de_credito_lleva_el_comprobante_asociado(comprobante: Comprobante) -> None:
    nc = replace(
        comprobante,
        cbte_tipo=13,
        cbte_asoc=CbteAsociado(11, 2, 41, CUIT_EMISOR, date(2026, 10, 1)),
    )
    xml = construir_detalle_factura_c(nc, 7)
    asoc = (
        "<ar:CbtesAsoc><ar:CbteAsoc><ar:Tipo>11</ar:Tipo><ar:PtoVta>2</ar:PtoVta>"
        f"<ar:Nro>41</ar:Nro><ar:Cuit>{CUIT_EMISOR}</ar:Cuit><ar:CbteFch>20261001</ar:CbteFch>"
        "</ar:CbteAsoc></ar:CbtesAsoc>"
    )
    assert asoc in xml
    # según el WSDL, CbtesAsoc va después de CondicionIVAReceptorId (la secuencia importa)
    assert xml.index("CondicionIVAReceptorId") < xml.index("CbtesAsoc")
    with pytest.raises(ValueError, match="comprobante que anula"):
        replace(nc, cbte_asoc=None).validar()
