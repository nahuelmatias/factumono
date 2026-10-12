from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from facturitas.arca.ambientes import HOMO
from facturitas.arca.errors import WsaaError
from facturitas.arca.wsaa import Wsaa, construir_tra, firmar_tra, parsear_respuesta
from facturitas.core.models import Credencial
from tests.conftest import fixture

AHORA = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)  # 12:00 en Argentina


def test_tra_tiene_las_ventanas_de_tiempo_correctas() -> None:
    tra = construir_tra("wsfe", AHORA, unique_id=7)
    assert "<uniqueId>7</uniqueId>" in tra
    assert "<generationTime>2026-10-10T11:50:00-03:00</generationTime>" in tra  # −10 min
    assert "<expirationTime>2026-10-10T22:00:00-03:00</expirationTime>" in tra  # +10 h
    assert "<service>wsfe</service>" in tra


def test_cms_lleva_el_contenido_embebido(credencial: Credencial) -> None:
    tra = construir_tra("wsfe", AHORA, unique_id=7)
    cms = firmar_tra(tra, Path(credencial.cert).read_bytes(), Path(credencial.key).read_bytes())
    assert cms[0] == 0x30  # DER: SEQUENCE
    assert tra.encode() in cms  # no es detached


def test_parsear_respuesta_ok() -> None:
    t = parsear_respuesta(fixture("wsaa_ok.xml"))
    assert (t.token, t.sign) == ("TOKEN-FICTICIO", "SIGN-FICTICIO")
    assert t.vencimiento.isoformat().startswith("2026-10-10T22:00:00")


def test_parsear_falla_ya_autenticado() -> None:
    with pytest.raises(WsaaError, match="ya emitió un ticket"):
        parsear_respuesta(fixture("wsaa_ya_autenticado.xml"))


def _wsaa(cred: Credencial, tmp_path: Path) -> Wsaa:
    return Wsaa(cred, HOMO, tmp_path, ahora=lambda: AHORA)


@respx.mock
def test_el_ticket_se_cachea_en_disco(credencial: Credencial, tmp_path: Path) -> None:
    ruta = respx.post(HOMO.wsaa_url).mock(
        return_value=httpx.Response(200, text=fixture("wsaa_ok.xml"))
    )
    primero = _wsaa(credencial, tmp_path).ticket()
    segundo = _wsaa(credencial, tmp_path).ticket()  # otra instancia: lee del disco
    assert primero == segundo
    assert ruta.call_count == 1
    pedido = ruta.calls[0].request
    assert pedido.headers["SOAPAction"] == '""'
    assert b"<wsaa:in0>" in pedido.content


@respx.mock
def test_ticket_vencido_se_vuelve_a_pedir(credencial: Credencial, tmp_path: Path) -> None:
    ruta = respx.post(HOMO.wsaa_url).mock(
        return_value=httpx.Response(200, text=fixture("wsaa_ok.xml"))
    )
    _wsaa(credencial, tmp_path).ticket()
    tarde = Wsaa(credencial, HOMO, tmp_path, ahora=lambda: datetime(2026, 10, 11, 2, tzinfo=UTC))
    tarde.ticket()
    assert ruta.call_count == 2


@respx.mock
def test_fault_500_se_traduce(credencial: Credencial, tmp_path: Path) -> None:
    respx.post(HOMO.wsaa_url).mock(
        return_value=httpx.Response(500, text=fixture("wsaa_ya_autenticado.xml"))
    )
    with pytest.raises(WsaaError, match="no permite pedir otro"):
        _wsaa(credencial, tmp_path).ticket()


@respx.mock
def test_sin_red_da_mensaje_claro(credencial: Credencial, tmp_path: Path) -> None:
    respx.post(HOMO.wsaa_url).mock(side_effect=httpx.ConnectError("sin red"))
    with pytest.raises(WsaaError, match="No se pudo comunicar"):
        _wsaa(credencial, tmp_path).ticket()
