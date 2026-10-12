"""Asistente `facturitas init`: flujo completo, retomar, validaciones y producción."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from facturitas import config
from facturitas.arca import csr as csr_mod
from facturitas.cli import app as cli
from facturitas.cli import asistente as asis
from facturitas.cli.asistente import Asistente, limpiar_ruta, validar_certificado

CUIT = "20000000001"  # ficticio

Respuesta = str | Callable[[], str]


class Guion:
    """Responde las preguntas en orden; una respuesta puede ser una función (arma archivos)."""

    def __init__(self, *respuestas: Respuesta) -> None:
        self.cola = list(respuestas)
        self.salida: list[str] = []
        self.abiertas: list[str] = []
        self.preguntas: list[str] = []

    def _siguiente(self, texto: str) -> str:
        self.preguntas.append(texto)
        assert self.cola, f"El asistente preguntó algo no previsto: {texto!r}"
        r = self.cola.pop(0)
        return r() if callable(r) else r

    def preguntar(self, texto: str, defecto: str | None = None) -> str:
        r = self._siguiente(texto)
        return r if r != "" or defecto is None else defecto

    def confirmar(self, texto: str, defecto: bool = True) -> bool:
        r = self._siguiente(texto)
        return defecto if r == "" else r.lower() in ("s", "si", "sí")

    def mostrar(self, texto: str = "") -> None:
        self.salida.append(texto)

    def abrir(self, url: str) -> None:
        self.abiertas.append(url)

    @property
    def texto(self) -> str:
        return "\n".join(self.salida)


@pytest.fixture(autouse=True)
def carpeta_certificados(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "certs"
    monkeypatch.setattr(asis, "carpeta_certificados", lambda ambiente: base / ambiente)
    return base


def emitir_crt(
    ambiente: str, destino: Path, carpeta: Path, alias: str | None = None
) -> Callable[[], str]:
    """Simula a ARCA: firma un certificado para la clave que generó el asistente."""

    def hacer() -> str:
        key_path = next((carpeta / ambiente).glob("*.key"))
        clave = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(clave, rsa.RSAPrivateKey)
        nombre = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, alias or "alias"),
                x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {CUIT}"),
            ]
        )
        ahora = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(nombre)
            .issuer_name(nombre)
            .public_key(clave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(ahora - timedelta(days=1))
            .not_valid_after(ahora + timedelta(days=365))
            .sign(clave, hashes.SHA256())
        )
        destino.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return f'"{destino}"'  # como queda al arrastrar el archivo a la consola

    return hacer


DATOS_HOMO = ("1", CUIT, "EMISOR FICTICIO", "1", "Calle Falsa 123", "Exento", "", "", "")
# ambiente · CUIT · razón social · condición · domicilio · IIBB · fantasía · inicio · alias


def correr(
    io: Guion, ok: bool = True, ambiente: str | None = None
) -> tuple[bool, list[tuple[str, str]]]:
    llamadas: list[tuple[str, str]] = []

    def verificar(perfil: str, amb: str) -> bool:
        llamadas.append((perfil, amb))
        return ok

    return Asistente(io, verificar).correr(ambiente), llamadas


def test_flujo_completo_en_homologacion(tmp_path: Path, carpeta_certificados: Path) -> None:
    io = Guion(
        *DATOS_HOMO,
        "",  # presionar Enter tras los trámites
        emitir_crt("homo", tmp_path / "descargado.crt", carpeta_certificados),
        "",  # punto de venta (1)
    )
    # ¿Abro ARCA? va antes del Enter: se responde con la cola, así que lo intercalamos
    io.cola.insert(len(DATOS_HOMO), "s")
    ok, llamadas = correr(io)
    assert ok and llamadas == [("default", "homo")]
    cfg = config.cargar()
    p = cfg.perfil()
    assert (p.cuit, p.razon_social, p.condicion_iva, p.punto_venta) == (
        CUIT,
        "EMISOR FICTICIO",
        "Responsable Monotributo",
        1,
    )
    cred = cfg.credencial_de(p)
    assert cred.ambiente == "homo" and Path(cred.key).is_file() and Path(cred.cert).is_file()
    assert Path(cred.cert).parent == carpeta_certificados / "homo"  # copiado junto a la clave
    assert io.abiertas and "auth.afip.gob.ar" in io.abiertas[0].replace("wsass-homo.", "auth.")
    assert "Paso 1 de 6" in io.texto and "Paso 6 de 6" in io.texto and "¡Todo listo!" in io.texto
    assert "BEGIN CERTIFICATE REQUEST" in io.texto  # muestra el CSR para pegar en ARCA
    assert "WSASS" in io.texto
    assert asis.cargar_avance("homo") is None  # terminado: no queda nada a medias


def test_se_puede_cortar_y_retomar(tmp_path: Path, carpeta_certificados: Path) -> None:
    primero = Guion(*DATOS_HOMO, "n", "", "salir")  # no abrir ARCA, Enter, y sale al pedir el .crt
    ok, _ = correr(primero)
    assert not ok and "Volvé a correr" in primero.texto
    avance = asis.cargar_avance("homo")
    assert avance and avance.paso == 3 and avance.cuit == CUIT
    key = Path(avance.key)
    clave_original = key.read_bytes()

    segundo = Guion(
        "1",  # ambiente
        "s",  # ¿seguir desde ahí?
        emitir_crt("homo", tmp_path / "x.crt", carpeta_certificados),
        "",  # punto de venta
    )
    ok, llamadas = correr(segundo)
    assert ok and llamadas == [("default", "homo")]
    assert key.read_bytes() == clave_original  # la clave NO se regeneró
    assert "Paso 1 de 6" not in segundo.texto and "Paso 4 de 6" in segundo.texto
    assert not any("CUIT" in q for q in segundo.preguntas)  # no vuelve a pedir los datos


def test_no_retomar_empieza_de_cero(carpeta_certificados: Path) -> None:
    correr(Guion(*DATOS_HOMO, "n", "", "salir"))
    nuevo = Guion("1", "n", CUIT, "OTRO", "2", "Calle 1", "", "", "", "alias2", "n", "", "salir")
    # "n" a retomar -> pide los datos otra vez
    correr(nuevo)
    assert "Paso 1 de 6" in nuevo.texto


def test_rechaza_certificado_de_otra_clave_y_pide_de_nuevo(
    tmp_path: Path, carpeta_certificados: Path
) -> None:
    ajeno = tmp_path / "ajeno.crt"
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "otro")])
    ahora = datetime.now(UTC)
    ajeno.write_bytes(
        x509.CertificateBuilder()
        .subject_name(n)
        .issuer_name(n)
        .public_key(clave.public_key())
        .serial_number(1)
        .not_valid_before(ahora - timedelta(days=1))
        .not_valid_after(ahora + timedelta(days=30))
        .sign(clave, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    io = Guion(
        *DATOS_HOMO,
        "n",
        "",
        str(ajeno),  # certificado equivocado
        str(tmp_path / "no-existe.crt"),
        str(tmp_path / "basura.txt"),
        emitir_crt("homo", tmp_path / "bueno.crt", carpeta_certificados),
        "",
    )
    (tmp_path / "basura.txt").write_text("no soy un certificado")
    ok, _ = correr(io)
    assert ok
    assert "NO corresponde a la clave" in io.texto
    assert "No encuentro el archivo" in io.texto
    assert "no parece un certificado" in io.texto


def test_cuit_invalido_se_vuelve_a_pedir(carpeta_certificados: Path, tmp_path: Path) -> None:
    io = Guion("1", "20123456780", CUIT, "NOMBRE", "1", "Calle 1", "", "", "", "", "n", "", "salir")
    # alias queda en blanco -> usa el sugerido; luego corta en "Enter" de los trámites
    correr(io)
    assert "dígito verificador" in io.texto.lower() or "inválid" in io.texto.lower()
    av = asis.cargar_avance("homo")
    assert av and av.cuit == CUIT


def test_si_la_prueba_falla_queda_guardado_y_ofrece_reintentar(
    tmp_path: Path, carpeta_certificados: Path
) -> None:
    io = Guion(
        *DATOS_HOMO,
        "n",
        "",
        emitir_crt("homo", tmp_path / "c.crt", carpeta_certificados),
        "",
        "n",  # no reintentar
    )
    ok, _ = correr(io, ok=False)
    assert not ok and "Algo no está en orden" in io.texto
    assert config.cargar().perfil().cuit == CUIT  # la config quedó guardada igual
    assert asis.cargar_avance("homo") is not None  # y el asistente sigue retomable


def test_certificado_de_otro_cuit_pide_confirmar(
    tmp_path: Path, carpeta_certificados: Path
) -> None:
    io = Guion(
        "1",
        "30000000007",  # el CUIT cargado no es el del certificado (20000000001)
        *DATOS_HOMO[2:],
        "n",
        "",
        emitir_crt("homo", tmp_path / "c.crt", carpeta_certificados),
        "n",  # «¿es a propósito?» -> no: vuelve a pedir el archivo
        "salir",
    )
    correr(io)
    assert "distinto del que cargaste" in io.texto


def test_produccion_sin_perfil_empieza_por_homologacion(carpeta_certificados: Path) -> None:
    io = Guion(*DATOS_HOMO[1:], "n", "", "salir")
    correr(io, ambiente="prod")
    assert "primero hay que dejar andando homologación" in io.texto
    assert asis.cargar_avance("homo") is not None and asis.cargar_avance("prod") is None


def test_produccion_con_perfil_guarda_credencial_y_punto_de_venta_sin_activar(
    tmp_path: Path, carpeta_certificados: Path
) -> None:
    ok, _ = correr(
        Guion(
            *DATOS_HOMO,
            "n",
            "",
            emitir_crt("homo", tmp_path / "h.crt", carpeta_certificados),
            "",
        )
    )
    assert ok
    io = Guion(
        "",  # alias sugerido (facturitas-prod)
        "n",
        "",
        emitir_crt("prod", tmp_path / "p.crt", carpeta_certificados),
        "2",  # punto de venta real
    )
    ok, llamadas = correr(io, ambiente="prod")
    assert ok and llamadas == [("default", "prod")]
    cfg = config.cargar()
    p = cfg.perfil()
    assert (p.credencial_prod, p.punto_venta_prod, p.punto_venta) == ("default-prod", 2, 1)
    assert cfg.credenciales["default-prod"].ambiente == "prod"
    assert cfg.ambiente == "homo"  # NO activó producción
    assert "Administración de Certificados Digitales" in io.texto
    assert "punto de venta" in io.texto.lower() and "NO queda activa" in io.texto
    assert not any(q.startswith("Tu CUIT") for q in io.preguntas)  # reutiliza el perfil


# --- piezas sueltas --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        '"C:\\Users\\yo\\a.crt"',
        "& 'C:\\Users\\yo\\a.crt'",
        "  C:\\Users\\yo\\a.crt  ",
        "'C:\\Users\\yo\\a.crt'",
    ],
)
def test_limpiar_ruta_arrastrada(texto: str) -> None:
    assert limpiar_ruta(texto) == Path("C:\\Users\\yo\\a.crt")


def test_csr_reutiliza_la_clave_y_trae_el_cuit(tmp_path: Path) -> None:
    key, csr, nueva = csr_mod.generar_clave_y_csr(tmp_path, "a", CUIT, "NOMBRE")
    assert nueva
    original = key.read_bytes()
    _, csr2, nueva2 = csr_mod.generar_clave_y_csr(tmp_path, "a", CUIT, "NOMBRE")
    assert not nueva2 and key.read_bytes() == original and csr2 == csr
    pedido = x509.load_pem_x509_csr(csr.read_bytes())
    assert pedido.is_signature_valid
    assert f"CUIT {CUIT}" in pedido.subject.rfc4514_string()


def test_validar_certificado_vencido(tmp_path: Path) -> None:
    key, _, _ = csr_mod.generar_clave_y_csr(tmp_path, "v", CUIT, "N")
    clave = serialization.load_pem_private_key(key.read_bytes(), password=None)
    assert isinstance(clave, rsa.RSAPrivateKey)
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "v")])
    ahora = datetime.now(UTC)
    crt = tmp_path / "v.crt"
    crt.write_bytes(
        x509.CertificateBuilder()
        .subject_name(n)
        .issuer_name(n)
        .public_key(clave.public_key())
        .serial_number(1)
        .not_valid_before(ahora - timedelta(days=30))
        .not_valid_after(ahora - timedelta(days=1))
        .sign(clave, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    with pytest.raises(ValueError, match="vencido"):
        validar_certificado(crt, key)


def test_init_sin_consola_interactiva_explica_que_use_las_opciones() -> None:
    r = CliRunner().invoke(cli.app, ["init"])  # CliRunner no es una TTY
    assert r.exit_code == 1 and "interactivo" in r.output and "--cuit" in r.output
