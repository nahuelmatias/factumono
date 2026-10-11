"""Tools del MCP: flujo preparar -> confirmar, con un ARCA falso en memoria."""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from mcp import Client

from facturitas import config
from facturitas.arca.wsfe import CondicionIva, PuntoVenta
from facturitas.contexto import Contexto
from facturitas.core.models import Cliente, Credencial, Perfil
from facturitas.mcp.herramientas import Herramientas, ItemEntrada, ReceptorEntrada
from facturitas.mcp.server import crear_servidor
from facturitas.storage.historial import Historial
from tests.test_cli import WsaaFalso, WsfeFalso

AHORA = datetime(2026, 10, 10, 15, 0)


class WsfeMcp(WsfeFalso):
    def resolver_condicion_iva(self, texto: str, clase: str = "C") -> CondicionIva:
        if "inscripto" in texto.lower():
            return CondicionIva(1, "IVA Responsable Inscripto", "C")
        return CondicionIva(5, "Consumidor Final", "C")


@dataclass
class Entorno:
    h: Herramientas
    arca: WsfeMcp
    reloj: list[datetime]


@pytest.fixture
def entorno(perfil: Perfil, credencial: Credencial, tmp_path: Path) -> Entorno:
    config.guardar(
        config.Config(
            {"default": credencial}, {"default": perfil}, carpeta_pdf=str(tmp_path / "pdf")
        )
    )
    arca = WsfeMcp()
    ctx = Contexto(
        perfil, credencial, WsaaFalso(), arca,  # type: ignore[arg-type]
        "HOMOLOGACIÓN (pruebas)", perfil_nombre="default", ambiente="homo", historial=Historial(),
    )  # fmt: skip
    ctx.clientes.guardar(
        "default",
        Cliente(
            "acciona",
            "ACCIONA IT S.R.L",
            80,
            30710581467,
            "IVA Responsable Inscripto",
            "Moreau De Justo Alicia Av. 550",
            "",
            "Servicios de consultoría en Innovación",
            "otras unidades",
            "servicios",
            0,
        ),  # fmt: skip
    )
    ctx.clientes.guardar(
        "default",
        Cliente(
            "seba",
            "MARTIN SEBASTIAN PABLO",
            80,
            20354267277,
            "Consumidor Final",
            descripcion="Servicios informáticos",
        ),  # fmt: skip
    )
    reloj = [AHORA]
    return Entorno(Herramientas(lambda nombre: ctx, lambda: reloj[0]), arca, reloj)


def _id(texto: str) -> str:
    return texto.split("Borrador ")[1].split(" ")[0]


def test_preparar_no_emite_y_muestra_ambiente_y_preview(entorno: Entorno) -> None:
    r = entorno.h.preparar_comprobante(
        cliente="acciona", importe="1.093.843,20", periodo="septiembre"
    )
    assert r.startswith("🧪 HOMOLOGACIÓN") and r.count("HOMOLOGACIÓN") == 1
    assert "Borrador B-" in r and "ACCIONA IT S.R.L" in r
    assert "$ 1.093.843,20" in r and "01/09/2026 al 30/09/2026" in r
    assert "Servicios de consultoría en Innovación" in r and "otras unidades" in r
    assert "NO se emitió" in r or "Todavía NO se emitió" in r
    assert entorno.arca.autorizados == {}


def test_emitir_exige_confirmacion_explicita(entorno: Entorno) -> None:
    b = _id(entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="septiembre"))
    for confirmo in (False,):
        r = entorno.h.emitir_comprobante(b, confirmo)
        assert "🛑" in r and "No se emitió nada" in r
    assert entorno.arca.autorizados == {}
    r = entorno.h.emitir_comprobante(b, True)
    assert "✅" in r and "70000000000001" in r and "PDF" in r
    assert r.startswith("🧪 HOMOLOGACIÓN")
    assert len(entorno.arca.autorizados) == 1


def test_confirmar_dos_veces_el_mismo_borrador_no_duplica(entorno: Entorno) -> None:
    b = _id(entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="septiembre"))
    entorno.h.emitir_comprobante(b, True)
    r = entorno.h.emitir_comprobante(b, True)
    assert "ya se había emitido" in r
    assert len(entorno.arca.autorizados) == 1


