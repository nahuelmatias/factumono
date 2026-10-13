"""Graba respuestas SOAP REALES de homologación como fixtures de test, ya sanitizadas.

Uso (desde la raíz del repo, con un perfil de homologación configurado):

    uv run python tools/grabar_fixtures.py [--emitir]

Sin `--emitir` solo hace consultas (FEDummy, FECompUltimoAutorizado, FEParamGet*, FECompConsultar).
Con `--emitir` además autoriza UNA Factura C y UNA Nota de Crédito C de prueba en homologación
(consumen dos números de la numeración de pruebas, que es independiente de la real).

Sanitizado: el CUIT del emisor se reemplaza por uno ficticio (20000000001). Las respuestas del
WSFE no traen token ni sign; la del WSAA no se graba nunca. Nunca toca producción.
"""

import argparse
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from factumono import config
from factumono.arca import ambientes
from factumono.arca.errors import ArcaError
from factumono.arca.wsaa import Wsaa
from factumono.arca.wsfe import Wsfe
from factumono.core.models import (
    CbteAsociado,
    Comprobante,
    Concepto,
    DocTipo,
    Item,
    Receptor,
)

DESTINO = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real"
CUIT_FICTICIO = "20000000001"
RECEPTOR_FICTICIO = 30000000007


class Grabadora:
    """Hook de httpx que guarda la última respuesta de cada método SOAP del WSFE."""

    def __init__(self, cuit_real: str) -> None:
        self.cuit_real = cuit_real
        self.ultima: dict[str, str] = {}

    def __call__(self, resp: httpx.Response) -> None:
        resp.read()
        accion = resp.request.headers.get("SOAPAction", "").strip('"').rsplit("/", 1)[-1]
        self.ultima[accion] = self.sanitizar(resp.text)

    def sanitizar(self, xml: str) -> str:
        xml = xml.replace(self.cuit_real, CUIT_FICTICIO)
        # Cualquier otro CUIT de 11 dígitos en <Cuit>/<DocNro> también se anonimiza.
        xml = re.sub(r"<Cuit>(\d{11})</Cuit>", f"<Cuit>{CUIT_FICTICIO}</Cuit>", xml)
        for etiqueta in ("Token", "Sign", "token", "sign"):
            if re.search(rf"<{etiqueta}>", xml):
                raise SystemExit(f"Una respuesta trae <{etiqueta}>: no se graba.")
        return xml

    def guardar(self, accion: str, nombre: str) -> None:
        DESTINO.mkdir(parents=True, exist_ok=True)
        (DESTINO / nombre).write_text(self.ultima[accion], encoding="utf-8")
        print(f"  grabado {nombre}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emitir", action="store_true")
    ap.add_argument("--perfil", default=None)
    args = ap.parse_args()

    cfg = config.cargar()
    perfil = cfg.perfil(args.perfil)
    cred = cfg.credencial_de(perfil)
    amb = ambientes.obtener(cred.ambiente)
    if amb.es_produccion:
        sys.exit("Este script es solo para homologación.")
    pv = perfil.punto_venta or 1
    grabadora = Grabadora(perfil.cuit)
    cliente = httpx.Client(timeout=60, event_hooks={"response": [grabadora]})
    cache = config.directorio_cache()
    wsaa = Wsaa(cred, amb, cache)  # el WSAA no graba nada
    wsfe = Wsfe(perfil.cuit, wsaa, amb, cache, cliente=cliente)

    print("Consultas:")
    wsfe.dummy()
    grabadora.guardar("FEDummy", "fedummy.xml")
    ultimo = wsfe.ultimo_autorizado(pv, 11)
    grabadora.guardar("FECompUltimoAutorizado", "ultimo.xml")
    wsfe.puntos_venta()
    grabadora.guardar("FEParamGetPtosVenta", "ptosventa.xml")
    # el caché de condiciones puede estar vigente: se fuerza la consulta real
    for archivo in cache.glob("condiciones-iva-*.json"):
        archivo.unlink()
    condiciones = wsfe.condiciones_iva_receptor("C")
    grabadora.guardar("FEParamGetCondicionIvaReceptor", "condiciones.xml")
    if ultimo:
        wsfe.consultar(pv, 11, ultimo)
        grabadora.guardar("FECompConsultar", "consulta_existente.xml")
    wsfe.consultar(pv, 11, ultimo + 100000)
    grabadora.guardar("FECompConsultar", "consulta_inexistente.xml")

    if not args.emitir:
        return
    print("Emisión de prueba en homologación:")
    ri = next(c for c in condiciones if "Inscripto" in c.descripcion)
    hoy = date.today()
    base = Comprobante(
        perfil=perfil,
        punto_venta=pv,
        cbte_tipo=11,
        concepto=Concepto.SERVICIOS,
        receptor=Receptor(
            doc_tipo=int(DocTipo.CUIT),
            doc_nro=RECEPTOR_FICTICIO,
            nombre="RECEPTOR FICTICIO SRL",
            condicion_iva_id=ri.id,
            condicion_iva=ri.descripcion,
        ),
        items=(Item("Servicios de prueba", Decimal(1), Decimal("1234.56"), "otras unidades"),),
        fecha=hoy,
        periodo_desde=hoy.replace(day=1),
        periodo_hasta=hoy,
        vto_pago=hoy,
    )
    # 1) Rechazo real: condición de IVA inexistente (no consume número).
    malo = Comprobante(**{**base.__dict__, "receptor": Receptor(80, RECEPTOR_FICTICIO, "X", 999)})
    try:
        wsfe.emitir(malo)
    except ArcaError as e:
        print(f"  rechazo esperado: {e}")
    grabadora.guardar("FECAESolicitar", "cae_rechazado.xml")
    # 2) Factura C autorizada.
    r = wsfe.emitir(base)
    grabadora.guardar("FECAESolicitar", "cae_ok.xml")
    print(f"  Factura C Nro. {r.numero} (CAE {r.cae})")
    wsfe.consultar(pv, 11, r.numero)
    grabadora.guardar("FECompConsultar", "consulta_existente.xml")
    # 3) Nota de Crédito C que la anula.
    nc = Comprobante(
        **{
            **base.__dict__,
            "cbte_tipo": 13,
            "cbte_asoc": CbteAsociado(11, pv, r.numero, perfil.cuit, hoy),
            "motivo": "Prueba de anulación",
        }
    )
    rn = wsfe.emitir(nc)
    grabadora.guardar("FECAESolicitar", "nc_ok.xml")
    print(f"  Nota de Crédito C Nro. {rn.numero} (CAE {rn.cae})")


if __name__ == "__main__":
    main()
