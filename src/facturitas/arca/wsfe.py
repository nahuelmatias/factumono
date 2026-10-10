"""WSFEv1: cliente SOAP liviano (httpx + plantillas XML).

Referencia: manual del desarrollador WSFEv1 (RG 4291) v4.7. El orden de los elementos de
`FECAEDetRequest` sigue el WSDL oficial (es una secuencia: el orden importa).
"""

import json
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from facturitas.arca.ambientes import Ambiente
from facturitas.arca.errors import (
    ArcaError,
    ComprobanteRechazado,
    EstadoIncierto,
    traducir,
)
from facturitas.arca.wsaa import Wsaa
from facturitas.core.models import FACTURA_C, Comprobante, Concepto
from facturitas.core.money import redondear

NS = "http://ar.gov.afip.dif.FEV1/"
_TTL_PARAMETROS = 24 * 3600


@dataclass(frozen=True)
class PuntoVenta:
    nro: int
    emision_tipo: str
    bloqueado: bool
    fecha_baja: str


@dataclass(frozen=True)
class CondicionIva:
    id: int
    descripcion: str
    clase: str


@dataclass(frozen=True)
class ResultadoCae:
    resultado: str
    cae: str
    cae_vto: str
    numero: int
    observaciones: list[str] = field(default_factory=list)


def _sin_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _fecha(d: date) -> str:
    return d.strftime("%Y%m%d")


def _num(d: Decimal) -> str:
    return f"{redondear(d):.2f}"


def normalizar_texto(t: str) -> str:
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn"
    )
    return sin_acentos.lower().replace("-", " ").strip()


def construir_detalle_factura_c(c: Comprobante, numero: int) -> str:
    """XML de `FECAEDetRequest` para Factura C: ImpNeto = total, ImpIVA = 0, sin array Iva."""
    if c.cbte_tipo != FACTURA_C:
        raise ValueError("Por ahora solo se puede emitir Factura C (A/B llegan en el hito M5).")
    c.validar()
    total = _num(c.total)
    partes = [
        f"<ar:Concepto>{int(c.concepto)}</ar:Concepto>",
        f"<ar:DocTipo>{c.receptor.doc_tipo}</ar:DocTipo>",
        f"<ar:DocNro>{c.receptor.doc_nro}</ar:DocNro>",
        f"<ar:CbteDesde>{numero}</ar:CbteDesde>",
        f"<ar:CbteHasta>{numero}</ar:CbteHasta>",
        f"<ar:CbteFch>{_fecha(c.fecha)}</ar:CbteFch>",
        f"<ar:ImpTotal>{total}</ar:ImpTotal>",
        "<ar:ImpTotConc>0.00</ar:ImpTotConc>",
        f"<ar:ImpNeto>{total}</ar:ImpNeto>",
        "<ar:ImpOpEx>0.00</ar:ImpOpEx>",
        "<ar:ImpTrib>0.00</ar:ImpTrib>",
        "<ar:ImpIVA>0.00</ar:ImpIVA>",
    ]
    if c.concepto in (Concepto.SERVICIOS, Concepto.PRODUCTOS_Y_SERVICIOS):
        assert c.periodo_desde and c.periodo_hasta and c.vto_pago  # validado arriba
        partes += [
            f"<ar:FchServDesde>{_fecha(c.periodo_desde)}</ar:FchServDesde>",
            f"<ar:FchServHasta>{_fecha(c.periodo_hasta)}</ar:FchServHasta>",
            f"<ar:FchVtoPago>{_fecha(c.vto_pago)}</ar:FchVtoPago>",
        ]
    partes += [
        f"<ar:MonId>{escape(c.moneda)}</ar:MonId>",
        f"<ar:MonCotiz>{c.cotizacion}</ar:MonCotiz>",
        f"<ar:CondicionIVAReceptorId>{c.receptor.condicion_iva_id}</ar:CondicionIVAReceptorId>",
    ]
    return "<ar:FECAEDetRequest>" + "".join(partes) + "</ar:FECAEDetRequest>"


