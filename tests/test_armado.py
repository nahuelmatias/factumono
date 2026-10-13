from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from factumono.core import armado, service
from factumono.core.models import Cliente, Comprobante
from factumono.storage.clientes import ClienteNoEncontrado, Clientes
from factumono.storage.historial import Historial
from tests.test_service import ArcaFalso

HOY = date(2026, 10, 10)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("septiembre", (date(2026, 9, 1), date(2026, 9, 30))),
        ("Septiembre", (date(2026, 9, 1), date(2026, 9, 30))),
        ("octubre", (date(2026, 10, 1), date(2026, 10, 31))),
        ("diciembre", (date(2025, 12, 1), date(2025, 12, 31))),  # no hay diciembre futuro
        ("setiembre de 2025", (date(2025, 9, 1), date(2025, 9, 30))),
        ("febrero 2024", (date(2024, 2, 1), date(2024, 2, 29))),
        ("09/2026", (date(2026, 9, 1), date(2026, 9, 30))),
        ("2026-09", (date(2026, 9, 1), date(2026, 9, 30))),
        ("01/09/2026 al 15/09/2026", (date(2026, 9, 1), date(2026, 9, 15))),
    ],
)
def test_parsear_periodo(texto: str, esperado: tuple[date, date]) -> None:
    assert armado.parsear_periodo(texto, HOY) == esperado


@pytest.mark.parametrize("texto", ["marciano", "13/2026", "15/09/2026 al 01/09/2026", ""])
def test_periodo_invalido(texto: str) -> None:
    with pytest.raises(ValueError):
        armado.parsear_periodo(texto, HOY)


def test_periodo_siguiente() -> None:
    # mes completo -> mes completo siguiente
    assert armado.periodo_siguiente(date(2026, 10, 1), date(2026, 10, 31)) == (
        date(2026, 11, 1),
        date(2026, 11, 30),
    )
    assert armado.periodo_siguiente(date(2026, 12, 1), date(2026, 12, 31)) == (
        date(2027, 1, 1),
        date(2027, 1, 31),
    )
    assert armado.periodo_siguiente(date(2024, 1, 1), date(2024, 1, 31)) == (
        date(2024, 2, 1),
        date(2024, 2, 29),
    )
    # un solo día (como la de Seba en mayo): conserva los días, un mes corrido
    assert armado.periodo_siguiente(date(2026, 5, 1), date(2026, 5, 1)) == (
        date(2026, 6, 1),
        date(2026, 6, 1),
    )
    # sin día equivalente: se ajusta a fin de mes
    assert armado.periodo_siguiente(date(2026, 1, 31), date(2026, 1, 31)) == (
        date(2026, 2, 28),
        date(2026, 2, 28),
    )


def test_repetir_ultima_corre_el_periodo_y_la_fecha(comprobante: Comprobante) -> None:
    h = Historial()
    arca = ArcaFalso()
    original = replace(comprobante, fecha=date(2026, 9, 28), vto_pago=date(2026, 10, 2))
    ultima = service.emitir(
        h, arca, perfil="default", ambiente="homo", c=original, clave="k"
    ).registro
    nueva = armado.repetir_ultima(ultima, hoy=HOY)
    assert (nueva.periodo_desde, nueva.periodo_hasta) == (date(2026, 11, 1), date(2026, 11, 30))
    assert nueva.fecha == HOY
    assert nueva.vto_pago == date(2026, 10, 14)  # misma espera (4 días) que la anterior
    assert nueva.items == original.items and nueva.receptor == original.receptor
    # con otro importe y período explícito
    otra = armado.repetir_ultima(
        ultima, hoy=HOY, importe=Decimal("999.5"), desde=date(2026, 9, 1), hasta=date(2026, 9, 30)
    )
    assert otra.total == Decimal("999.50") and otra.periodo_desde == date(2026, 9, 1)


def test_clientes_se_guardan_por_perfil_y_se_buscan() -> None:
    c = Clientes()
    acciona = Cliente("Acciona", "ACCIONA IT S.R.L", 80, 30710581467, "IVA Responsable Inscripto")
    c.guardar("matias", acciona)
    assert c.buscar("matias", "ACCIONA") == replace(acciona, alias="acciona")  # alias en minúscula
    assert c.buscar("otro", "acciona") is None
    with pytest.raises(ValueError, match="Ya existe"):
        c.guardar("matias", acciona)
    c.guardar("matias", replace(acciona, email="a@b.com"), reemplazar=True)
    assert c.obtener("matias", "acciona it").email == "a@b.com"  # por parte del nombre
    with pytest.raises(ClienteNoEncontrado):
        c.obtener("matias", "nadie")
    assert c.borrar("matias", "acciona") and not c.borrar("matias", "acciona")
