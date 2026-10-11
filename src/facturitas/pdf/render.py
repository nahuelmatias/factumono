"""PDF del comprobante, fiel al formato que genera ARCA ("Mis Comprobantes"): ORIGINAL,
DUPLICADO y TRIPLICADO (una hoja cada uno), letra en recuadro, QR y CAE.

Las posiciones se tomaron de los PDF de referencia en píxeles de una hoja de 924 px de ancho y
se escalan a A4 (`_K`). El origen de `_y` es el borde superior, como en la imagen.
"""

from datetime import date
from decimal import Decimal
from io import BytesIO

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from facturitas.arca.qr import url_qr
from facturitas.core.fechas import mostrar
from facturitas.core.models import NOMBRES_COMPROBANTE, Comprobante, Concepto, Item
from facturitas.core.money import redondear

_W: float = A4[0]
_H: float = A4[1]
_K = _W / 924
_IZQ, _DER = 22, 902
COPIAS = ("ORIGINAL", "DUPLICADO", "TRIPLICADO")

_NEGRITA, _NORMAL, _CURSIVA = "Helvetica-Bold", "Helvetica", "Helvetica-BoldOblique"
_GRIS_ENCABEZADO = 0.80

_TABLA_ARRIBA, _TABLA_ENCABEZADO_ALTO = 403, 28
_TABLA_CUERPO_ARRIBA, _TABLA_CUERPO_ABAJO = 430, 895
_COLUMNAS = (22, 85, 305, 405, 475, 595, 650, 758, 902)
_TITULOS = ("Código", "Producto / Servicio", "Cantidad", "U. Medida", "Precio Unit.",
            "% Bonif", "Imp. Bonif.", "Subtotal")  # fmt: skip


def _x(px: float) -> float:
    return px * _K


def _y(px: float) -> float:
    return _H - px * _K


def _num(valor: Decimal) -> str:
    """Importe al estilo del PDF de ARCA: sin separador de miles y con coma decimal."""
    return f"{redondear(valor):.2f}".replace(".", ",")


def _cantidad(valor: Decimal) -> str:
    texto = f"{valor:f}"
    if "." in texto:
        entero, dec = texto.split(".")
        dec = dec.rstrip("0").ljust(2, "0")[:4]
        texto = f"{entero}.{dec}"
    else:
        texto = f"{texto}.00"
    return texto.replace(".", ",")


class _Hoja:
    """Dibuja una hoja con coordenadas de la imagen de referencia."""

    def __init__(self, pdf: Canvas) -> None:
        self.pdf = pdf

    def texto(
        self, x: float, y: float, s: str, fuente: str = _NORMAL, tam: float = 8, *, alin: str = "i"
    ) -> None:
        self.pdf.setFont(fuente, tam)
        if alin == "d":
            self.pdf.drawRightString(_x(x), _y(y), s)
        elif alin == "c":
            self.pdf.drawCentredString(_x(x), _y(y), s)
        else:
            self.pdf.drawString(_x(x), _y(y), s)

    def parrafo(
        self, x: float, y: float, s: str, ancho: float, fuente: str, tam: float, paso: float
    ) -> int:
        lineas = simpleSplit(s, fuente, tam, _x(ancho)) or [""]
        for n, linea in enumerate(lineas):
            self.texto(x, y + n * paso, linea, fuente, tam)
        return len(lineas)

    def linea(self, x1: float, y1: float, x2: float, y2: float, grosor: float = 0.8) -> None:
        self.pdf.setLineWidth(grosor)
        self.pdf.line(_x(x1), _y(y1), _x(x2), _y(y2))

    def caja(
        self, x1: float, y1: float, x2: float, y2: float, relleno: float | None = None
    ) -> None:
        self.pdf.setLineWidth(0.8)
        if relleno is not None:
            self.pdf.setFillGray(relleno)
        self.pdf.rect(
            _x(x1), _y(y2), _x(x2 - x1), _x(y2 - y1), stroke=1, fill=0 if relleno is None else 1
        )
        self.pdf.setFillGray(0)


def _nombre_comprobante(c: Comprobante) -> str:
    return "NOTA DE CRÉDITO" if c.es_nota_de_credito else "FACTURA"


def _letra(c: Comprobante) -> str:
    nombre = NOMBRES_COMPROBANTE.get(c.cbte_tipo, "")
    return nombre[-1] if nombre else "?"