class Wsfe:
    def __init__(
        self,
        cuit_representado: str,
        wsaa: Wsaa,
        ambiente: Ambiente,
        cache_dir: Path,
        cliente: httpx.Client | None = None,
    ) -> None:
        self._cuit = cuit_representado
        self._wsaa = wsaa
        self._amb = ambiente
        self._cache = cache_dir
        self._cliente = cliente or httpx.Client(timeout=60)

    # --- transporte -------------------------------------------------------------------------

    def _auth(self) -> str:
        t = self._wsaa.ticket("wsfe")
        return (
            "<ar:Auth>"
            f"<ar:Token>{escape(t.token)}</ar:Token><ar:Sign>{escape(t.sign)}</ar:Sign>"
            f"<ar:Cuit>{self._cuit}</ar:Cuit></ar:Auth>"
        )

    def _llamar(self, metodo: str, cuerpo: str = "", *, con_auth: bool = True) -> ET.Element:
        interior = (self._auth() if con_auth else "") + cuerpo
        sobre = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><ar:{metodo} xmlns:ar="{NS}">{interior}</ar:{metodo}></soap:Body>'
            "</soap:Envelope>"
        )
        resp = self._cliente.post(
            self._amb.wsfe_url,
            content=sobre.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{NS}{metodo}"',
            },
        )
        try:
            raiz = _sin_ns(ET.fromstring(resp.text))
        except ET.ParseError:
            raise ArcaError(
                f"ARCA devolvió una respuesta ilegible (HTTP {resp.status_code})."
            ) from None
        falla = raiz.find(".//faultstring")
        if falla is not None:
            raise ArcaError(f"ARCA devolvió un error SOAP: {(falla.text or '').strip()}")
        resultado = raiz.find(f".//{metodo}Result")
        if resultado is None:
            raise ArcaError(f"La respuesta de {metodo} no trae resultado.")
        return resultado

    @staticmethod
    def _errores(resultado: ET.Element) -> list[tuple[int, str]]:
        return [
            (int(e.findtext("Code") or 0), (e.findtext("Msg") or "").strip())
            for e in resultado.findall("./Errors/Err")
        ]

    def _fallar_si_hay_errores(self, resultado: ET.Element) -> None:
        errores = self._errores(resultado)
        if errores:
            texto = "; ".join(traducir(c, m) for c, m in errores)
            raise ArcaError(texto, codigo=errores[0][0])

    # --- consultas --------------------------------------------------------------------------

    def dummy(self) -> dict[str, str]:
        r = self._llamar("FEDummy", con_auth=False)
        return {
            "AppServer": r.findtext("AppServer") or "",
            "DbServer": r.findtext("DbServer") or "",
            "AuthServer": r.findtext("AuthServer") or "",
        }

    def ultimo_autorizado(self, punto_venta: int, cbte_tipo: int) -> int:
        r = self._llamar(
            "FECompUltimoAutorizado",
            f"<ar:PtoVta>{punto_venta}</ar:PtoVta><ar:CbteTipo>{cbte_tipo}</ar:CbteTipo>",
        )
        self._fallar_si_hay_errores(r)
        return int(r.findtext("CbteNro") or 0)

    def puntos_venta(self) -> list[PuntoVenta]:
        r = self._llamar("FEParamGetPtosVenta")
        errores = self._errores(r)
        if errores and all(c == 602 for c, _ in errores):
            return []  # 602 = "no hay puntos de venta": no es un error, es un estado
        self._fallar_si_hay_errores(r)
        return [
            PuntoVenta(
                nro=int(p.findtext("Nro") or 0),
                emision_tipo=(p.findtext("EmisionTipo") or "").strip(),
                bloqueado=(p.findtext("Bloqueado") or "N").strip().upper() == "S",
                fecha_baja=(p.findtext("FchBaja") or "").strip(),
            )
            for p in r.findall("./ResultGet/PtoVenta")
        ]

    def condiciones_iva_receptor(self, clase: str | None = "C") -> list[CondicionIva]:
        """FEParamGetCondicionIvaReceptor, cacheado 24 h en disco (no se hardcodean los códigos)."""
        ruta = self._cache / f"condiciones-iva-{self._amb.nombre}-{clase or 'todas'}.json"
        if ruta.exists() and time.time() - ruta.stat().st_mtime < _TTL_PARAMETROS:
            try:
                return [CondicionIva(**d) for d in json.loads(ruta.read_text(encoding="utf-8"))]
            except (ValueError, TypeError):
                pass
        cuerpo = f"<ar:ClaseCmp>{escape(clase)}</ar:ClaseCmp>" if clase else ""
        r = self._llamar("FEParamGetCondicionIvaReceptor", cuerpo)
        self._fallar_si_hay_errores(r)
        condiciones = [
            CondicionIva(
                id=int(c.findtext("Id") or 0),
                descripcion=(c.findtext("Desc") or "").strip(),
                clase=(c.findtext("Cmp_Clase") or "").strip(),
            )
            for c in r.findall("./ResultGet/CondicionIvaReceptor")
        ]
        ruta.write_text(
            json.dumps([c.__dict__ for c in condiciones], ensure_ascii=False), encoding="utf-8"
        )
        return condiciones

    def resolver_condicion_iva(self, texto: str, clase: str | None = "C") -> CondicionIva:
        """Busca por id numérico o por texto (sin acentos, sin mayúsculas) entre las opciones."""
        opciones = self.condiciones_iva_receptor(clase)
        if texto.strip().isdigit():
            for o in opciones:
                if o.id == int(texto):
                    return o
            raise ArcaError(f"No existe la condición de IVA con código {texto} para esta clase.")
        buscado = normalizar_texto(texto)
        exactas = [o for o in opciones if normalizar_texto(o.descripcion) == buscado]
        candidatas = exactas or [o for o in opciones if buscado in normalizar_texto(o.descripcion)]
        if len(candidatas) == 1:
            return candidatas[0]
        detalle = ", ".join(f"{o.id}={o.descripcion}" for o in (candidatas or opciones))
        motivo = "es ambiguo" if candidatas else "no coincide con ninguna opción"
        raise ArcaError(f"'{texto}' {motivo}. Opciones: {detalle}")

    # --- emisión ----------------------------------------------------------------------------

    def proximo_numero(self, punto_venta: int, cbte_tipo: int) -> int:
        return self.ultimo_autorizado(punto_venta, cbte_tipo) + 1

    def emitir(self, c: Comprobante) -> ResultadoCae:
        """Pide el CAE. Consulta el último autorizado justo antes de enviar.

        Ojo: llamar solo después de que el usuario confirmó el preview. Si se corta la
        comunicación una vez enviado el pedido, no se sabe si ARCA lo autorizó: se levanta
        `EstadoIncierto` y NO se reintenta (la reconciliación llega en M2).
        """
        numero = self.proximo_numero(c.punto_venta, c.cbte_tipo)
        detalle = construir_detalle_factura_c(c, numero)
        cuerpo = (
            "<ar:FeCAEReq><ar:FeCabReq>"
            f"<ar:CantReg>1</ar:CantReg><ar:PtoVta>{c.punto_venta}</ar:PtoVta>"
            f"<ar:CbteTipo>{c.cbte_tipo}</ar:CbteTipo></ar:FeCabReq>"
            f"<ar:FeDetReq>{detalle}</ar:FeDetReq></ar:FeCAEReq>"
        )
        try:
            r = self._llamar("FECAESolicitar", cuerpo)
        except httpx.HTTPError as e:
            raise EstadoIncierto(
                "Se cortó la comunicación con ARCA al pedir el CAE: NO se sabe si el comprobante "
                f"{numero} quedó autorizado. No reintentes a ciegas: revisá con "
                f"`facturitas ultimo` si el último número subió. ({e})"
            ) from None
        self._fallar_si_hay_errores(r)
        det = r.find("./FeDetResp/FECAEDetResponse")
        if det is None:
            raise ArcaError("La respuesta de FECAESolicitar no trae el detalle del comprobante.")
        obs = [
            traducir(int(o.findtext("Code") or 0), (o.findtext("Msg") or "").strip())
            for o in det.findall("./Observaciones/Obs")
        ]
        resultado = (det.findtext("Resultado") or r.findtext("./FeCabResp/Resultado") or "").strip()
        if resultado != "A":
            detalle_obs = "; ".join(obs) or "ARCA no dio detalles."
            raise ComprobanteRechazado(f"ARCA rechazó el comprobante. {detalle_obs}")
        return ResultadoCae(
            resultado=resultado,
            cae=(det.findtext("CAE") or "").strip(),
            cae_vto=(det.findtext("CAEFchVto") or "").strip(),
            numero=int(det.findtext("CbteDesde") or numero),
            observaciones=obs,
        )
