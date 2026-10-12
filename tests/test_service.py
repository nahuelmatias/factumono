"""Emisión con historial, idempotencia y reconciliación (con un ARCA falso en memoria)."""

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

import httpx
import pytest

from facturitas.arca.errors import ArcaError, ComprobanteRechazado, EstadoIncierto
from facturitas.arca.wsfe import ComprobanteConsultado, ResultadoCae
from facturitas.core import service
from facturitas.core.models import Comprobante, Concepto
from facturitas.storage.historial import (
    AUTORIZADO,
    FALLIDO,
    INCIERTO,
    RECHAZADO,
    Historial,
)

AMB = "homo"


@dataclass
class ArcaFalso:
    """Simula a ARCA: guarda lo autorizado y permite cortar la comunicación en cada paso."""

    autorizados: dict[int, Comprobante] = field(default_factory=dict)
    # 'antes': el pedido no llega; 'despues': ARCA autoriza pero la respuesta se pierde
    corte: str | None = None
    sin_conexion: bool = False
    rechazar: bool = False
    envios: int = 0

    def _red(self) -> None:
        if self.sin_conexion:
            raise httpx.ConnectError("sin red")

    def ultimo_autorizado(self, punto_venta: int, cbte_tipo: int) -> int:
        self._red()
        return max((n for n, c in self.autorizados.items() if c.cbte_tipo == cbte_tipo), default=0)

    def proximo_numero(self, punto_venta: int, cbte_tipo: int) -> int:
        # El falso numera todos los tipos en una sola secuencia (más simple para los tests).
        self._red()
        return max(self.autorizados, default=0) + 1

    def solicitar_cae(self, c: Comprobante, numero: int) -> ResultadoCae:
        self.envios += 1
        if self.rechazar:
            raise ComprobanteRechazado("ARCA rechazó el comprobante. [10242] condición inválida")
        if self.corte == "antes":
            self.corte = None
            self.sin_conexion = False
            raise EstadoIncierto("se cortó antes de llegar")
        self.autorizados[numero] = c
        if self.corte == "despues":
            self.corte = None
            raise EstadoIncierto("se perdió la respuesta")
        return ResultadoCae("A", f"7000000000{numero:04d}", "20261020", numero)

    def consultar(
        self, punto_venta: int, cbte_tipo: int, numero: int
    ) -> ComprobanteConsultado | None:
        self._red()
        c = self.autorizados.get(numero)
        if c is None or c.cbte_tipo != cbte_tipo:
            return None
        return ComprobanteConsultado(
            punto_venta=punto_venta,
            cbte_tipo=cbte_tipo,
            numero=numero,
            concepto=int(c.concepto),
            doc_tipo=c.receptor.doc_tipo,
            doc_nro=c.receptor.doc_nro,
            fecha=c.fecha,
            importe_total=c.total,
            resultado="A",
            cae=f"7000000000{numero:04d}",
            cae_vto="20261020",
        )


@pytest.fixture
def historial() -> Historial:
    return Historial()


@pytest.fixture
def arca() -> ArcaFalso:
    return ArcaFalso()


def emitir(h: Historial, arca: ArcaFalso, c: Comprobante, clave: str = "k1") -> service.Emision:
    return service.emitir(h, arca, perfil="default", ambiente=AMB, c=c, clave=clave)


