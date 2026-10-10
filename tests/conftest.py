"""Fixtures compartidas. Todos los datos son ficticios (nada de CUITs ni claves reales)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from facturitas.core.models import (
    FACTURA_C,
    Comprobante,
    Concepto,
    Credencial,
    DocTipo,
    Item,
    Perfil,
    Receptor,
)

FIXTURES = Path(__file__).parent / "fixtures"
CUIT_EMISOR = "20000000001"  # ficticio, sale de la especificación oficial del QR
CUIT_RECEPTOR = "30000000007"  # ficticio, ídem


def fixture(nombre: str) -> str:
    return (FIXTURES / nombre).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def home_aislado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Cada test usa su propio directorio de datos: jamás toca el del usuario."""
    home = tmp_path / "home"
    monkeypatch.setenv("FACTURITAS_HOME", str(home))
    yield home


@pytest.fixture
def credencial(tmp_path: Path) -> Credencial:
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ficticio")])
    ahora = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(nombre)
        .issuer_name(nombre)
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - timedelta(days=1))
        .not_valid_after(ahora + timedelta(days=30))
        .sign(clave, hashes.SHA256())
    )
    ruta_cert, ruta_key = tmp_path / "t.crt", tmp_path / "t.key"
    ruta_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ruta_key.write_bytes(
        clave.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return Credencial(cuit=CUIT_EMISOR, cert=str(ruta_cert), key=str(ruta_key))


@pytest.fixture
def perfil() -> Perfil:
    return Perfil(
        cuit=CUIT_EMISOR,
        razon_social="EMISOR FICTICIO",
        condicion_iva="Responsable Monotributo",
        domicilio="Calle Falsa 123 - Ciudad de Buenos Aires",
        iibb="Exento",
        inicio_actividades=date(2020, 1, 1),
        punto_venta=2,
    )


@pytest.fixture
def comprobante(perfil: Perfil) -> Comprobante:
    return Comprobante(
        perfil=perfil,
        punto_venta=2,
        cbte_tipo=FACTURA_C,
        concepto=Concepto.SERVICIOS,
        receptor=Receptor(
            doc_tipo=int(DocTipo.CUIT),
            doc_nro=int(CUIT_RECEPTOR),
            nombre="RECEPTOR FICTICIO SRL",
            condicion_iva_id=1,
            condicion_iva="IVA Responsable Inscripto",
        ),
        items=(
            Item(
                descripcion="Servicios de prueba",
                cantidad=Decimal(1),
                precio_unitario=Decimal("1234.56"),
                unidad="otras unidades",
            ),
        ),
        fecha=date(2026, 10, 10),
        periodo_desde=date(2026, 10, 1),
        periodo_hasta=date(2026, 10, 31),
        vto_pago=date(2026, 10, 14),
    )
