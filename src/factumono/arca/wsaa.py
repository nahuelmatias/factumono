"""WSAA: autenticación con certificado y caché del ticket de acceso (TA).

Referencia: manual del desarrollador del WSAA (WSAA 1.2.2). El TRA se firma como CMS/PKCS#7 en
DER con el contenido embebido (no detached) y se manda en base64 a `loginCms`.
"""

import base64
import hashlib
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs7

from factumono.arca.ambientes import Ambiente
from factumono.arca.errors import WsaaError, traducir_error_red, traducir_falla_wsaa
from factumono.arca.tls import cliente_arca
from factumono.config import restringir_permisos
from factumono.core.models import Credencial

ZONA_AR = timezone(timedelta(hours=-3))
_MARGEN = timedelta(minutes=5)  # no reutilizar un TA que está por vencer

_SOAP_LOGIN = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov">'
    "<soapenv:Body><wsaa:loginCms><wsaa:in0>{cms}</wsaa:in0></wsaa:loginCms></soapenv:Body>"
    "</soapenv:Envelope>"
)


@dataclass(frozen=True)
class Ticket:
    token: str
    sign: str
    vencimiento: datetime

    def vigente(self, ahora: datetime) -> bool:
        return self.vencimiento - _MARGEN > ahora


def construir_tra(servicio: str, ahora: datetime, unique_id: int | None = None) -> str:
    """TRA: generationTime = ahora − 10 min, expirationTime = ahora + 10 h."""
    ahora = ahora.astimezone(ZONA_AR)
    uid = unique_id if unique_id is not None else int(ahora.timestamp())
    gen = (ahora - timedelta(minutes=10)).isoformat(timespec="seconds")
    exp = (ahora + timedelta(hours=10)).isoformat(timespec="seconds")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<loginTicketRequest version="1.0"><header>'
        f"<uniqueId>{uid}</uniqueId>"
        f"<generationTime>{gen}</generationTime>"
        f"<expirationTime>{exp}</expirationTime>"
        f"</header><service>{escape(servicio)}</service></loginTicketRequest>"
    )


def firmar_tra(tra: str, cert_pem: bytes, key_pem: bytes) -> bytes:
    """CMS en DER con el contenido embebido, firmado con SHA-256."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise WsaaError("La clave privada debe ser RSA o EC.")
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra.encode("utf-8"))
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )


def _sin_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parsear_respuesta(cuerpo: str) -> Ticket:
    """Interpreta la respuesta SOAP de loginCms (éxito o SOAP Fault)."""
    try:
        raiz = _sin_ns(ET.fromstring(cuerpo))
    except ET.ParseError:
        raise WsaaError("ARCA devolvió una respuesta ilegible al pedir el acceso.") from None
    falla = raiz.find(".//faultstring")
    if falla is not None:
        raise WsaaError(traducir_falla_wsaa((falla.text or "").strip()))
    retorno = raiz.find(".//loginCmsReturn")
    if retorno is None or not retorno.text:
        raise WsaaError("La respuesta de acceso de ARCA no trae el ticket.")
    ta = _sin_ns(ET.fromstring(retorno.text))
    token, sign = ta.findtext(".//token"), ta.findtext(".//sign")
    exp = ta.findtext(".//expirationTime")
    if not (token and sign and exp):
        raise WsaaError("El ticket de acceso de ARCA está incompleto.")
    return Ticket(token=token, sign=sign, vencimiento=datetime.fromisoformat(exp))


class Wsaa:
    def __init__(
        self,
        credencial: Credencial,
        ambiente: Ambiente,
        cache_dir: Path,
        cliente: httpx.Client | None = None,
        ahora: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cred = credencial
        self._amb = ambiente
        self._cache = cache_dir
        self._cliente = cliente or cliente_arca(30)
        self._ahora = ahora

    def _archivo_cache(self, servicio: str) -> Path:
        # Un TA por (certificado, servicio, ambiente): la huella identifica el certificado.
        cert = Path(self._cred.cert).read_bytes()
        huella = hashlib.sha256(cert).hexdigest()[:16]
        return self._cache / f"ta-{self._amb.nombre}-{servicio}-{huella}.json"

    def _leer_cache(self, servicio: str) -> Ticket | None:
        ruta = self._archivo_cache(servicio)
        if not ruta.exists():
            return None
        try:
            d = json.loads(ruta.read_text(encoding="utf-8"))
            ticket = Ticket(d["token"], d["sign"], datetime.fromisoformat(d["vencimiento"]))
        except (ValueError, KeyError, OSError):
            return None
        return ticket if ticket.vigente(self._ahora()) else None

    def _guardar_cache(self, servicio: str, ticket: Ticket) -> None:
        ruta = self._archivo_cache(servicio)
        ruta.write_text(
            json.dumps(
                {
                    "token": ticket.token,
                    "sign": ticket.sign,
                    "vencimiento": ticket.vencimiento.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        restringir_permisos(ruta)

    def ticket(self, servicio: str = "wsfe") -> Ticket:
        """Devuelve un TA vigente: del caché si hay uno, o pidiendo uno nuevo a ARCA."""
        cacheado = self._leer_cache(servicio)
        if cacheado:
            return cacheado
        tra = construir_tra(servicio, self._ahora())
        cms = firmar_tra(tra, Path(self._cred.cert).read_bytes(), Path(self._cred.key).read_bytes())
        cuerpo = _SOAP_LOGIN.format(cms=base64.b64encode(cms).decode("ascii"))
        try:
            resp = self._cliente.post(
                self._amb.wsaa_url,
                content=cuerpo.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
            )
        except httpx.HTTPError as e:
            raise WsaaError(f"WSAA: {traducir_error_red(e)}") from None
        # ARCA responde 500 con un SOAP Fault cuando rechaza el pedido: se parsea igual.
        ticket = parsear_respuesta(resp.text)
        self._guardar_cache(servicio, ticket)
        return ticket