def test_emision_feliz_queda_en_el_historial(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    em = emitir(historial, arca, comprobante)
    r = em.registro
    assert (r.estado, r.numero, r.cae) == (AUTORIZADO, 1, "70000000000001")
    assert r.cae_vto == date(2026, 10, 20)
    assert r.es_homologacion
    assert r.comprobante == comprobante  # se guarda completo: alcanza para reimprimir el PDF
    assert historial.por_numero(comprobante.perfil.cuit, AMB, 2, 11, 1) == r


def test_misma_clave_no_emite_dos_veces(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    emitir(historial, arca, comprobante)
    otra = emitir(historial, arca, comprobante)
    assert otra.reutilizado
    assert arca.envios == 1
    assert len(arca.autorizados) == 1


def test_clave_de_otro_emisor_se_rechaza(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    emitir(historial, arca, comprobante)
    otro = replace(comprobante, perfil=replace(comprobante.perfil, cuit="30000000007"))
    with pytest.raises(ValueError, match="otro emisor"):
        emitir(historial, arca, otro)


def test_corte_despues_de_enviar_se_reconcilia_sin_duplicar(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    arca.corte = "despues"  # ARCA autoriza, pero la respuesta no llega
    em = emitir(historial, arca, comprobante)
    assert em.reconciliado
    assert (em.registro.estado, em.registro.numero) == (AUTORIZADO, 1)
    assert em.registro.cae == "70000000000001"
    assert arca.envios == 1 and len(arca.autorizados) == 1


def test_corte_antes_de_llegar_a_arca_confirma_que_no_se_emitio(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    arca.corte = "antes"
    with pytest.raises(ArcaError, match="NO se emitió"):
        emitir(historial, arca, comprobante)
    assert arca.autorizados == {}
    reg = historial.por_clave("k1")
    assert reg is not None and reg.estado == FALLIDO
    # Reintentar con la misma clave ahora es seguro y emite una sola vez.
    em = emitir(historial, arca, comprobante)
    assert em.registro.estado == AUTORIZADO
    assert len(arca.autorizados) == 1


def test_corte_y_sin_red_queda_incierto_y_se_reconcilia_despues(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    original = arca.solicitar_cae

    def corta_y_cae_la_red(c: Comprobante, numero: int) -> ResultadoCae:
        arca.autorizados[numero] = c  # ARCA lo autorizó...
        arca.sin_conexion = True  # ...y se cayó la red
        raise EstadoIncierto("timeout")

    arca.solicitar_cae = corta_y_cae_la_red  # type: ignore[method-assign]
    with pytest.raises(EstadoIncierto, match="INCIERTO"):
        emitir(historial, arca, comprobante)
    reg = historial.por_clave("k1")
    assert reg is not None and reg.estado == INCIERTO and reg.numero == 1

    # Vuelve la red: reconciliar encuentra el comprobante y no se emite otro.
    arca.sin_conexion = False
    arca.solicitar_cae = original  # type: ignore[method-assign]
    (resuelto,) = service.reconciliar_pendientes(historial, arca, comprobante.perfil.cuit, AMB)
    assert (resuelto.estado, resuelto.cae) == (AUTORIZADO, "70000000000001")
    assert len(arca.autorizados) == 1


def test_reintento_con_la_misma_clave_tras_un_corte_no_duplica(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    arca.autorizados[1] = comprobante  # ARCA ya lo tiene...
    historial.crear(  # ...pero acá quedó marcado como incierto (se murió el proceso)
        perfil="default", ambiente=AMB, c=comprobante, clave="k1", estado=INCIERTO, numero=1
    )
    em = emitir(historial, arca, comprobante)
    assert em.reconciliado and em.registro.numero == 1
    assert arca.envios == 0


def test_dos_tickets_iguales_no_se_confunden_al_reconciliar(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    emitir(historial, arca, comprobante, "k1")  # nro. 1, autorizado
    # Un segundo comprobante idéntico quedó "incierto" con el mismo número reservado:
    # el nro. 1 existe en ARCA y coincide en todo, pero ya es del primero, así que NO se adopta.
    dudoso = historial.crear(
        perfil="default", ambiente=AMB, c=comprobante, clave="k2", estado=INCIERTO, numero=1
    )
    resuelto = service.reconciliar(historial, arca, dudoso)
    assert resuelto.estado == FALLIDO
    assert len(arca.autorizados) == 1


def test_rechazo_se_guarda_con_el_error(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    arca.rechazar = True
    with pytest.raises(ComprobanteRechazado):
        emitir(historial, arca, comprobante)
    reg = historial.por_clave("k1")
    assert reg is not None and reg.estado == RECHAZADO and "10242" in reg.error
    # Corregido el problema, la misma clave puede reintentarse.
    arca.rechazar = False
    assert emitir(historial, arca, comprobante).registro.estado == AUTORIZADO


def test_si_falla_antes_de_enviar_no_queda_pendiente(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    arca.sin_conexion = True
    with pytest.raises(httpx.ConnectError):
        emitir(historial, arca, comprobante)
    assert historial.sin_resolver(comprobante.perfil.cuit, AMB) == []
    assert arca.envios == 0


def test_antes_de_numerar_se_resuelven_los_pendientes_anteriores(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    arca.autorizados[1] = comprobante
    viejo = historial.crear(
        perfil="default", ambiente=AMB, c=comprobante, clave="viejo", estado=INCIERTO, numero=1
    )
    otro = replace(comprobante, fecha=date(2026, 10, 11))
    emitir(historial, arca, otro, "nuevo")
    resuelto = historial.por_id(viejo.id)
    assert resuelto is not None and resuelto.estado == AUTORIZADO
    assert sorted(arca.autorizados) == [1, 2]


def test_nota_de_credito_anula_la_factura(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    factura = emitir(historial, arca, comprobante).registro
    nc = service.nota_de_credito(historial, factura, "Error de facturación", date(2026, 10, 12))
    assert nc.cbte_tipo == 13 and nc.es_nota_de_credito
    assert nc.total == comprobante.total and nc.receptor == comprobante.receptor
    assert nc.cbte_asoc is not None
    assert (nc.cbte_asoc.tipo, nc.cbte_asoc.punto_venta, nc.cbte_asoc.numero) == (11, 2, 1)
    assert nc.vto_pago == date(2026, 10, 12)  # servicios: ARCA exige vencimiento de pago

    registro_nc = emitir(historial, arca, nc, "nc1").registro
    assert registro_nc.asociado_id == factura.id
    assert historial.notas_de_credito_de(factura.id) == [registro_nc]


def test_nota_de_credito_necesita_factura_autorizada_y_motivo(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    factura = emitir(historial, arca, comprobante).registro
    with pytest.raises(ValueError, match="motivo"):
        service.nota_de_credito(historial, factura, "  ")
    pendiente = historial.crear(
        perfil="default", ambiente=AMB, c=comprobante, clave="p", estado=INCIERTO, numero=9
    )
    with pytest.raises(ValueError, match="autorizado"):
        service.nota_de_credito(historial, pendiente, "x")


def test_aviso_de_posible_duplicado(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    cuit = comprobante.perfil.cuit
    assert historial.posibles_duplicados(cuit, AMB, comprobante) == []
    factura = emitir(historial, arca, comprobante).registro
    # mismo receptor + importe + período: avisa
    assert historial.posibles_duplicados(cuit, AMB, comprobante) == [factura]
    # otro período u otro importe: no avisa
    otro_mes = replace(comprobante, periodo_desde=date(2026, 9, 1), periodo_hasta=date(2026, 9, 30))
    assert historial.posibles_duplicados(cuit, AMB, otro_mes) == []
    # otro ambiente: no avisa (la homologación no se mezcla con producción)
    assert historial.posibles_duplicados(cuit, "prod", comprobante) == []
    # si ya se anuló con una nota de crédito, volver a emitirla no es un duplicado
    nc = service.nota_de_credito(historial, factura, "anulada")
    emitir(historial, arca, nc, "nc")
    assert historial.posibles_duplicados(cuit, AMB, comprobante) == []


def test_sin_aviso_de_duplicado_para_consumidor_final_sin_identificar(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    cf = replace(
        comprobante,
        concepto=Concepto.PRODUCTOS,
        periodo_desde=None,
        periodo_hasta=None,
        vto_pago=None,
        receptor=replace(comprobante.receptor, doc_tipo=99, doc_nro=0),
        items=(replace(comprobante.items[0], precio_unitario=Decimal("500")),),
    )
    emitir(historial, arca, cf, "t1")
    assert historial.posibles_duplicados(cf.perfil.cuit, AMB, cf) == []


def test_historial_busca_por_receptor_y_numero(
    historial: Historial, arca: ArcaFalso, comprobante: Comprobante
) -> None:
    emitir(historial, arca, comprobante)
    assert len(historial.listar(texto="FICTICIO")) == 1
    assert len(historial.listar(texto="00000001")) == 1
    assert historial.listar(texto="nadie") == []
    assert historial.listar(perfil="otro") == []
