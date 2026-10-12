"""La GUI web: seguridad, flujo preparar → emitir, facturas, clientes, ajustes y asistente."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from facturitas import config
from facturitas.app.server import COOKIE, crear_app
from facturitas.app.servicios import CONSUMIDOR_FINAL, Servicios
from facturitas.cli import asistente as asis
from facturitas.contexto import Contexto
from facturitas.core import topes
from facturitas.core.models import Cliente, Credencial, Perfil
from facturitas.storage.historial import Historial
from tests.test_cli import WsaaFalso, WsfeFalso

TOKEN = "token-de-prueba"
AHORA = datetime(2026, 10, 12, 15, 0)
CUIT = "20000000001"


@dataclass
class Mundo:
    c: TestClient
    arca: WsfeFalso
    ctx: Contexto
    svc: Servicios


@pytest.fixture
def mundo(perfil: Perfil, credencial: Credencial, tmp_path: Path) -> Mundo:
    """Config guardada, un ARCA falso y un cliente HTTP ya autenticado."""
    perfil = replace(perfil, categoria_monotributo="C")
    config.guardar(
        config.Config(
            {"default": credencial}, {"default": perfil}, carpeta_pdf=str(tmp_path / "pdf")
        )
    )
    arca = WsfeFalso()
    ctx = Contexto(
        perfil, credencial, WsaaFalso(), arca,  # type: ignore[arg-type]
        "HOMOLOGACIÓN (pruebas)", perfil_nombre="default", ambiente="homo", historial=Historial(),
    )  # fmt: skip
    ctx.clientes.guardar(
        "default",
        Cliente(
            "acciona", "ACCIONA IT S.R.L", 80, 30710581467, "IVA Responsable Inscripto",
            "Moreau De Justo 550", "pagos@acciona.test", "Servicios de consultoría",
            "otras unidades", "servicios", 0,
        ),
    )  # fmt: skip
    svc = Servicios(lambda nombre, ambiente: ctx, lambda: AHORA)
    cliente = TestClient(crear_app(svc, TOKEN), follow_redirects=False)
    cliente.cookies.set(COOKIE, TOKEN)
    return Mundo(cliente, arca, ctx, svc)


def _con_produccion() -> config.Config:
    """La config de `mundo` más un certificado y un punto de venta de producción."""
    cfg = config.cargar()
    cred = cfg.credenciales["default"]
    cfg.credenciales["default-prod"] = replace(cred, ambiente="prod")
    cfg.perfiles["default"] = replace(
        cfg.perfil(), credencial_prod="default-prod", punto_venta_prod=2
    )
    return cfg


# --- seguridad ---------------------------------------------------------------------------------


def test_sin_cookie_no_entra_nadie(mundo: Mundo) -> None:
    otro = TestClient(crear_app(mundo.svc, TOKEN))
    assert otro.get("/").status_code == 403
    assert otro.post("/nueva/emitir", data={"borrador_id": "x"}).status_code == 403
    otro.cookies.set(COOKIE, "otro-token")
    assert otro.get("/facturas").status_code == 403


def test_el_enlace_de_arranque_entrega_la_cookie_y_el_token_malo_no(mundo: Mundo) -> None:
    c = TestClient(crear_app(mundo.svc, TOKEN), follow_redirects=False)
    r = c.get(f"/?t={TOKEN}")
    assert r.status_code == 303 and COOKIE in r.headers["set-cookie"]
    assert (
        "httponly" in r.headers["set-cookie"].lower()
        and "samesite=strict" in r.headers["set-cookie"].lower()
    )
    assert c.get("/?t=malo").status_code == 403
    assert c.get("/").status_code in (200, 303)  # ya con la cookie


def test_rechaza_otro_host_contra_dns_rebinding(mundo: Mundo) -> None:
    r = mundo.c.get("/", headers={"host": "malo.example.com"})
    assert r.status_code == 400


def test_un_post_desde_otro_origen_se_rechaza(mundo: Mundo) -> None:
    r = mundo.c.post(
        "/nueva/emitir", data={"borrador_id": "x"}, headers={"origin": "https://malo.example"}
    )
    assert r.status_code == 403
    assert mundo.arca.autorizados == {}


def test_no_hay_documentacion_automatica_expuesta(mundo: Mundo) -> None:
    assert mundo.c.get("/docs").status_code in (404, 405)
    assert mundo.c.get("/openapi.json").status_code == 404


def test_el_servidor_real_escucha_solo_en_localhost() -> None:
    from facturitas.app.ventana import ServidorLocal

    srv = ServidorLocal()
    assert srv.servidor.config.host == "127.0.0.1"
    assert srv.url.startswith("http://127.0.0.1:") and "?t=" in srv.url


# --- inicio, bienvenida y tope ----------------------------------------------------------------


def test_sin_configurar_lleva_a_la_bienvenida(tmp_path: Path) -> None:
    svc = Servicios(lambda n, a: (_ for _ in ()).throw(config.ConfigError("x")), lambda: AHORA)
    c = TestClient(crear_app(svc, TOKEN), follow_redirects=False)
    c.cookies.set(COOKIE, TOKEN)
    assert c.get("/").headers["location"] == "/bienvenida"
    r = c.get("/bienvenida")
    assert "Si sos monotributista, no te vuelvas mono con las facturas 🐒" in r.text
    assert "HOMOLOGACIÓN" in r.text  # el ambiente se ve siempre, aun sin perfil


def test_inicio_muestra_ambiente_resumen_y_la_barra_de_tope(mundo: Mundo) -> None:
    r = mundo.c.get("/")
    assert r.status_code == 200
    assert "🧪 HOMOLOGACIÓN" in r.text and "EMISOR FICTICIO" in r.text
    assert "categoría C" in r.text and "Tu tope de monotributo" in r.text
    assert "afip.gob.ar/monotributo/categorias.asp" in r.text  # la fuente
    assert "desde el 01/08/2026" in r.text


def test_la_barra_de_tope_calcula_el_porcentaje_con_facturas_y_notas_de_credito(
    mundo: Mundo,
) -> None:
    t = topes.cargar()
    mitad = (t.tope("C") / 2).quantize(Decimal("0.01"))
    p = mundo.c.post(
        "/nueva/preparar",
        data={"cliente": "acciona", "importe": f"{mitad}".replace(".", ","), "periodo": "2026-09"},
    )
    assert p.status_code == 200
    bid = p.text.split('name="borrador_id" value="')[1].split('"')[0]
    mundo.c.post("/nueva/emitir", data={"borrador_id": bid})
    barra = mundo.svc.barra_tope()
    assert barra and barra.porcentaje == 50 and barra.nivel == "ok" and barra.categoria == "C"
    assert "50,0" in barra.porcentaje_real


def test_niveles_de_la_barra() -> None:
    t = topes.cargar()
    desde, hasta = AHORA.date(), AHORA.date()
    tope = t.tope("A")
    niveles = {
        Decimal("0.5"): "ok", Decimal("0.8"): "atencion", Decimal("0.95"): "peligro",
        Decimal("1.2"): "excedido",
    }  # fmt: skip
    for fraccion, esperado in niveles.items():
        assert topes.avance(t, "a", tope * fraccion, desde, hasta).nivel == esperado


def test_topes_se_leen_de_un_archivo_actualizable_con_vigencia_y_fuente(tmp_path: Path) -> None:
    incluido = topes.cargar()
    assert incluido.origen == "incluido" and incluido.fuente.startswith("https://www.afip.gob.ar")
    assert incluido.tope("K") > incluido.tope("A") and incluido.vigencia_desde.year >= 2026
    propio = config.directorio_datos() / topes.NOMBRE_ARCHIVO
    propio.write_text(
        '{"vigencia_desde": "2027-02-01", "fuente": "x", "categorias": {"A": "1000"}}',
        encoding="utf-8",
    )
    actual = topes.cargar()
    assert actual.origen == str(propio) and actual.tope("a") == Decimal("1000")
    with pytest.raises(topes.TopesError, match="No conozco la categoría"):
        actual.tope("Z")
    propio.write_text("no es json", encoding="utf-8")
    with pytest.raises(topes.TopesError):
        topes.cargar()


def test_los_topes_viejos_avisan() -> None:
    t = topes.cargar()
    assert not t.desactualizados(t.vigencia_desde)
    assert t.desactualizados(t.vigencia_desde + timedelta(days=200))


# --- nueva factura -----------------------------------------------------------------------------


def _preparar(mundo: Mundo, **datos: str) -> Any:
    base = {"cliente": "acciona", "importe": "1.093.843,20", "periodo": "2026-09"}
    return mundo.c.post("/nueva/preparar", data={**base, **datos})


def _borrador(html: str) -> str:
    return html.split('name="borrador_id" value="')[1].split('"')[0]


def test_preparar_muestra_el_detalle_y_no_emite_nada(mundo: Mundo) -> None:
    r = _preparar(mundo)
    assert r.status_code == 200
    assert "ACCIONA IT S.R.L" in r.text and "Servicios de consultoría" in r.text
    assert "$ 1.093.843,20" in r.text and "01/09/2026 al 30/09/2026" in r.text
    assert "otras unidades" in r.text  # el detalle de ítems se ve en el preview (y en el PDF)
    assert "sin ítems" in r.text  # y se aclara que el QR/ARCA no los llevan
    assert "Emitir (prueba)" in r.text
    assert mundo.arca.autorizados == {}


def test_emitir_confirma_genera_el_pdf_y_se_puede_ver(mundo: Mundo, tmp_path: Path) -> None:
    bid = _borrador(_preparar(mundo).text)
    r = mundo.c.post("/nueva/emitir", data={"borrador_id": bid})
    assert "autorizada" in r.text and "70000000000001" in r.text
    assert "pagos@acciona.test" in r.text  # enlace de mail para mandarla
    assert len(mundo.arca.autorizados) == 1
    pdfs = list((tmp_path / "pdf" / "default" / "homologacion").glob("*.pdf"))
    assert len(pdfs) == 1
    fila = mundo.svc.listar()[0]
    pdf = mundo.c.get(f"/facturas/{fila.id}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_emitir_dos_veces_el_mismo_borrador_no_duplica(mundo: Mundo) -> None:
    bid = _borrador(_preparar(mundo).text)
    mundo.c.post("/nueva/emitir", data={"borrador_id": bid})
    r = mundo.c.post("/nueva/emitir", data={"borrador_id": bid})
    assert "Ya estaba emitida" in r.text
    assert len(mundo.arca.autorizados) == 1


def test_no_se_emite_sin_borrador_ni_vencido(mundo: Mundo) -> None:
    r = mundo.c.post(
        "/nueva/emitir", data={"borrador_id": "B-NOEXISTE"}, headers={"hx-request": "true"}
    )
    assert "ya no existe" in r.text and mundo.arca.autorizados == {}
    bid = _borrador(_preparar(mundo).text)
    futuro = Servicios(lambda n, a: mundo.ctx, lambda: AHORA + timedelta(minutes=16))
    c2 = TestClient(crear_app(futuro, TOKEN))
    c2.cookies.set(COOKIE, TOKEN)
    r = c2.post("/nueva/emitir", data={"borrador_id": bid}, headers={"hx-request": "true"})
    assert "venció" in r.text and mundo.arca.autorizados == {}


def test_la_unica_forma_de_emitir_es_el_borrador() -> None:
    import inspect

    from facturitas.app import server, servicios

    assert inspect.getsource(servicios.Servicios).count("service.emitir(") == 1
    assert "service.emitir" not in inspect.getsource(server)


def test_un_parecido_es_solo_informativo(mundo: Mundo) -> None:
    mundo.c.post("/nueva/emitir", data={"borrador_id": _borrador(_preparar(mundo).text)})
    r = _preparar(mundo)
    assert "ya emitiste" in r.text and "Es solo un aviso" in r.text
    assert "otra vez" not in r.text  # sin casilla de confirmación extra
    assert (
        "autorizada" in mundo.c.post("/nueva/emitir", data={"borrador_id": _borrador(r.text)}).text
    )
    assert len(mundo.arca.autorizados) == 2


def test_la_de_siempre_repite_la_ultima_con_el_periodo_corrido(mundo: Mundo) -> None:
    mundo.c.post(
        "/nueva/emitir", data={"borrador_id": _borrador(_preparar(mundo, importe="1000").text)}
    )
    r = mundo.c.post("/nueva/preparar", data={"cliente": "acciona", "repetir": "1"})
    assert "Es la de siempre" in r.text and "01/10/2026 al 31/10/2026" in r.text
    assert "$ 1.000,00" in r.text
    assert "repetir=1" in mundo.c.get("/clientes").text  # el botón existe en Clientes
    assert 'name="repetir"' in mundo.c.get("/nueva?cliente=acciona&repetir=1").text


def test_consumidor_final_pide_importe_y_detalle(mundo: Mundo) -> None:
    r = mundo.c.post("/nueva/preparar", data={"cliente": CONSUMIDOR_FINAL, "importe": "500"})
    assert "Falta el importe y qué se vendió" in r.text
    r = mundo.c.post(
        "/nueva/preparar",
        data={
            "cliente": CONSUMIDOR_FINAL,
            "importe": "500",
            "descripcion": "Pan",
            "concepto": "productos",
        },
    )
    assert "Consumidor Final" in r.text and "$ 500,00" in r.text


def test_errores_de_carga_son_claros(mundo: Mundo) -> None:
    assert "El importe no es válido" in _preparar(mundo, importe="abc").text
    assert "Elegí un cliente" in _preparar(mundo, cliente="nadie").text
    assert "Falta el importe" in _preparar(mundo, importe="").text


def test_en_produccion_todo_lo_dice_y_el_boton_es_otro(mundo: Mundo) -> None:
    mundo.ctx.ambiente = "prod"
    cfg = _con_produccion()
    cfg.ambiente = "prod"
    config.guardar(cfg)
    for ruta in (
        "/",
        "/nueva",
        "/facturas",
        "/clientes",
        "/ajustes",
        "/ia",
        "/acerca",
        "/bienvenida",
    ):
        assert "⚠️ PRODUCCIÓN — comprobante con validez fiscal" in mundo.c.get(ruta).text, ruta
    r = _preparar(mundo)
    assert "Emitir con validez fiscal" in r.text and "Emitir (prueba)" not in r.text


# --- mis facturas y notas de crédito ---------------------------------------------------------


def test_listado_busqueda_y_anulacion(mundo: Mundo) -> None:
    mundo.c.post("/nueva/emitir", data={"borrador_id": _borrador(_preparar(mundo).text)})
    fila = mundo.svc.listar()[0]
    lista = mundo.c.get("/facturas")
    assert "ACCIONA IT S.R.L" in lista.text and "prueba" in lista.text
    assert "ACCIONA" in mundo.c.get("/facturas?q=accio", headers={"hx-request": "true"}).text
    assert (
        "No hay comprobantes" in mundo.c.get("/facturas?q=zzz", headers={"hx-request": "true"}).text
    )
    assert "Anular una factura" in mundo.c.get(f"/facturas/{fila.id}/anular").text
    r = mundo.c.post(f"/facturas/{fila.id}/anular/preparar", data={"motivo": "Error"})
    assert "Nota de Crédito C" in r.text and "Anula la" in r.text and "Error" in r.text
    assert mundo.arca.autorizados[1].cbte_tipo == 11 and len(mundo.arca.autorizados) == 1
    mundo.c.post("/nueva/emitir", data={"borrador_id": _borrador(r.text)})
    assert mundo.arca.autorizados[2].cbte_tipo == 13
    assert "anulada" in mundo.c.get("/facturas").text
    # anularla de nuevo exige la casilla
    otra = mundo.c.post(f"/facturas/{fila.id}/anular/preparar", data={"motivo": "otra vez"})
    assert "YA FUE ANULADA" in otra.text and "confirmo_anulada" in otra.text
    sin = mundo.c.post(
        "/nueva/emitir", data={"borrador_id": _borrador(otra.text)}, headers={"hx-request": "true"}
    )
    assert "ya estaba anulada" in sin.text and len(mundo.arca.autorizados) == 2


def test_el_motivo_es_obligatorio(mundo: Mundo) -> None:
    mundo.c.post("/nueva/emitir", data={"borrador_id": _borrador(_preparar(mundo).text)})
    fila = mundo.svc.listar()[0]
    assert (
        "Contá el motivo"
        in mundo.c.post(f"/facturas/{fila.id}/anular/preparar", data={"motivo": " "}).text
    )


# --- clientes ----------------------------------------------------------------------------------


def test_clientes_alta_edicion_y_baja(mundo: Mundo) -> None:
    nuevo = {
        "nombre": "MARTIN SEBASTIAN PABLO", "alias": "seba", "documento": "20-35426727-7",
        "condicion_iva": "Consumidor Final", "descripcion": "Servicios informáticos",
        "vto_dias": "4",
    }  # fmt: skip
    r = mundo.c.post("/clientes/guardar", data=nuevo)
    assert r.status_code == 303
    assert "MARTIN SEBASTIAN PABLO" in mundo.c.get("/clientes").text
    ed = mundo.c.post("/clientes/guardar", data={**nuevo, "editando": "1", "email": "s@s.test"})
    assert ed.status_code == 303 and "s@s.test" in mundo.c.get("/clientes/seba/editar").text
    assert "ya existe" in mundo.c.post("/clientes/guardar", data=nuevo).text.lower()
    assert mundo.c.post("/clientes/seba/borrar").status_code == 303
    assert "MARTIN SEBASTIAN" not in mundo.c.get("/clientes").text


def test_cliente_con_datos_invalidos(mundo: Mundo) -> None:
    base = {"nombre": "X", "condicion_iva": "Consumidor Final"}
    assert (
        "CUIT (11 números) o un DNI"
        in mundo.c.post("/clientes/guardar", data={**base, "documento": "12"}).text
    )
    assert (
        "verificador"
        in mundo.c.post("/clientes/guardar", data={**base, "documento": "30000000008"}).text.lower()
    )
    assert (
        "Elegí la condición"
        in mundo.c.post(
            "/clientes/guardar", data={**base, "documento": "30000000007", "condicion_iva": "otra"}
        ).text
    )


# --- ajustes -----------------------------------------------------------------------------------


def test_ajustes_guardan_categoria_y_preferencias(mundo: Mundo, tmp_path: Path) -> None:
    r = mundo.c.post(
        "/ajustes",
        data={"carpeta_pdf": str(tmp_path / "otra"), "categoria": "e"},
        headers={"hx-request": "true"},
    )
    assert "Listo" in r.text
    p = config.cargar().perfil()
    assert p.categoria_monotributo == "E" and p.aviso_duplicados is False  # casilla destildada
    assert config.cargar().carpeta_pdf == str(tmp_path / "otra")
    bad = mundo.c.post("/ajustes", data={"categoria": "Z"})
    assert "No conozco la categoría" in bad.text


def test_no_se_activa_produccion_sin_una_emision_de_prueba(mundo: Mundo) -> None:
    r = mundo.c.post("/ajustes/ambiente", data={"destino": "prod"}, headers={"hx-request": "true"})
    assert "No se activó producción" in r.text and "emisión de prueba" in r.text
    assert config.cargar().ambiente == "homo"


def test_se_activa_produccion_cuando_todo_esta_en_orden_y_se_vuelve_a_pruebas(mundo: Mundo) -> None:
    mundo.c.post("/nueva/emitir", data={"borrador_id": _borrador(_preparar(mundo).text)})
    r = mundo.c.post("/ajustes/ambiente", data={"destino": "prod"}, headers={"hx-request": "true"})
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/ajustes"
    assert config.cargar().ambiente == "prod"
    mundo.c.post("/ajustes/ambiente", data={"destino": "homo"}, headers={"hx-request": "true"})
    assert config.cargar().ambiente == "homo"


def test_backup_desde_ajustes_no_lleva_claves(mundo: Mundo, tmp_path: Path) -> None:
    r = mundo.c.post("/ajustes/backup")
    assert "Backup listo" in r.text and "No incluye tu clave privada" in r.text


def test_conectar_claude_desktop_escribe_la_entrada_con_backup(
    mundo: Mundo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destino = tmp_path / "Claude" / "claude_desktop_config.json"
    destino.parent.mkdir()
    destino.write_text('{"otro": 1}', encoding="utf-8")
    from facturitas.mcp import instalar

    monkeypatch.setattr(instalar, "rutas_claude_desktop", lambda: [destino])
    r = mundo.c.post("/ia/conectar")
    assert "Conecté Claude Desktop" in r.text and "copia del archivo anterior" in r.text
    assert '"facturitas"' in destino.read_text(
        encoding="utf-8"
    ) and '"otro": 1' in destino.read_text(encoding="utf-8")
    assert "claude mcp add" in mundo.c.get("/ia").text


# --- asistente de primera vez ------------------------------------------------------------------


def _crt_para(key: Path, destino: Path, cuit: str = CUIT) -> bytes:
    clave = serialization.load_pem_private_key(key.read_bytes(), password=None)
    assert isinstance(clave, rsa.RSAPrivateKey)
    n = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "a"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
        ]
    )
    ahora = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(clave.public_key())
        .serial_number(1).not_valid_before(ahora - timedelta(days=1))
        .not_valid_after(ahora + timedelta(days=365)).sign(clave, hashes.SHA256())
    )  # fmt: skip
    return cert.public_bytes(serialization.Encoding.PEM)


def test_asistente_web_de_punta_a_punta(
    perfil: Perfil, credencial: Credencial, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asis, "carpeta_certificados", lambda a: tmp_path / "certs" / a)
    from facturitas.app import servicios as srv_mod

    monkeypatch.setattr(srv_mod, "carpeta_certificados", lambda a: tmp_path / "certs" / a)
    arca = WsfeFalso()

    def contexto(nombre: str | None, ambiente: str | None) -> Contexto:
        cfg = config.cargar()
        p, cred = cfg.perfil_para(nombre, ambiente or cfg.ambiente)
        return Contexto(
            p,
            cred,
            WsaaFalso(),  # type: ignore[arg-type]
            arca,  # type: ignore[arg-type]
            "HOMO",
            perfil_nombre="default",
            ambiente="homo",
            historial=Historial(),
        )

    svc = Servicios(contexto, lambda: AHORA)
    c = TestClient(crear_app(svc, TOKEN), follow_redirects=False)
    c.cookies.set(COOKIE, TOKEN)
    assert c.get("/").headers["location"] == "/bienvenida"
    assert "Paso 1: tus datos" in c.get("/asistente").text

    r = c.post("/asistente/datos", data={
        "ambiente": "homo", "cuit": "20-00000000-1", "razon_social": "EMISOR FICTICIO",
        "condicion": "Responsable Monotributo", "domicilio": "Calle Falsa 123",
    })  # fmt: skip
    assert r.status_code == 303
    paso3 = c.get("/asistente?ambiente=homo").text
    assert "Paso 3: los trámites en ARCA" in paso3 and "BEGIN CERTIFICATE REQUEST" in paso3
    assert "WSASS" in paso3 and "facturitas-homo" in paso3
    assert c.get("/asistente/csr?ambiente=homo").text.startswith("-----BEGIN CERTIFICATE REQUEST")

    c.post("/asistente/tramites", data={"ambiente": "homo"})
    assert "Paso 4: el certificado" in c.get("/asistente?ambiente=homo").text
    av = asis.cargar_avance("homo")
    assert av
    # un certificado de otra clave se rechaza
    otra = tmp_path / "otra"
    otra.mkdir()
    from facturitas.arca.csr import generar_clave_y_csr

    k2, _, _ = generar_clave_y_csr(otra, "x", CUIT, "X")
    malo = c.post(
        "/asistente/certificado",
        data={"ambiente": "homo"},
        files={"archivo": ("a.crt", _crt_para(k2, tmp_path / "m.crt"))},
    )
    assert "NO corresponde a la clave" in malo.text
    bueno = c.post(
        "/asistente/certificado",
        data={"ambiente": "homo"},
        files={"archivo": ("a.crt", _crt_para(Path(av.key), tmp_path / "b.crt"))},
    )
    assert bueno.status_code == 303 and "Paso 5: guardar" in c.get("/asistente?ambiente=homo").text

    c.post("/asistente/guardar", data={"ambiente": "homo", "punto_venta": "1", "categoria": "A"})
    p = config.cargar().perfil()
    assert (p.cuit, p.punto_venta, p.categoria_monotributo) == (CUIT, 1, "A")
    r = c.post("/asistente/verificar", data={"ambiente": "homo"}, headers={"hx-request": "true"})
    assert "¡Todo listo!" in r.text
    assert asis.cargar_avance("homo") is None  # terminó
    assert c.get("/").status_code == 200  # ya hay perfil: entra al inicio


def test_el_asistente_web_pega_el_certificado_como_texto(
    perfil: Perfil, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from facturitas.app import servicios as srv_mod

    monkeypatch.setattr(srv_mod, "carpeta_certificados", lambda a: tmp_path / "certs" / a)
    svc = Servicios(lambda n, a: (_ for _ in ()).throw(config.ConfigError("x")), lambda: AHORA)
    c = TestClient(crear_app(svc, TOKEN), follow_redirects=False)
    c.cookies.set(COOKIE, TOKEN)
    c.post(
        "/asistente/datos",
        data={
            "ambiente": "homo",
            "cuit": CUIT,
            "razon_social": "N",
            "condicion": "Responsable Monotributo",
            "domicilio": "D",
        },
    )
    c.post("/asistente/tramites", data={"ambiente": "homo"})
    av = asis.cargar_avance("homo")
    assert av
    crt = _crt_para(Path(av.key), tmp_path / "t.crt").decode()
    r = c.post("/asistente/certificado", data={"ambiente": "homo", "texto": crt})
    assert r.status_code == 303 and asis.cargar_avance("homo").paso == 4  # type: ignore[union-attr]
    assert "Subí el archivo" in c.post("/asistente/certificado", data={"ambiente": "homo"}).text


def test_el_icono_y_los_archivos_estaticos_se_sirven(mundo: Mundo) -> None:
    assert mundo.c.get("/static/icono.png").content.startswith(b"\x89PNG")
    assert b"htmx" in mundo.c.get("/static/htmx.min.js").content[:200]
    assert (
        "Si sos monotributista" in mundo.c.get("/acerca").text
        and "medialuna" in mundo.c.get("/acerca").text
    )
