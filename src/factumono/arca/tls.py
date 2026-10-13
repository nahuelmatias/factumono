"""Contexto TLS para hablar con los servidores de ARCA.

Por qué existe: `servicios1.afip.gov.ar` (WSFE de producción) ofrece intercambio DHE con una clave
de 1024 bits. El OpenSSL que trae Python 3.12 lo rechaza (`[SSL: DH_KEY_TOO_SMALL] dh key too
small`) y no se puede conectar. En vez de bajar la seguridad, excluimos los cifrados DH
(`DEFAULT:!DH`): el servidor negocia ECDHE/RSA y el handshake funciona.

Límites: el contexto sale de `ssl.create_default_context()`, o sea que la verificación del
certificado y del hostname **siguen activas**, y solo lo usan los clientes de ARCA (WSAA y WSFE,
en homologación y producción). Nunca se aplica globalmente ni se desactiva `verify`.
Se probó que `DEFAULT:!DH` alcanza; `DEFAULT@SECLEVEL=1` (más permisivo) no hizo falta.
"""

import ssl

import httpx

CIFRADOS = "DEFAULT:!DH"


def contexto_arca() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # CERT_REQUIRED + check_hostname
    ctx.set_ciphers(CIFRADOS)
    return ctx


def cliente_arca(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, verify=contexto_arca())