def test_no_se_puede_emitir_sin_borrador_o_vencido(entorno: Entorno) -> None:
    assert "No existe el borrador" in entorno.h.emitir_comprobante("B-INEXISTENTE", True)
    b = _id(entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="septiembre"))
    entorno.reloj[0] = AHORA + timedelta(minutes=16)
    r = entorno.h.emitir_comprobante(b, True)
    assert "venció" in r and entorno.arca.autorizados == {}


def test_posible_duplicado_es_solo_informativo(entorno: Entorno) -> None:
    primero = _id(
        entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="septiembre")
    )
    entorno.h.emitir_comprobante(primero, True)
    segundo = entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="septiembre")
    assert "ya emitiste" in segundo and "Es solo un aviso" in segundo
    assert "confirmo_duplicado" not in segundo  # no pide confirmación extra
    r = entorno.h.emitir_comprobante(_id(segundo), True)  # alcanza con confirmo=true
    assert "✅" in r and len(entorno.arca.autorizados) == 2


def test_el_aviso_de_duplicado_se_puede_apagar_por_perfil(entorno: Entorno) -> None:
    ctx = entorno.h._contexto(None)
    ctx.perfil = replace(ctx.perfil, aviso_duplicados=False)
    primero = _id(
        entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="09/2026")
    )
    entorno.h.emitir_comprobante(primero, True)
    segundo = entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="09/2026")
    assert "ya emitiste" not in segundo


def test_en_produccion_toda_respuesta_empieza_con_el_aviso_de_produccion(
    entorno: Entorno,
) -> None:
    ctx = entorno.h._contexto(None)
    ctx.ambiente = "prod"
    aviso = "⚠️ PRODUCCIÓN — comprobante con validez fiscal"
    respuestas = [
        entorno.h.estado_arca(),
        entorno.h.listar_clientes(),
        entorno.h.listar_comprobantes(),
        entorno.h.resumen_facturacion(),
        entorno.h.preparar_comprobante(cliente="acciona", importe=1000, periodo="septiembre"),
        entorno.h.preparar_comprobante(cliente="marciano", importe=1),  # error
        entorno.h.emitir_comprobante("B-INEXISTENTE", True),  # error sin contexto
    ]
    for r in respuestas[:-1]:
        assert r.startswith(aviso), r[:80]
    assert "No existe el borrador" in respuestas[-1]


def test_estado_arca_en_prod_marca_el_punto_de_venta_configurado(entorno: Entorno) -> None:
    ctx = entorno.h._contexto(None)
    ctx.ambiente = "prod"
    entorno.arca.puntos = [
        PuntoVenta(2, "Factura Electrónica - Monotributo - Web Services", False, "NULL"),
        PuntoVenta(5, "Comprobantes en línea", False, "NULL"),
    ]
    r = entorno.h.estado_arca()
    assert "00002 · Factura Electrónica - Monotributo - Web Services" in r
    assert "← **configurado**" in r and r.count("configurado**") == 1
    entorno.arca.puntos = [PuntoVenta(5, "Comprobantes en línea", False, "NULL")]
    assert "❌" in entorno.h.estado_arca()


def test_la_de_siempre_repite_la_ultima_con_periodo_corrido(entorno: Entorno) -> None:
    sin_historial = entorno.h.preparar_comprobante(cliente="seba", repetir_ultima=True)
    assert "❌" in sin_historial and "No hay ninguna factura anterior" in sin_historial
    b = _id(
        entorno.h.preparar_comprobante(
            cliente="seba", importe="1500", periodo="01/09/2026 al 30/09/2026"
        )
    )
    entorno.h.emitir_comprobante(b, True)
    r = entorno.h.preparar_comprobante(cliente="seba", repetir_ultima=True)
    assert "Es la de siempre" in r and "FC 00002-00000001" in r
    assert "01/10/2026 al 31/10/2026" in r and "$ 1.500,00" in r
    assert len(entorno.arca.autorizados) == 1  # preparar no emite


def test_anular_prepara_la_nota_de_credito_y_pide_confirmacion(entorno: Entorno) -> None:
    b = _id(entorno.h.preparar_comprobante(cliente="seba", importe="1500", periodo="septiembre"))
    entorno.h.emitir_comprobante(b, True)
    r = entorno.h.nota_de_credito(1, "Error de facturación")
    assert "Nota de Crédito C" in r and "Anula:" in r and "00002-00000001" in r
    assert len(entorno.arca.autorizados) == 1  # todavía no se emitió
    assert "🛑" in entorno.h.emitir_comprobante(_id(r), False)
    assert "✅" in entorno.h.emitir_comprobante(_id(r), True)
    assert entorno.arca.autorizados[2].cbte_tipo == 13
    # anularla de nuevo exige confirmación extra
    otra = entorno.h.nota_de_credito(1, "otra vez")
    assert "YA FUE ANULADA" in otra
    assert "🛑" in entorno.h.emitir_comprobante(_id(otra), True)
    assert "no encuentro" in entorno.h.nota_de_credito(77, "x").lower()


