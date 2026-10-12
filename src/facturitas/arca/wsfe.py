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
from facturitas.arca.tls import cliente_arca
from facturitas.arca.wsaa import Wsaa
from facturitas.core.fechas import a_arca as _fecha
from facturitas.core.fechas import de_arca
from facturitas.core.models import FACTURA_C, NOTA_DE_CREDITO_C, Comprobante, Concepto
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
class ComprobanteConsultado:
    """Respuesta de `FECompConsultar` para un comprobante que existe en ARCA."""

    punto_venta: int
    cbte_tipo: int
    numero: int
    concepto: int
    doc_tipo: int
    doc_nro: int
    fecha: date
    importe_total: Decimal
    resultado: str
    cae: str
    cae_vto: str
    periodo_desde: date | None = None
    periodo_hasta: date | None = None
    vto_pago: date | None = None
    condicion_iva_id: int | None = None


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


def _num(d: Decimal) -> str:
    return f"{redondear(d):.2f}"


def normalizar_texto(t: str) -> str:
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn"
    )
    return sin_acentos.lower().replace("-", " ").strip()


def construir_detalle_factura_c(c: Comprobante, numero: int) -> str:
    """XML de `FECAEDetRequest` para Factura C / Nota de Crédito C.

    ImpNeto = total, ImpIVA = 0, sin array Iva. La nota de crédito agrega `CbtesAsoc` con la
    factura que anula (va después de `CondicionIVAReceptorId`, según el WSDL).
    """
    if c.cbte_tipo not in (FACTURA_C, NOTA_DE_CREDITO_C):
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
    if c.cbte_asoc:
        a = c.cbte_asoc
        partes.append(
            "<ar:CbtesAsoc><ar:CbteAsoc>"
            f"<ar:Tipo>{a.tipo}</ar:Tipo><ar:PtoVta>{a.punto_venta}</ar:PtoVta>"
            f"<ar:Nro>{a.numero}</ar:Nro><ar:Cuit>{escape(a.cuit)}</ar:Cuit>"
            + (f"<ar:CbteFch>{_fecha(a.fecha)}</ar:CbteFch>" if a.fecha else "")
            + "</ar:CbteAsoc></ar:CbtesAsoc>"
        )
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
        self._cliente = cliente or cliente_arca(60)

    # --- transporte -------------------------------------------------------------------------

    def _auth(self) -> str:
        t = self._wsaa.ticket("wsfe")
        return (
            "<ar:Auth>"
            f"<ar:Token>{escape(t.token)}</ar:Token><ar:Sign>{escape(t.sign)}</ar:Sign>"
            f"<ar:Cuit>{self._cuit}</ar:Cuit></ar:Auth>"
        )

    def _llamar(
        self, metodo: str, cuerpo: str = "", *, con_auth: bool = True, escribe: bool = False
    ) -> ET.Element:
        """POST SOAP. Con `escribe=True` (FECAESolicitar), una vez enviado el pedido cualquier
        falla de transporte o de formato se informa como `EstadoIncierto`: pudo haber llegado."""
        interior = (self._auth() if con_auth else "") + cuerpo  # si falla acá, no se envió nada
        try:
            return self._post(metodo, interior)
        except (httpx.HTTPError, ArcaError) as e:
            if not escribe:
                raise
            raise EstadoIncierto(
                "Se cortó la comunicación con ARCA o su respuesta no se pudo leer después de "
                f"pedir el CAE, así que NO se sabe si el comprobante quedó autorizado. ({e})"
            ) from None

    def _post(self, metodo: str, interior: str) -> ET.Element:
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

    def consultar(
        self, punto_venta: int, cbte_tipo: int, numero: int
    ) -> ComprobanteConsultado | None:
        """FECompConsultar. Devuelve None si ARCA no tiene ese comprobante (error 602)."""
        r = self._llamar(
            "FECompConsultar",
            "<ar:FeCompConsReq>"
            f"<ar:CbteTipo>{cbte_tipo}</ar:CbteTipo><ar:CbteNro>{numero}</ar:CbteNro>"
            f"<ar:PtoVta>{punto_venta}</ar:PtoVta></ar:FeCompConsReq>",
        )
        errores = self._errores(r)
        if errores and all(c == 602 for c, _ in errores):
            return None
        self._fallar_si_hay_errores(r)
        g = r.find("./ResultGet")
        if g is None:
            return None

        def fecha(campo: str) -> date | None:
            v = (g.findtext(campo) or "").strip()
            return de_arca(v) if v else None

        cond = (g.findtext("CondicionIVAReceptorId") or "").strip()
        return ComprobanteConsultado(
            punto_venta=int(g.findtext("PtoVta") or punto_venta),
            cbte_tipo=int(g.findtext("CbteTipo") or cbte_tipo),
            numero=int(g.findtext("CbteDesde") or numero),
            concepto=int(g.findtext("Concepto") or 0),
            doc_tipo=int(g.findtext("DocTipo") or 0),
            doc_nro=int(g.findtext("DocNro") or 0),
            fecha=fecha("CbteFch") or date.min,
            importe_total=Decimal((g.findtext("ImpTotal") or "0").strip()),
            resultado=(g.findtext("Resultado") or "").strip(),
            cae=(g.findtext("CodAutorizacion") or "").strip(),
            cae_vto=(g.findtext("FchVto") or "").strip(),
            periodo_desde=fecha("FchServDesde"),
            periodo_hasta=fecha("FchServHasta"),
            vto_pago=fecha("FchVtoPago"),
            condicion_iva_id=int(cond) if cond else None,
        )

    def emitir(self, c: Comprobante) -> ResultadoCae:
        """Numera con `FECompUltimoAutorizado + 1` justo antes de pedir el CAE.

        Ojo: llamar solo después de que el usuario confirmó el preview. Quien necesite
        idempotencia y reconciliación usa `core.service.emitir`, que guarda el número en el
        historial antes de enviar.
        """
        return self.solicitar_cae(c, self.proximo_numero(c.punto_venta, c.cbte_tipo))

    def solicitar_cae(self, c: Comprobante, numero: int) -> ResultadoCae:
        """FECAESolicitar. Si se corta la comunicación una vez enviado el pedido, no se sabe si
        ARCA lo autorizó: se levanta `EstadoIncierto` y NO se reintenta (hay que reconciliar)."""
        detalle = construir_detalle_factura_c(c, numero)
        cuerpo = (
            "<ar:FeCAEReq><ar:FeCabReq>"
            f"<ar:CantReg>1</ar:CantReg><ar:PtoVta>{c.punto_venta}</ar:PtoVta>"
            f"<ar:CbteTipo>{c.cbte_tipo}</ar:CbteTipo></ar:FeCabReq>"
            f"<ar:FeDetReq>{detalle}</ar:FeDetReq></ar:FeCAEReq>"
        )
        r = self._llamar("FECAESolicitar", cuerpo, escribe=True)
        self._fallar_si_hay_errores(r)
        det = r.find("./FeDetResp/FECAEDetResponse")
        if det is None:
            raise EstadoIncierto(
                "La respuesta de FECAESolicitar no trae el detalle del comprobante: NO se sabe "
                "si quedó autorizado."
            )
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
