from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from factumono.arca.ambientes import HOMO
from factumono.arca.errors import ArcaError, ComprobanteRechazado, EstadoIncierto
from factumono.arca.wsaa import Wsaa
from factumono.arca.wsfe import Wsfe, construir_detalle_factura_c
from factumono.core.models import Comprobante, Concepto, Credencial
from tests.conftest import CUIT_EMISOR, fixture

AHORA = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def wsfe(credencial: Credencial, tmp_path: Path) -> Wsfe:
    with respx.mock:
        respx.post(HOMO.wsaa_url).mock(
            return_value=httpx.Response(200, text=fixture("wsaa_ok.xml"))
        )
        # reloj fijo: el TA grabado vence el 10/10/2026 22:00 (hora argentina)
        wsaa = Wsaa(credencial, HOMO, tmp_path, ahora=lambda: AHORA)
        wsaa.ticket()  # deja el TA en caché
    return Wsfe(CUIT_EMISOR, wsaa, HOMO, tmp_path)


def _responder(fixture_xml: str) -> respx.Route:
    return respx.post(HOMO.wsfe_url).mock(
        return_value=httpx.Response(200, text=fixture(fixture_xml))
    )


@respx.mock
def test_dummy(wsfe: Wsfe) -> None:
    ruta = _responder("fedummy.xml")
    assert wsfe.dummy() == {"AppServer": "OK", "DbServer": "OK", "AuthServer": "OK"}
    pedido = ruta.calls[0].request
    assert pedido.headers["SOAPAction"] == '"http://ar.gov.afip.dif.FEV1/FEDummy"'
    assert b"<ar:Auth>" not in pedido.content  # FEDummy no lleva autenticación


@respx.mock
def test_ultimo_autorizado_manda_auth_con_cuit_representado(wsfe: Wsfe) -> None:
    ruta = _responder("ultimo.xml")
    assert wsfe.ultimo_autorizado(2, 11) == 41
    cuerpo = ruta.calls[0].request.content.decode()
    assert f"<ar:Cuit>{CUIT_EMISOR}</ar:Cuit>" in cuerpo
    assert "<ar:Token>TOKEN-FICTICIO</ar:Token>" in cuerpo
    assert "<ar:PtoVta>2</ar:PtoVta><ar:CbteTipo>11</ar:CbteTipo>" in cuerpo


@respx.mock
def test_puntos_de_venta(wsfe: Wsfe) -> None:
    _responder("ptosventa.xml")
    (pv,) = wsfe.puntos_venta()
    assert (pv.nro, pv.emision_tipo, pv.bloqueado) == (2, "CAE", False)


@respx.mock
def test_sin_puntos_de_venta_no_es_un_error(wsfe: Wsfe) -> None:
    _responder("ptosventa_vacio.xml")
    assert wsfe.puntos_venta() == []


@respx.mock
def test_condiciones_iva_se_cachean(wsfe: Wsfe) -> None:
    ruta = _responder("condiciones.xml")
    assert len(wsfe.condiciones_iva_receptor("C")) == 4
    wsfe.condiciones_iva_receptor("C")
    assert ruta.call_count == 1
    assert "<ar:ClaseCmp>C</ar:ClaseCmp>" in ruta.calls[0].request.content.decode()


@respx.mock
def test_resolver_condicion_iva(wsfe: Wsfe) -> None:
    _responder("condiciones.xml")
    assert wsfe.resolver_condicion_iva("consumidor final").id == 5
    assert wsfe.resolver_condicion_iva("Responsable Inscripto").id == 1
    assert wsfe.resolver_condicion_iva("13").descripcion == "Monotributista Social"
    with pytest.raises(ArcaError, match="ambiguo"):
        wsfe.resolver_condicion_iva("responsable")
    with pytest.raises(ArcaError, match="no coincide"):
        wsfe.resolver_condicion_iva("marciano")


def test_detalle_factura_c(comprobante: Comprobante) -> None:
    xml = construir_detalle_factura_c(comprobante, 42)
    assert "<ar:ImpTotal>1234.56</ar:ImpTotal>" in xml
    assert "<ar:ImpNeto>1234.56</ar:ImpNeto>" in xml  # Factura C: neto = total
    assert "<ar:ImpIVA>0.00</ar:ImpIVA>" in xml
    assert "<ar:Iva>" not in xml
    assert "<ar:CondicionIVAReceptorId>1</ar:CondicionIVAReceptorId>" in xml
    assert "<ar:FchServDesde>20261001</ar:FchServDesde>" in xml
    assert "<ar:FchVtoPago>20261014</ar:FchVtoPago>" in xml
    # el orden de la secuencia del WSDL importa
    orden = ["Concepto", "DocTipo", "ImpTotal", "ImpIVA", "FchServDesde", "MonId", "CondicionIVA"]
    posiciones = [xml.index(f"<ar:{t}") for t in orden]
    assert posiciones == sorted(posiciones)


def test_productos_no_mandan_periodo(comprobante: Comprobante) -> None:
    c = replace(
        comprobante,
        concepto=Concepto.PRODUCTOS,
        periodo_desde=None,
        periodo_hasta=None,
        vto_pago=None,
    )
    assert "FchServDesde" not in construir_detalle_factura_c(c, 1)


def test_solo_factura_c(comprobante: Comprobante) -> None:
    with pytest.raises(ValueError, match="M5"):
        construir_detalle_factura_c(replace(comprobante, cbte_tipo=6), 1)


@respx.mock
def test_emitir_usa_ultimo_mas_uno(wsfe: Wsfe, comprobante: Comprobante) -> None:
    ruta = respx.post(HOMO.wsfe_url).mock(
        side_effect=[
            httpx.Response(200, text=fixture("ultimo.xml")),
            httpx.Response(200, text=fixture("cae_ok.xml")),
        ]
    )
    r = wsfe.emitir(comprobante)
    assert (r.numero, r.cae, r.cae_vto) == (42, "70417054367476", "20261020")
    envio = ruta.calls[1].request.content.decode()
    assert "<ar:CbteDesde>42</ar:CbteDesde><ar:CbteHasta>42</ar:CbteHasta>" in envio


@respx.mock
def test_comprobante_rechazado_traduce_el_error(wsfe: Wsfe, comprobante: Comprobante) -> None:
    respx.post(HOMO.wsfe_url).mock(
        side_effect=[
            httpx.Response(200, text=fixture("ultimo.xml")),
            httpx.Response(200, text=fixture("cae_rechazado.xml")),
        ]
    )
    with pytest.raises(ComprobanteRechazado, match="Indicá la condición del receptor"):
        wsfe.emitir(comprobante)


@respx.mock
def test_corte_de_red_al_pedir_cae_no_reintenta(wsfe: Wsfe, comprobante: Comprobante) -> None:
    ruta = respx.post(HOMO.wsfe_url).mock(
        side_effect=[
            httpx.Response(200, text=fixture("ultimo.xml")),
            httpx.ReadTimeout("timeout"),
        ]
    )
    with pytest.raises(EstadoIncierto, match="NO se sabe"):
        wsfe.emitir(comprobante)
    assert ruta.call_count == 2  # ni un reintento a ciegas
