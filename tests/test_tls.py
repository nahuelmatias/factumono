"""El cliente de ARCA usa un contexto TLS sin DH, con verificación activa."""

import ssl

import httpx

from factumono.arca import ambientes
from factumono.arca.errors import traducir_error_red
from factumono.arca.tls import CIFRADOS, cliente_arca, contexto_arca
from factumono.arca.wsaa import Wsaa
from factumono.arca.wsfe import Wsfe
from factumono.core.models import Credencial


def _sin_dhe(ctx: ssl.SSLContext) -> bool:
    """Sin DHE con certificado (los DHE-PSK exigen clave precompartida y no se usan con ARCA)."""
    return not any(
        c["name"].startswith("DHE-") and "PSK" not in c["name"] for c in ctx.get_ciphers()
    )


def test_contexto_verifica_certificado_y_hostname_y_excluye_dh() -> None:
    ctx = contexto_arca()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert CIFRADOS == "DEFAULT:!DH"
    assert _sin_dhe(ctx)


def test_el_contexto_no_es_global() -> None:
    contexto_arca()
    assert not _sin_dhe(ssl.create_default_context())  # el default global sigue intacto


def test_cliente_arca_usa_ese_contexto(credencial: Credencial, tmp_path) -> None:  # type: ignore[no-untyped-def]
    c = cliente_arca(10)
    assert isinstance(c, httpx.Client)
    pool = c._transport._pool  # type: ignore[attr-defined]
    ctx = pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert _sin_dhe(ctx)


def test_wsaa_y_wsfe_por_defecto_usan_el_cliente_de_arca(credencial: Credencial, tmp_path) -> None:  # type: ignore[no-untyped-def]
    for amb in (ambientes.HOMO, ambientes.PROD):
        wsaa = Wsaa(credencial, amb, tmp_path)
        wsfe = Wsfe("20000000001", wsaa, amb, tmp_path)
        for cliente in (wsaa._cliente, wsfe._cliente):
            ctx = cliente._transport._pool._ssl_context  # type: ignore[attr-defined]
            assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
            assert _sin_dhe(ctx)


def test_traduccion_de_errores_ssl() -> None:
    dh = traducir_error_red(httpx.ConnectError("[SSL: DH_KEY_TOO_SMALL] dh key too small"))
    assert "Diffie-Hellman" in dh and "DH_KEY_TOO_SMALL" in dh
    cert = traducir_error_red(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] x"))
    assert "fecha y hora" in cert and "no desactiva" in cert
    assert "Revisá la conexión" in traducir_error_red(httpx.ConnectError("timeout"))