def _dibujar_marca_homologacion(h: _Hoja) -> None:
    h.pdf.saveState()
    h.pdf.setFillGray(0.9)
    h.pdf.setFont(_NEGRITA, 34)
    h.pdf.translate(_x(462), _y(640))
    h.pdf.rotate(35)
    h.pdf.drawCentredString(0, 0, "HOMOLOGACIÓN")
    h.pdf.setFont(_NEGRITA, 16)
    h.pdf.drawCentredString(0, -22, "PRUEBA SIN VALIDEZ FISCAL")
    h.pdf.restoreState()


def _dibujar_qr(h: _Hoja, url: str) -> None:
    qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    matriz = qr.get_matrix()
    lado = _x(108) / len(matriz)
    x0, y0 = _x(40), _y(998)
    h.pdf.setFillGray(0)
    for fila, celdas in enumerate(matriz):
        for col, negro in enumerate(celdas):
            if negro:
                h.pdf.rect(
                    x0 + col * lado, y0 - (fila + 1) * lado, lado + 0.05, lado + 0.05,
                    stroke=0, fill=1,
                )  # fmt: skip


def _encabezado(h: _Hoja, c: Comprobante, numero: int, copia: str) -> None:
    p, r = c.perfil, c.receptor
    h.caja(_IZQ, 36.6, _DER, 295)
    h.linea(_IZQ, 79, _DER, 79)
    h.texto(462, 65, copia, _NEGRITA, 14, alin="c")

    # recuadro de la letra
    h.caja(428, 79, 500, 143, relleno=1)
    h.texto(464, 112, _letra(c), _NEGRITA, 24, alin="c")
    h.texto(464, 133, f"COD. {c.cbte_tipo:03d}", _NEGRITA, 8, alin="c")
    h.linea(464, 143, 464, 262)
    h.linea(_IZQ, 262, _DER, 262)

    # emisor (izquierda)
    nombre, tam = (p.nombre_fantasia or p.razon_social).upper(), 10.5
    while stringWidth(nombre, _NEGRITA, tam) > _x(385) and tam > 6:
        tam -= 0.5
    h.texto(225, 118, nombre, _NEGRITA, tam, alin="c")
    h.texto(33, 181, "Razón Social:", _NEGRITA, 9)
    h.texto(130, 181, p.razon_social, _NORMAL, 9)
    h.texto(33, 218, "Domicilio Comercial:", _NEGRITA, 9)
    h.parrafo(180, 218, p.domicilio, 270, _NORMAL, 9, 16)
    h.texto(33, 257, "Condición frente al IVA:", _NEGRITA, 9)
    h.texto(205, 257, p.condicion_iva, _NEGRITA, 9)

    # comprobante (derecha)
    titulo = _nombre_comprobante(c)
    tam_t = 18.0
    while stringWidth(titulo, _NEGRITA, tam_t) > _x(340) and tam_t > 8:
        tam_t -= 0.5
    h.texto(530, 120, titulo, _NEGRITA, tam_t)
    h.texto(530, 154, "Punto de Venta:", _NEGRITA, 9)
    h.texto(648, 154, f"{c.punto_venta:05d}", _NEGRITA, 9)
    h.texto(717, 154, "Comp. Nro:", _NEGRITA, 9)
    h.texto(803, 154, f"{numero:08d}", _NEGRITA, 9)
    h.texto(530, 180, "Fecha de Emisión:", _NEGRITA, 9)
    h.texto(665, 180, mostrar(c.fecha), _NEGRITA, 9)
    h.texto(530, 214, "CUIT:", _NEGRITA, 9)
    h.texto(572, 214, p.cuit, _NORMAL, 9)
    h.texto(530, 235, "Ingresos Brutos:", _NEGRITA, 9)
    h.texto(651, 235, p.iibb, _NORMAL, 9)
    h.texto(530, 253, "Fecha de Inicio de Actividades:", _NEGRITA, 9)
    h.texto(754, 253, mostrar(p.inicio_actividades), _NORMAL, 9)

    # período facturado
    if c.concepto in (Concepto.SERVICIOS, Concepto.PRODUCTOS_Y_SERVICIOS):
        h.texto(48, 287, "Período Facturado Desde:", _NEGRITA, 9.3)
        h.texto(248, 287, mostrar(c.periodo_desde), _NORMAL, 9.3)
        h.texto(361, 287, "Hasta:", _NEGRITA, 9.3)
        h.texto(412, 287, mostrar(c.periodo_hasta), _NORMAL, 9.3)
        h.texto(564, 287, "Fecha de Vto. para el pago:", _NEGRITA, 9.3)
        h.texto(770, 287, mostrar(c.vto_pago), _NORMAL, 9.3)

    # receptor
    h.caja(_IZQ, 301, _DER, 397)
    if r.doc_tipo == 80:
        h.texto(33, 316, "CUIT:", _NEGRITA, 8.2)
        h.texto(75, 316, str(r.doc_nro).zfill(11), _NORMAL, 8.2)
    elif r.doc_tipo == 96:
        h.texto(33, 316, "DNI:", _NEGRITA, 8.2)
        h.texto(68, 316, str(r.doc_nro), _NORMAL, 8.2)
    etiqueta = "Apellido y Nombre / Razón Social:"
    h.texto(345, 316, etiqueta, _NEGRITA, 8)
    h.texto(345 + stringWidth(etiqueta, _NEGRITA, 8) / _K + 3, 316, r.nombre, _NORMAL, 8)
    h.texto(33, 341, "Condición frente al IVA:", _NEGRITA, 8.2)
    h.texto(203, 341, r.condicion_iva, _NORMAL, 8.2)
    if r.domicilio:
        h.texto(485, 341, "Domicilio:", _NEGRITA, 8)
        x_dom = 485 + stringWidth("Domicilio:", _NEGRITA, 8) / _K + 3
        h.parrafo(x_dom, 341, r.domicilio, _DER - 6 - x_dom, _NORMAL, 8, 14.5)
    h.texto(33, 371, "Condición de venta:", _NEGRITA, 8.2)
    h.texto(176, 371, c.condicion_venta, _NORMAL, 8.2)
    if c.cbte_asoc:
        a = c.cbte_asoc
        asociado = NOMBRES_COMPROBANTE.get(a.tipo, f"Cbte. {a.tipo}")
        etiqueta_asoc = "Comprobante asociado:"
        x_asoc = 485 + stringWidth(etiqueta_asoc, _NEGRITA, 8.2) / _K + 4
        h.texto(485, 371, etiqueta_asoc, _NEGRITA, 8.2)
        h.texto(x_asoc, 371, f"{asociado} {a.punto_venta:05d}-{a.numero:08d}", _NORMAL, 8.2)
        if c.motivo:
            h.texto(485, 386, "Motivo:", _NEGRITA, 8.2)
            h.texto(
                485 + stringWidth("Motivo:", _NEGRITA, 8.2) / _K + 4,
                386,
                c.motivo[:70],
                _NORMAL,
                8.2,
            )


