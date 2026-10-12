"""Clave privada y pedido de certificado (CSR) para los web services de ARCA.

Reemplaza a `tools/gen_csr.py`. La clave privada (.key) no sale nunca de la máquina: lo único que
se sube a ARCA es el CSR.
"""

import contextlib
import os
import stat
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.x509.oid import NameOID


def carpeta_certificados(ambiente: str) -> Path:
    """`~/.facturitas/<ambiente>/`, fuera del directorio de datos y del repo."""
    return Path.home() / ".facturitas" / ambiente


def generar_clave_y_csr(
    carpeta: Path, alias: str, cuit: str, nombre: str
) -> tuple[Path, Path, bool]:
    """Crea `<alias>.key` y `<alias>.csr`. Devuelve (key, csr, clave_nueva).

    Si la clave ya existe se **reutiliza** (nunca se pisa: puede haber un certificado emitido para
    ella) y solo se rearma el CSR, que es lo que permite retomar el asistente a mitad de camino.
    """
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_key, ruta_csr = carpeta / f"{alias}.key", carpeta / f"{alias}.csr"
    nueva = not ruta_key.exists()
    if nueva:
        clave: PrivateKeyTypes = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ruta_key.write_bytes(
            clave.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        with contextlib.suppress(OSError):
            os.chmod(ruta_key, stat.S_IRUSR | stat.S_IWUSR)
    else:
        clave = serialization.load_pem_private_key(ruta_key.read_bytes(), password=None)
    assert isinstance(clave, rsa.RSAPrivateKey)
    sujeto = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, nombre),
            x509.NameAttribute(NameOID.COMMON_NAME, alias),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
        ]
    )
    csr = x509.CertificateSigningRequestBuilder().subject_name(sujeto).sign(clave, hashes.SHA256())
    ruta_csr.write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    return ruta_key, ruta_csr, nueva


def cuit_del_certificado(cert_pem: bytes) -> str | None:
    """El CUIT que figura en el serialNumber del certificado («CUIT 20...»), si está."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    for atributo in cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER):
        valor = str(atributo.value)
        digitos = "".join(c for c in valor if c.isdigit())
        if len(digitos) == 11:
            return digitos
    return None
