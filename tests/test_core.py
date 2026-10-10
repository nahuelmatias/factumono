from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from facturitas.config import Config, cargar, guardar
from facturitas.core import cuit, money
from facturitas.core.models import Comprobante, Credencial, Item, Perfil


def test_cuit_validos_de_la_especificacion_oficial() -> None:
    assert cuit.es_valido("30000000007")
    assert cuit.es_valido("20-00000000-1")


def test_cuit_digito_verificador_incorrecto() -> None:
    assert not cuit.es_valido("30000000008")
    with pytest.raises(ValueError, match="verificador"):
        cuit.normalizar("30000000008")


def test_cuit_largo_incorrecto() -> None:
    with pytest.raises(ValueError, match="11 dígitos"):
        cuit.normalizar("123")


def test_cuit_formato() -> None:
    assert cuit.formatear("30000000007") == "30-00000000-7"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1500", "1500"),
        ("1500.50", "1500.50"),
        ("1500,50", "1500.50"),
        ("1.234,56", "1234.56"),
        ("$ 123.456.789,87", "123456789.87"),
        ("1.234.567", "1234567"),
    ],
)
def test_parsear_importes(texto: str, esperado: str) -> None:
    assert money.parsear(texto) == Decimal(esperado)


def test_parsear_importe_invalido() -> None:
    with pytest.raises(ValueError):
        money.parsear("abc")


def test_redondeo_mitad_hacia_arriba() -> None:
    assert money.redondear(Decimal("0.005")) == Decimal("0.01")
    assert money.redondear(Decimal("2.675")) == Decimal("2.68")


def test_formato_argentino() -> None:
    assert money.formatear_ar(Decimal("123456789.87")) == "123.456.789,87"
    assert money.formatear_ar(Decimal("1500")) == "1.500,00"


def test_item_con_bonificacion() -> None:
    item = Item("Pan", Decimal("0.5"), Decimal("3000"), "kg", bonificacion_pct=Decimal(10))
    assert item.importe_bonificacion == Decimal("150.00")
    assert item.subtotal == Decimal("1350.00")


def test_total_es_la_suma_de_los_items(comprobante: Comprobante) -> None:
    extra = Item("Otro", Decimal(2), Decimal("0.01"))
    c = replace(comprobante, items=(*comprobante.items, extra))
    assert c.total == Decimal("1234.58")


def test_servicios_exigen_periodo_y_vencimiento(comprobante: Comprobante) -> None:
    with pytest.raises(ValueError, match="período"):
        replace(comprobante, vto_pago=None).validar()


def test_total_cero_es_invalido(comprobante: Comprobante) -> None:
    c = replace(comprobante, items=(Item("x", Decimal(1), Decimal(0)),))
    with pytest.raises(ValueError, match="mayor a cero"):
        c.validar()


def test_emisor_responsable_inscripto_todavia_no_soportado(perfil: Perfil) -> None:
    ri = replace(perfil, condicion_iva="IVA Responsable Inscripto")
    with pytest.raises(ValueError, match="M5"):
        _ = ri.cbte_tipo_default


def test_config_ida_y_vuelta(perfil: Perfil) -> None:
    perfil = replace(perfil, nombre_fantasia='Panadería "Ñandú"')
    cfg = Config(
        credenciales={"default": Credencial("20000000001", "C:/x/a.crt", "C:/x/a.key")},
        perfiles={"default": perfil},
    )
    guardar(cfg)
    leido = cargar()
    assert leido.perfiles["default"] == perfil
    assert leido.credenciales["default"].cert == "C:/x/a.crt"
    assert leido.perfiles["default"].inicio_actividades == date(2020, 1, 1)