def _encabezado_tabla(h: _Hoja) -> None:
    h.caja(
        _IZQ, _TABLA_ARRIBA, _DER, _TABLA_ARRIBA + _TABLA_ENCABEZADO_ALTO, relleno=_GRIS_ENCABEZADO
    )
    for xc in _COLUMNAS[1:-1]:
        h.linea(xc, _TABLA_ARRIBA, xc, _TABLA_ARRIBA + _TABLA_ENCABEZADO_ALTO, 0.5)
    for n, titulo in enumerate(_TITULOS):
        x1, x2 = _COLUMNAS[n], _COLUMNAS[n + 1]
        if n in (0, 1):
            h.texto(x1 + 7, _TABLA_ARRIBA + 19, titulo, _NEGRITA, 8.2)
        else:
            h.texto((x1 + x2) / 2, _TABLA_ARRIBA + 19, titulo, _NEGRITA, 8, alin="c")


def _alto_fila(i: Item) -> float:
    lineas_desc = len(simpleSplit(i.descripcion, _NORMAL, 7.7, _x(_COLUMNAS[2] - _COLUMNAS[1] - 8)))
    lineas_unidad = len(simpleSplit(i.unidad, _NORMAL, 7.2, _x(_COLUMNAS[4] - _COLUMNAS[3] - 6)))
    return max(lineas_desc * 13, lineas_unidad * 11) + 14