def test_receptor_manual_con_items(entorno: Entorno) -> None:
    r = entorno.h.preparar_comprobante(
        receptor=ReceptorEntrada(nombre="Consumidor Final"),
        concepto="productos",
        items=[
            ItemEntrada(descripcion="Facturas", precio_unitario="5000", unidad="docena"),
            ItemEntrada(descripcion="Pan", cantidad="0.5", precio_unitario=3000, unidad="kg"),
        ],
    )
    assert "$ 6.500,00" in r and "docena" in r and "kg" in r
    assert "Período facturado" not in r


def test_listar_y_resumen(entorno: Entorno) -> None:
    assert "Todavía no hay" in entorno.h.listar_comprobantes()
    for importe in (1000, 2000, 3000):
        b = _id(
            entorno.h.preparar_comprobante(cliente="acciona", importe=importe, periodo="09/2026")
        )
        entorno.h.emitir_comprobante(b, True, True)
    ultimas = entorno.h.listar_comprobantes(limite=2)
    assert "$ 3.000,00" in ultimas and "$ 2.000,00" in ultimas and "$ 1.000,00" not in ultimas
    nc = _id(entorno.h.nota_de_credito(1, "anulada"))
    entorno.h.emitir_comprobante(nc, True)
    r = entorno.h.resumen_facturacion()
    assert "Facturas emitidas: 3 por **$ 6.000,00**" in r
    assert "Notas de crédito: 1 por $ 1.000,00" in r and "neto: $ 5.000,00" in r
    assert "Últimos 12 meses" in r and "homologación" in r


def test_clientes_por_tool(entorno: Entorno) -> None:
    assert "ACCIONA IT S.R.L" in entorno.h.listar_clientes()
    r = entorno.h.guardar_cliente(
        "nuevo", "NUEVO SA", "IVA Responsable Inscripto", cuit="30000000007"
    )
    assert "✅" in r
    assert "Ya existe" in entorno.h.guardar_cliente("nuevo", "X", "Consumidor Final", dni="1")
    assert "Indicá el CUIT" in entorno.h.guardar_cliente("z", "Z", "Consumidor Final")


def test_cliente_inexistente_es_un_error_claro(entorno: Entorno) -> None:
    r = entorno.h.preparar_comprobante(cliente="marciano", importe=1)
    assert "❌ No hay un cliente guardado" in r


def test_el_servidor_expone_las_tools_y_no_emite_sin_confirmar(entorno: Entorno) -> None:
    async def sesion() -> tuple[set[str], str, str]:
        async with Client(crear_servidor(entorno.h)) as c:
            nombres = {t.name for t in (await c.list_tools()).tools}
            prep = await c.call_tool(
                "preparar_comprobante",
                {"cliente": "acciona", "importe": "1.093.843,20", "periodo": "septiembre"},
            )
            texto = prep.content[0].text  # type: ignore[union-attr]
            sin = await c.call_tool("emitir_comprobante", {"borrador_id": _id(texto)})
            return nombres, texto, sin.content[0].text  # type: ignore[union-attr]

    nombres, preview, sin_confirmar = asyncio.run(sesion())
    assert nombres >= {
        "estado_arca", "listar_perfiles", "listar_clientes", "guardar_cliente",
        "ultimo_comprobante", "preparar_comprobante", "emitir_comprobante",
        "consultar_comprobante", "reimprimir_pdf", "nota_de_credito", "resumen_facturacion",
    }  # fmt: skip
    assert "Borrador B-" in preview
    assert "🛑" in sin_confirmar and entorno.arca.autorizados == {}


def test_hay_una_sola_forma_de_emitir() -> None:
    """Ninguna otra tool llama al servicio de emisión: solo `emitir_comprobante`."""
    import inspect

    from facturitas.mcp import herramientas

    fuente = inspect.getsource(herramientas.Herramientas)
    assert fuente.count("service.emitir(") == 1
    assert "service.emitir(" in inspect.getsource(herramientas.Herramientas.emitir_comprobante)
