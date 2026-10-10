"""Modelo de datos del comprobante, el perfil y la credencial.

Guarda todo lo necesario para el PDF (M2): domicilio del receptor, condición de venta e ítems
con código, unidad y bonificación. WSFE no recibe ítems: solo los totales.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import IntEnum

from facturitas.core.money import redondear

FACTURA_C = 11
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

    @property
    def total(self) -> Decimal:
        return sum((i.subtotal for i in self.items), Decimal(0))

    def validar(self) -> None:
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
