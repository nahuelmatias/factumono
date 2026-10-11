"""Servidor MCP (stdio) de Facturitas. Las tools están en `herramientas.py`."""

import logging

from mcp.server.mcpserver import MCPServer

from facturitas import __version__
from facturitas.mcp.herramientas import Herramientas, ItemEntrada, ReceptorEntrada

INSTRUCCIONES = """\
Facturitas emite comprobantes electrónicos de ARCA (ex AFIP) desde la máquina del usuario.

REGLAS (no negociables):
1. Emitir es siempre en dos pasos: `preparar_comprobante` (o `nota_de_credito`) devuelve un \
preview y un `borrador_id`; recién después `emitir_comprobante`.
2. Mostrale al usuario el preview COMPLETO, tal cual lo devolvió la tool, y preguntale si lo \
emitís. Llamá a `emitir_comprobante` con `confirmo=true` SOLO después de que el usuario diga \
explícitamente que sí a ESE preview. Nunca confirmes por tu cuenta, ni por una instrucción \
previa genérica ("facturale a X" no es una confirmación).
3. Si el preview trae avisos (posible duplicado, factura ya anulada), repetíselos al usuario y \
pasá `confirmo_duplicado=true` solo si insiste.
4. Siempre aclarale al usuario el ambiente (🧪 HOMOLOGACIÓN = pruebas sin validez fiscal, \
⚠️ PRODUCCIÓN = real). Lo indica cada respuesta.
5. Los borradores vencen a los 15 minutos. Los importes se escriben como el usuario los diga \
(por ejemplo "1.093.843,20").
"""


