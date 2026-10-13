"""Modelo de datos del comprobante, el perfil y la credencial.

Guarda todo lo necesario para el PDF (M2): domicilio del receptor, condición de venta e ítems
con código, unidad y bonificación. WSFE no recibe ítems: solo los totales.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import IntEnum

from factumono.core.money import redondear

FACTURA_C = 11
# Nota de crédito que anula cada tipo de factura.
NOTA_DE_CREDITO_DE = {1: 3, 6: 8, FACTURA_C: 13}
NOTA_DE_CREDITO_C = 13
NOMBRES_COMPROBANTE = {
    1: "Factura A",
    3: "Nota de Crédito A",
    6: "Factura B",
    8: "Nota de Crédito B",
    11: "Factura C",
    13: "Nota de Crédito C",
}


class Concepto(IntEnum):
    PRODUCTOS = 1
    SERVICIOS = 2
    PRODUCTOS_Y_SERVICIOS = 3


NOMBRES_CONCEPTO = {
    Concepto.PRODUCTOS: "Productos",
    Concepto.SERVICIOS: "Servicios",
    Concepto.PRODUCTOS_Y_SERVICIOS: "Productos y servicios",
}
_ALIAS_CONCEPTO = {
    "1": Concepto.PRODUCTOS, "productos": Concepto.PRODUCTOS, "producto": Concepto.PRODUCTOS,
    "prod": Concepto.PRODUCTOS, "bienes": Concepto.PRODUCTOS,
    "2": Concepto.SERVICIOS, "servicios": Concepto.SERVICIOS, "servicio": Concepto.SERVICIOS,
    "serv": Concepto.SERVICIOS,
    "3": Concepto.PRODUCTOS_Y_SERVICIOS, "ambos": Concepto.PRODUCTOS_Y_SERVICIOS,
    "productos y servicios": Concepto.PRODUCTOS_Y_SERVICIOS,
    "productos_y_servicios": Concepto.PRODUCTOS_Y_SERVICIOS,
    "productos+servicios": Concepto.PRODUCTOS_Y_SERVICIOS,
    "prod y serv": Concepto.PRODUCTOS_Y_SERVICIOS,
}  # fmt: skip
# Cómo se guarda el concepto de un cliente.
CONCEPTO_TEXTO = {
    Concepto.PRODUCTOS: "productos",
    Concepto.SERVICIOS: "servicios",
    Concepto.PRODUCTOS_Y_SERVICIOS: "ambos",
}


def parsear_concepto(valor: "str | int | Concepto") -> Concepto:
    """1 = productos, 2 = servicios, 3 = productos y servicios (también por nombre)."""
    if isinstance(valor, Concepto):
        return valor
    clave = str(valor).strip().lower()
    try:
        return _ALIAS_CONCEPTO[clave]
    except KeyError:
        raise ValueError(
            f"Concepto inválido: {valor!r}. Usá 1 (productos), 2 (servicios) o 3 (productos y "
            "servicios)."
        ) from None


# Cómo se arma el período de un comprobante de servicios, por cliente.
MODOS_PERIODO = {
    "mes_completo": "Mes completo",
    "dia_emision": "Día de emisión",
    "rango": "Rango de fechas",
    "sin_periodo": "Sin período",
    "auto": "Automático (según la última factura)",
}
_ALIAS_MODO = {
    "mes": "mes_completo", "mes_completo": "mes_completo", "mes completo": "mes_completo",
    "completo": "mes_completo",
    "dia": "dia_emision", "día": "dia_emision", "dia_emision": "dia_emision",
    "día de emisión": "dia_emision", "dia de emision": "dia_emision", "emision": "dia_emision",
    "hoy": "dia_emision",
    "rango": "rango", "fechas": "rango",
    "sin": "sin_periodo", "sin_periodo": "sin_periodo", "sin período": "sin_periodo",
    "sin periodo": "sin_periodo", "ninguno": "sin_periodo",
    "auto": "auto", "automatico": "auto", "automático": "auto",
}  # fmt: skip


def parsear_modo_periodo(texto: str) -> str:
    try:
        return _ALIAS_MODO[texto.strip().lower()]
    except KeyError:
        raise ValueError(
            f"Modo de período inválido: {texto!r}. Usá mes (mes completo), dia (día de emisión), "
            "rango (desde-hasta) o sin (sin período, solo productos)."
        ) from None


class DocTipo(IntEnum):
    CUIT = 80
    DNI = 96
    CONSUMIDOR_FINAL = 99


@dataclass(frozen=True)
class Credencial:
    """Certificado y clave del operador. Las rutas apuntan a archivos fuera del repo."""

    cuit: str
    cert: str
    key: str
    ambiente: str = "homo"


@dataclass(frozen=True)
class Perfil:
    """Contribuyente en cuyo nombre se factura (CUIT representado)."""

    cuit: str
    razon_social: str
    condicion_iva: str
    domicilio: str
    credencial: str = "default"
    iibb: str = ""
    inicio_actividades: date | None = None
    punto_venta: int | None = None
    nombre_fantasia: str = ""
    # Producción tiene su propia credencial y su propio punto de venta (los de homologación no
    # sirven). `config.Config.perfil_para` los usa en lugar de `credencial`/`punto_venta`.
    credencial_prod: str = ""
    punto_venta_prod: int | None = None
    # Avisar (solo informativo) cuando un comprobante se parece a uno ya emitido.
    aviso_duplicados: bool = True
    # Categoría del monotributo (A-K), para la barra de tope de la GUI. Vacío: no se muestra.
    categoria_monotributo: str = ""

    @property
    def cbte_tipo_default(self) -> int:
        """Factura C para monotributo/exento. Responsable Inscripto (A/B) llega en M5."""
        c = self.condicion_iva.lower()
        if "monotribut" in c or "exento" in c:
            return FACTURA_C
        raise ValueError(
            "Para un emisor Responsable Inscripto hace falta Factura A/B, que todavía no está "
            "implementada (hito M5)."
        )


@dataclass(frozen=True)
class Receptor:
    doc_tipo: int
    doc_nro: int
    nombre: str
    condicion_iva_id: int
    condicion_iva: str = ""
    domicilio: str = ""


@dataclass(frozen=True)
class Item:
    descripcion: str
    cantidad: Decimal
    precio_unitario: Decimal
    unidad: str = "unidades"
    codigo: str = "1"
    bonificacion_pct: Decimal = Decimal(0)

    @property
    def importe_bruto(self) -> Decimal:
        return self.cantidad * self.precio_unitario

    @property
    def importe_bonificacion(self) -> Decimal:
        return redondear(self.importe_bruto * self.bonificacion_pct / 100)

    @property
    def subtotal(self) -> Decimal:
        return redondear(self.importe_bruto) - self.importe_bonificacion


@dataclass(frozen=True)
class CbteAsociado:
    """Comprobante que una nota de crédito anula (`CbtesAsoc` de WSFE)."""

    tipo: int
    punto_venta: int
    numero: int
    cuit: str
    fecha: date | None = None


@dataclass(frozen=True)
class Comprobante:
    perfil: Perfil
    punto_venta: int
    cbte_tipo: int
    concepto: Concepto
    receptor: Receptor
    items: tuple[Item, ...]
    fecha: date
    periodo_desde: date | None = None
    periodo_hasta: date | None = None
    vto_pago: date | None = None
    condicion_venta: str = "Contado"
    moneda: str = "PES"
    cotizacion: Decimal = Decimal(1)
    extras: dict[str, str] = field(default_factory=dict)
    cbte_asoc: CbteAsociado | None = None
    motivo: str = ""

    @property
    def total(self) -> Decimal:
        return sum((i.subtotal for i in self.items), Decimal(0))

    @property
    def es_nota_de_credito(self) -> bool:
        return self.cbte_tipo in NOTA_DE_CREDITO_DE.values()

    def validar(self) -> None:
        if self.es_nota_de_credito and not self.cbte_asoc:
            raise ValueError("Una nota de crédito necesita el comprobante que anula.")
        if not self.items:
            raise ValueError("El comprobante necesita al menos un ítem.")
        if self.total <= 0:
            raise ValueError("El total del comprobante tiene que ser mayor a cero.")
        if self.concepto in (Concepto.SERVICIOS, Concepto.PRODUCTOS_Y_SERVICIOS) and not (
            self.periodo_desde and self.periodo_hasta and self.vto_pago
        ):
            raise ValueError(
                "Para servicios son obligatorios el período (desde/hasta) y el vencimiento de pago."
            )
        if self.periodo_desde and self.periodo_hasta and self.periodo_desde > self.periodo_hasta:
            raise ValueError("El período 'desde' no puede ser posterior al 'hasta'.")


@dataclass(frozen=True)
class Cliente:
    """Cliente guardado de un perfil, con los defaults para facturarle rápido."""

    alias: str
    nombre: str
    doc_tipo: int
    doc_nro: int
    condicion_iva: str
    domicilio: str = ""
    email: str = ""
    descripcion: str = ""  # ítem habitual
    unidad: str = "otras unidades"
    concepto: str = "servicios"  # 'servicios' | 'productos' | 'ambos' (1/2/3)
    vto_dias: int = 4  # días de vencimiento de pago desde la emisión
    modo_periodo: str = "mes_completo"  # ver MODOS_PERIODO
    condicion_venta: str = "Contado"
    plantilla: str = ""  # ítems de la plantilla (JSON), ver `factumono importar`