def _fila(h: _Hoja, i: Item, y: float) -> None:
    base = y + 17
    h.texto(_COLUMNAS[0] + 6, base, i.codigo, _NORMAL, 8)
    h.parrafo(_COLUMNAS[1] + 7, base, i.descripcion, _COLUMNAS[2] - _COLUMNAS[1] - 8,
              _NORMAL, 7.7, 13)  # fmt: skip
    h.texto(_COLUMNAS[3] - 9, base, _cantidad(i.cantidad), _NORMAL, 8, alin="d")
    unidad = simpleSplit(i.unidad, _NORMAL, 7.2, _x(_COLUMNAS[4] - _COLUMNAS[3] - 6))
    for n, parte in enumerate(unidad):
        h.texto((_COLUMNAS[3] + _COLUMNAS[4]) / 2, base + n * 11 - 1, parte, _NORMAL, 7.2, alin="c")
    h.texto(_COLUMNAS[5] - 1, base, _num(i.precio_unitario), _NORMAL, 8, alin="d")
    h.texto(_COLUMNAS[5] + 12, base, _num(i.bonificacion_pct), _NORMAL, 8)
    h.texto(_COLUMNAS[7] - 8, base, _num(i.importe_bonificacion), _NORMAL, 8, alin="d")
    h.texto(_COLUMNAS[8] - 4, base, _num(i.subtotal), _NORMAL, 8, alin="d")


def _paginar(items: tuple[Item, ...]) -> list[list[Item]]:
    paginas: list[list[Item]] = [[]]
    usado = 0.0
    capacidad = _TABLA_CUERPO_ABAJO - _TABLA_CUERPO_ARRIBA
    for i in items:
        alto = _alto_fila(i)
        if usado + alto > capacidad and paginas[-1]:
            paginas.append([])
            usado = 0.0
        paginas[-1].append(i)
        usado += alto
    return paginas


def _totales(h: _Hoja, c: Comprobante) -> None:
    h.caja(_IZQ, 803, _DER, 948)
    for y, etiqueta, valor in (
        (872, "Subtotal: $", c.total),
        (900, "Importe Otros Tributos: $", Decimal(0)),
        (929, "Importe Total: $", c.total),
    ):
        h.texto(764, y, etiqueta, _NEGRITA, 9, alin="d")
        h.texto(890, y, _num(valor), _NEGRITA, 9, alin="d")


def _pie(h: _Hoja, cae: str, cae_vto: date, pagina: int, paginas: int, url: str) -> None:
    _dibujar_qr(h, url)
    h.texto(428, 1019, f"Pág. {pagina}/{paginas}", _NEGRITA, 10)
    h.texto(678, 1015, "CAE N°:", _NEGRITA, 9.3)
    h.texto(745, 1015, cae, _NORMAL, 9.3)
    h.texto(570, 1039, "Fecha de Vto. de CAE:", _NEGRITA, 9.3)
    h.texto(745, 1039, mostrar(cae_vto), _NORMAL, 9.3)
    h.texto(178, 1080, "Comprobante Autorizado", _CURSIVA, 9)
    h.texto(
        178, 1106,
        "Esta Agencia no se responsabiliza por los datos ingresados en el detalle de la operación",
        _CURSIVA, 6.2,
    )  # fmt: skip


def renderizar(
    c: Comprobante,
    numero: int,
    cae: str,
    cae_vto: date,
    *,
    homologacion: bool,
    copias: tuple[str, ...] = COPIAS,
) -> bytes:
    """PDF completo (una hoja por copia y por página de ítems). Devuelve los bytes."""
    buf = BytesIO()
    pdf = Canvas(buf, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"{_nombre_comprobante(c).title()} {c.punto_venta:05d}-{numero:08d}")
    pdf.setAuthor("Facturitas")
    h = _Hoja(pdf)
    url = url_qr(
        fecha=c.fecha,
        cuit=c.perfil.cuit,
        punto_venta=c.punto_venta,
        cbte_tipo=c.cbte_tipo,
        numero=numero,
        importe=c.total,
        doc_tipo=c.receptor.doc_tipo,
        doc_nro=c.receptor.doc_nro,
        cae=cae,
        moneda=c.moneda,
        cotizacion=c.cotizacion,
    )
    paginas = _paginar(c.items)
    for copia in copias:
        for n, items in enumerate(paginas, start=1):
            if homologacion:
                _dibujar_marca_homologacion(h)
            _encabezado(h, c, numero, copia)
            _encabezado_tabla(h)
            y = float(_TABLA_CUERPO_ARRIBA)
            for i in items:
                _fila(h, i, y)
                y += _alto_fila(i)
            if n == len(paginas):
                _totales(h, c)
            _pie(h, cae, cae_vto, n, len(paginas), url)
            pdf.showPage()
    pdf.save()
    return buf.getvalue()
