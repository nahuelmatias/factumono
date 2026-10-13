"""Integración contra HOMOLOGACIÓN de ARCA (solo lectura). Correr con `pytest -m homo`.

Usa tu configuración real (la de `factumono init`), así que ignora el aislamiento de los
otros tests. Nunca emite comprobantes.
"""

import os

import pytest

from factumono import config
from factumono.arca import ambientes
from factumono.arca.wsaa import Wsaa
from factumono.arca.wsfe import Wsfe

pytestmark = pytest.mark.homo


@pytest.fixture
def wsfe(monkeypatch: pytest.MonkeyPatch) -> Wsfe:
    monkeypatch.delenv("FACTUMONO_HOME", raising=False)
    if os.environ.get("FACTUMONO_TEST_HOME"):
        monkeypatch.setenv("FACTUMONO_HOME", os.environ["FACTUMONO_TEST_HOME"])
    cfg = config.cargar()
    if not cfg.perfiles:
        pytest.skip("No hay perfil configurado (`factumono init`).")
    perfil = cfg.perfil()
    cred = cfg.credencial_de(perfil)
    amb = ambientes.obtener(cred.ambiente)
    cache = config.directorio_cache()
    return Wsfe(perfil.cuit, Wsaa(cred, amb, cache), amb, cache)


def test_dummy(wsfe: Wsfe) -> None:
    assert set(wsfe.dummy().values()) == {"OK"}


def test_ultimo_autorizado(wsfe: Wsfe) -> None:
    assert wsfe.ultimo_autorizado(1, 11) >= 0


def test_condiciones_iva_incluyen_consumidor_final(wsfe: Wsfe) -> None:
    assert any("consumidor final" in c.descripcion.lower() for c in wsfe.condiciones_iva_receptor())


def test_consultar_comprobante_inexistente_devuelve_none(wsfe: Wsfe) -> None:
    ultimo = wsfe.ultimo_autorizado(1, 11)
    assert wsfe.consultar(1, 11, ultimo + 100000) is None