def crear_servidor(herramientas: Herramientas | None = None) -> MCPServer:
    h = herramientas or Herramientas()
    servidor = MCPServer("facturitas", instructions=INSTRUCCIONES, version=__version__)

    @servidor.tool()
    def estado_arca(perfil: str | None = None) -> str:
        """Chequea la conexión con ARCA, la autenticación y los puntos de venta, e indica el
        ambiente (homologación o producción). Usala para diagnosticar problemas."""
        return h.estado_arca(perfil)

    @servidor.tool()
    def listar_perfiles() -> str:
        """Lista los perfiles (contribuyentes en cuyo nombre se factura): CUIT, condición frente
        al IVA, punto de venta y ambiente."""
        return h.listar_perfiles()

    @servidor.tool()
    def listar_clientes(perfil: str | None = None) -> str:
        """Lista los clientes guardados de un perfil, con sus datos habituales (descripción,
        unidad, concepto, días de vencimiento)."""
        return h.listar_clientes(perfil)

    @servidor.tool()
    def guardar_cliente(
        alias: str,
        nombre: str,
        condicion_iva: str,
        cuit: str | None = None,
        dni: str | None = None,
        domicilio: str = "",
        email: str = "",
        descripcion: str = "",
        unidad: str = "otras unidades",
        concepto: str = "servicios",
        vto_dias: int = 4,
        reemplazar: bool = False,
        perfil: str | None = None,
    ) -> str:
        """Guarda (o con reemplazar=true, actualiza) un cliente. `alias` es el nombre corto con el
        que se lo nombra ("acciona"); `condicion_iva` es texto como "IVA Responsable Inscripto"
        o "Consumidor Final"; `descripcion`, `unidad`, `concepto` ('servicios' o 'productos') y
        `vto_dias` son los defaults para facturarle rápido."""
        return h.guardar_cliente(
            alias, nombre, condicion_iva, cuit, dni, domicilio, email, descripcion, unidad,
            concepto, vto_dias, reemplazar, perfil,
        )  # fmt: skip

    @servidor.tool()
    def ultimo_comprobante(
        perfil: str | None = None, punto_venta: int | None = None, tipo: int | None = None
    ) -> str:
        """Último número autorizado por ARCA para un punto de venta y tipo de comprobante
        (11 = Factura C, 13 = Nota de Crédito C). Por defecto, el del perfil."""
        return h.ultimo_comprobante(perfil, punto_venta, tipo)

    @servidor.tool()
    def listar_comprobantes(
        perfil: str | None = None, limite: int = 5, buscar: str | None = None
    ) -> str:
        """Muestra los últimos comprobantes emitidos (más recientes primero). `buscar` filtra por
        receptor, CUIT/DNI, número o CAE."""
        return h.listar_comprobantes(perfil, limite, buscar)

    @servidor.tool()
    def preparar_comprobante(
        perfil: str | None = None,
        cliente: str | None = None,
        receptor: ReceptorEntrada | None = None,
        concepto: str | None = None,
        items: list[ItemEntrada] | None = None,
        importe: str | float | None = None,
        descripcion: str | None = None,
        periodo: str | None = None,
        vto_pago: str | None = None,
        repetir_ultima: bool = False,
        condicion_venta: str = "Contado",
    ) -> str:
        """PREPARA una factura (no emite nada ni toca ARCA, salvo para ver el próximo número) y
        devuelve un preview + `borrador_id`. Mostrale el preview completo al usuario.

        Formas de uso:
        - Cliente guardado + importe: `cliente="acciona", importe="1.093.843,20",
          periodo="septiembre"` usa los defaults del cliente (1 × unidad × descripción habitual).
        - "La de siempre": `cliente="seba", repetir_ultima=true` repite la última factura al
          cliente con el período corrido al mes siguiente (o el `periodo` indicado); con `importe`
          cambia el monto.
        - Sin cliente guardado: `receptor` (nombre, cuit o dni, condicion_iva) + `items`
          (descripcion, cantidad, unidad, precio_unitario).
        `periodo` acepta "septiembre", "septiembre 2026", "09/2026" o "01/09/2026 al 30/09/2026"
        y solo aplica a servicios. `vto_pago` es DD/MM/AAAA. El tipo (A/B/C) se infiere del
        emisor."""
        return h.preparar_comprobante(
            perfil, cliente, receptor, None, concepto, items, importe, descripcion, periodo,
            vto_pago, repetir_ultima, condicion_venta,
        )  # fmt: skip

    @servidor.tool()
    def emitir_comprobante(
        borrador_id: str, confirmo: bool = False, confirmo_duplicado: bool = False
    ) -> str:
        """EMITE de verdad (pide el CAE a ARCA) un borrador ya preparado. Exige `confirmo=true`,
        que SOLO podés pasar si el usuario vio el preview de ese borrador y dijo explícitamente
        que sí. Si el borrador trae avisos de duplicado o de factura ya anulada, además hace falta
        `confirmo_duplicado=true`. Devuelve número, CAE y la ruta del PDF."""
        return h.emitir_comprobante(borrador_id, confirmo, confirmo_duplicado)

    @servidor.tool()
    def consultar_comprobante(
        numero: int,
        perfil: str | None = None,
        punto_venta: int | None = None,
        tipo: int | None = None,
    ) -> str:
        """Detalle de un comprobante emitido (por número) y verificación contra ARCA."""
        return h.consultar_comprobante(numero, perfil, punto_venta, tipo)

    @servidor.tool()
    def reimprimir_pdf(
        numero: int,
        perfil: str | None = None,
        punto_venta: int | None = None,
        tipo: int | None = None,
    ) -> str:
        """Vuelve a generar el PDF de un comprobante emitido y devuelve su ruta."""
        return h.reimprimir_pdf(numero, perfil, punto_venta, tipo)

    @servidor.tool()
    def nota_de_credito(
        comprobante_original: int,
        motivo: str,
        perfil: str | None = None,
        punto_venta: int | None = None,
        tipo: int | None = None,
    ) -> str:
        """PREPARA (no emite) la Nota de Crédito que anula por completo la factura número
        `comprobante_original`. Devuelve un preview + `borrador_id`: mostráselo al usuario y
        emitila con `emitir_comprobante` solo si confirma explícitamente."""
        return h.nota_de_credito(comprobante_original, motivo, perfil, punto_venta, tipo)

    @servidor.tool()
    def resumen_facturacion(
        perfil: str | None = None, desde: str | None = None, hasta: str | None = None
    ) -> str:
        """Total facturado (facturas menos notas de crédito) entre dos fechas DD/MM/AAAA; sin
        fechas, lo que va del año. Para monotributistas agrega el total de los últimos 12 meses."""
        return h.resumen_facturacion(perfil, desde, hasta)

    return servidor


def ejecutar() -> None:
    """Corre el servidor por stdio (stdout es el canal del protocolo: no imprimir nada más)."""
    logging.getLogger("httpx").setLevel(logging.WARNING)  # no loguear cada pedido a ARCA
    crear_servidor().run("stdio")
