"""Lógica de las tools del MCP (sin depender del SDK, para poder testearla directo).

Regla de oro: **ninguna tool emite sin un borrador previo, vigente y confirmado
explícitamente**. Preparar nunca toca ARCA salvo para consultar el próximo número. Toda respuesta
empieza indicando el ambiente.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import BaseModel

from factumono import config
from factumono.arca import ambientes
from factumono.arca.errors import ArcaError, EstadoIncierto, traducir_error_red
from factumono.contexto import Contexto
from factumono.contexto import construir as construir_contexto
from factumono.core import armado, importacion, money, service
from factumono.core import cuit as cuit_mod
from factumono.core.diagnostico import diagnosticar, esta_dado_de_baja
from factumono.core.fechas import mostrar as fecha_ar
from factumono.core.lectura import FacturaLeida, LecturaMisComprobantes, NoReconocido
from factumono.core.models import (
    MODOS_PERIODO,
    NOMBRES_COMPROBANTE,
    Cliente,
    Comprobante,
    DocTipo,
    Item,
    Receptor,
)
from factumono.core.preview import markdown_preview
from factumono.core.sincronia import brechas, recuperar_brechas, total_faltantes
from factumono.pdf.salida import guardar_pdf
from factumono.storage.borradores import EMITIDO, Borradores
from factumono.storage.clientes import ClienteNoEncontrado
from factumono.storage.historial import AUTORIZADO, Registro

_ABREV = {1: "FA", 3: "NCA", 6: "FB", 8: "NCB", 11: "FC", 13: "NCC"}
_ERRORES = (ArcaError, ValueError, config.ConfigError, ClienteNoEncontrado)


class ItemEntrada(BaseModel):
    """Un renglón del comprobante."""

    descripcion: str
    precio_unitario: str | float | int
    cantidad: str | float | int = 1
    unidad: str = "unidades"
    codigo: str = "1"


class ItemPlantilla(BaseModel):
    """Un ítem de la factura anterior, tal como lo leyó la IA."""

    descripcion: str
    precio_unitario: str | float | int
    cantidad: str | float | int = 1
    unidad: str = "unidades"
    codigo: str = "1"
    bonificacion_pct: str | float | int = 0


class PlantillaEntrada(BaseModel):
    """Datos de una factura anterior extraídos por la IA (cuando el PDF no se pudo leer solo)."""

    nombre: str
    items: list[ItemPlantilla]
    cuit: str | None = None
    dni: str | None = None
    condicion_iva: str = "Consumidor Final"
    domicilio: str = ""
    concepto: str | int = 2
    condicion_venta: str = "Contado"
    fecha: str | None = None  # AAAA-MM-DD
    periodo_desde: str | None = None
    periodo_hasta: str | None = None
    vto_pago: str | None = None


class ReceptorEntrada(BaseModel):
    """Receptor que no está guardado como cliente."""

    nombre: str = "Consumidor Final"
    cuit: str | None = None
    dni: str | None = None
    condicion_iva: str = "Consumidor Final"
    domicilio: str = ""


def _num(x: str | float | int) -> Decimal:
    return money.parsear(str(x))


def _corto(r: Registro) -> str:
    nro = f"{r.numero:08d}" if r.numero is not None else "--------"
    return f"{_ABREV.get(r.cbte_tipo, r.cbte_tipo)} {r.punto_venta:05d}-{nro}"


class Herramientas:
    def __init__(
        self,
        contexto: Callable[[str | None], Contexto] = construir_contexto,
        ahora: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._contexto = contexto
        self._ahora = ahora

    @property
    def hoy(self) -> date:
        return self._ahora().date()

    # --- utilidades ------------------------------------------------------------------------

    def _responder(self, ctx: Contexto, cuerpo: str) -> str:
        return f"{ctx.aviso_ambiente}\n\n{cuerpo}"

    def _fallo(self, e: Exception, ctx: Contexto | None = None) -> str:
        texto = traducir_error_red(e) if isinstance(e, httpx.HTTPError) else str(e)
        cuerpo = f"❌ {texto}"
        return self._responder(ctx, cuerpo) if ctx else f"{self._banner_activo()}\n\n{cuerpo}"

    @staticmethod
    def _banner_activo() -> str:
        """El aviso del ambiente activo, para las respuestas que no llegaron a tener contexto."""
        try:
            return ambientes.obtener(config.cargar().ambiente).banner
        except (ValueError, OSError):
            return ambientes.HOMO.banner

    def _buscar(
        self, ctx: Contexto, numero: int, punto_venta: int | None, tipo: int | None
    ) -> Registro:
        pv = punto_venta or ctx.perfil.punto_venta
        if not pv:
            raise ValueError("Falta el punto de venta del perfil.")
        t = tipo or ctx.perfil.cbte_tipo_default
        assert ctx.historial
        r = ctx.historial.por_numero(ctx.perfil.cuit, ctx.ambiente, pv, t, numero)
        if not r:
            raise ValueError(
                f"No encuentro el comprobante {_ABREV.get(t, t)} {pv:05d}-{numero:08d} autorizado "
                "en el historial de este ambiente."
            )
        return r

    def _preparar(
        self,
        ctx: Contexto,
        c: Comprobante,
        *,
        nota: str | None = None,
        avisos_extra: list[str] | None = None,
    ) -> str:
        assert ctx.historial
        numero = ctx.wsfe.proximo_numero(c.punto_venta, c.cbte_tipo)
        avisos = list(avisos_extra or [])
        # Los parecidos son solo informativos: no piden confirmación extra (un comercio puede
        # facturarle dos veces al mismo cliente el mismo día).
        info = [
            f"Ojo: ya emitiste {_corto(d)} del {fecha_ar(d.fecha)} por "
            f"$ {money.formatear_ar(d.importe)} a {d.receptor_nombre} con el mismo "
            "período. Es solo un aviso: mencionáselo al usuario."
            for d in ctx.posibles_duplicados(c)
        ]
        info += self._aviso_brecha(ctx)
        b = ctx.borradores.crear(
            perfil=ctx.perfil_nombre,
            ambiente=ctx.ambiente,
            c=c,
            avisos=avisos,
            requiere_extra=bool(avisos),
            ahora=self._ahora(),
        )
        texto = markdown_preview(
            c,
            "",  # el ambiente lo antepone `_responder`
            numero,
            borrador_id=b.id,
            vence=b.vence.strftime("%H:%M"),
            avisos=avisos,
            info=info,
            nota=nota,
        )
        return texto + (
            f'\n\nPara emitir: `emitir_comprobante` con `borrador_id="{b.id}"` y `confirmo=true`'
            + (", más `confirmo_duplicado=true`" if b.requiere_extra else "")
            + ", SOLO después de que el usuario vio este preview y dijo explícitamente que sí."
        )

    @staticmethod
    def _aviso_brecha(ctx: Contexto) -> list[str]:
        """Aviso si ARCA tiene comprobantes que este historial no (emitidos desde otra PC)."""
        try:
            n = total_faltantes(brechas(ctx))
        except (ArcaError, httpx.HTTPError):
            return []
        if not n:
            return []
        return [
            f"Hay {n} comprobante(s) emitidos fuera de esta PC que este historial no tiene. "
            "Se pueden traer de ARCA con `recuperar_comprobantes` (no emite nada)."
        ]

    def recuperar_comprobantes(self, perfil: str | None = None) -> str:
        """Completa el historial local con lo que ARCA tiene y falta acá. No emite nada."""
        ctx = None
        try:
            ctx = self._contexto(perfil)
            bs = brechas(ctx)
            if not bs:
                return self._responder(ctx, "El historial está completo: no falta nada.")
            recuperados, no_encontrados = recuperar_brechas(ctx, bs)
            lineas = [f"✅ Recuperé {len(recuperados)} comprobante(s) de ARCA:"]
            lineas += [f"- {_corto(r)} · $ {money.formatear_ar(r.importe)}" for r in recuperados]
            if no_encontrados:
                lineas.append(f"- ⚠️ ARCA no devolvió los números {no_encontrados}.")
            lineas.append(
                "ℹ️ ARCA solo guarda los totales: ítems y datos del receptor quedan genéricos."
            )
            return self._responder(ctx, "\n".join(lineas))
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    # --- consultas -------------------------------------------------------------------------

    def estado_arca(self, perfil: str | None = None) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            d = diagnosticar(ctx)
            lineas = [
                f"- **Perfil:** {ctx.perfil.razon_social} — CUIT "
                f"{cuit_mod.formatear(ctx.perfil.cuit)} ({ctx.perfil.condicion_iva})",
                f"- **Servidores ARCA:** {'✅ OK' if d.dummy_ok else '❌ ' + str(d.dummy or '')}",
            ]
            if d.ticket_vence:
                lineas.append(
                    f"- **Autenticación:** ✅ OK (ticket vigente hasta "
                    f"{d.ticket_vence:%d/%m/%Y %H:%M})"
                )
            lineas.append(
                f"- **Punto de venta configurado:** {ctx.perfil.punto_venta or 'sin definir'}"
            )
            if d.pvs:
                lineas.append("- **Puntos de venta (web services):**")
                for pv in d.pvs:
                    extra = (" · BLOQUEADO" if pv.bloqueado else "") + (
                        f" · baja {pv.fecha_baja}" if esta_dado_de_baja(pv) else ""
                    )
                    marca = " ← **configurado**" if pv.nro == d.pv_configurado else ""
                    tipo = pv.emision_tipo or "sin tipo"
                    lineas.append(f"  - {pv.nro:05d} · {tipo}{extra}{marca}")
            elif d.ticket_vence and not ctx.es_produccion:
                lineas.append(
                    "- ℹ️ Sin puntos de venta listados (normal en homologación: no impide emitir)."
                )
            lineas += [f"- ⚠️ {a}" for a in d.avisos]
            if d.ticket_vence:
                lineas += [f"- ℹ️ {a}" for a in self._aviso_brecha(ctx)]
            lineas += [f"- ❌ {p}" for p in d.problemas]
            return self._responder(ctx, "\n".join(lineas))
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    def listar_perfiles(self) -> str:
        cfg = config.cargar()
        banner = ambientes.obtener(cfg.ambiente).banner
        if not cfg.perfiles:
            return f"{banner}\n\n❌ No hay perfiles configurados. Corré `factumono init`."
        filas = [
            banner,
            "",
            "| Perfil | Razón social | CUIT | Condición IVA | Pto. vta. | Ambiente |",
            "|--|--|--|--|--|--|",
        ]
        for nombre, p in cfg.perfiles.items():
            amb = ambientes.obtener(cfg.ambiente).titulo.lower()
            if cfg.ambiente == "prod":
                pv = p.punto_venta_prod
                amb += "" if p.credencial_prod else " (sin configurar)"
            else:
                pv = p.punto_venta
            filas.append(
                f"| {nombre}{' (principal)' if nombre == cfg.perfil_default else ''} | "
                f"{p.razon_social} | {cuit_mod.formatear(p.cuit)} | {p.condicion_iva} | "
                f"{pv or '—'} | {amb} |"
            )
        return "\n".join(filas)

    def listar_clientes(self, perfil: str | None = None) -> str:
        try:
            ctx = self._contexto(perfil)
            clientes = ctx.clientes.listar(ctx.perfil_nombre)
            if not clientes:
                return self._responder(
                    ctx, "No hay clientes guardados. Se agregan con `guardar_cliente`."
                )
            filas = [
                "| Alias | Nombre | Documento | Condición IVA | Habitual |",
                "|--|--|--|--|--|",
            ]
            for c in clientes:
                doc = (
                    cuit_mod.formatear(str(c.doc_nro).zfill(11))
                    if c.doc_tipo == 80
                    else f"DNI {c.doc_nro}"
                )
                filas.append(
                    f"| {c.alias} | {c.nombre} | {doc} | {c.condicion_iva} | "
                    f"{c.descripcion or '—'} ({c.unidad}, {c.concepto}, "
                    f"período: {MODOS_PERIODO.get(c.modo_periodo, c.modo_periodo).lower()}, "
                    f"vto. {c.vto_dias} días) |"
                )
            return self._responder(ctx, "\n".join(filas))
        except _ERRORES as e:
            return self._fallo(e)

    def guardar_cliente(
        self,
        alias: str,
        nombre: str,
        condicion_iva: str,
        cuit: str | None = None,
        dni: str | None = None,
        domicilio: str = "",
        email: str = "",
        descripcion: str = "",
        unidad: str = "otras unidades",
        concepto: str = "servicios",
        vto_dias: int = 4,
        reemplazar: bool = False,
        perfil: str | None = None,
        modo_periodo: str | None = None,
        condicion_venta: str | None = None,
    ) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            if cuit:
                tipo, nro = int(DocTipo.CUIT), int(cuit_mod.normalizar(cuit))
            elif dni:
                tipo, nro = int(DocTipo.DNI), int(dni)
            else:
                raise ValueError("Indicá el CUIT o el DNI del cliente.")
            ctx.wsfe.resolver_condicion_iva(condicion_iva, "C")  # valida la condición
            previo = ctx.clientes.buscar(ctx.perfil_nombre, alias) if reemplazar else None
            modo, conc = armado.validar_modo_y_concepto(
                modo_periodo or (previo.modo_periodo if previo else "mes"), concepto
            )
            ctx.clientes.guardar(
                ctx.perfil_nombre,
                Cliente(
                    alias,
                    nombre,
                    tipo,
                    nro,
                    condicion_iva,
                    domicilio,
                    email,
                    descripcion,
                    unidad,
                    conc,
                    vto_dias,
                    modo_periodo=modo,
                    condicion_venta=condicion_venta
                    or (previo.condicion_venta if previo else "Contado"),
                    plantilla=previo.plantilla if previo else "",  # no se pierde al reemplazar
                ),
                reemplazar=reemplazar,
            )
            return self._responder(ctx, f"✅ Cliente '{alias.lower()}' ({nombre}) guardado.")
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    def ultimo_comprobante(
        self, perfil: str | None = None, punto_venta: int | None = None, tipo: int | None = None
    ) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            pv = punto_venta or ctx.perfil.punto_venta
            if not pv:
                raise ValueError("Falta el punto de venta.")
            t = tipo or ctx.perfil.cbte_tipo_default
            n = ctx.wsfe.ultimo_autorizado(pv, t)
            nombre = NOMBRES_COMPROBANTE.get(t, f"tipo {t}")
            return self._responder(
                ctx, f"Último {nombre} autorizado en el punto de venta {pv:05d}: **Nro. {n:08d}**."
            )
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    def listar_comprobantes(
        self, perfil: str | None = None, limite: int = 5, buscar: str | None = None
    ) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            assert ctx.historial
            filas = [
                r
                for r in ctx.historial.listar(perfil=ctx.perfil_nombre, texto=buscar, limite=200)
                if r.ambiente == ctx.ambiente
            ][: max(1, min(limite, 50))]
            if not filas:
                return self._responder(ctx, "Todavía no hay comprobantes en el historial.")
            tabla = [
                "| Fecha | Comprobante | Receptor | Importe | Estado | CAE |",
                "|--|--|--|--:|--|--|",
            ]
            for r in filas:
                tabla.append(
                    f"| {fecha_ar(r.fecha)} | {_corto(r)} | {r.receptor_nombre} | "
                    f"$ {money.formatear_ar(r.importe)} | {r.estado} | {r.cae or '—'} |"
                )
            return self._responder(ctx, "\n".join(tabla))
        except _ERRORES as e:
            return self._fallo(e, ctx)

    def consultar_comprobante(
        self,
        numero: int,
        perfil: str | None = None,
        punto_venta: int | None = None,
        tipo: int | None = None,
    ) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            r = self._buscar(ctx, numero, punto_venta, tipo)
            c = r.comprobante
            lineas = [
                f"### {_corto(r)}",
                f"- **Estado:** {r.estado}",
                f"- **Fecha:** {fecha_ar(r.fecha)}",
                f"- **Receptor:** {r.receptor_nombre} ({c.receptor.condicion_iva})",
                f"- **Importe:** $ {money.formatear_ar(r.importe)}",
                f"- **CAE:** {r.cae} (vence {fecha_ar(r.cae_vto)})",
            ]
            if r.periodo_desde and r.periodo_hasta:
                lineas.append(
                    f"- **Período:** {fecha_ar(r.periodo_desde)} al {fecha_ar(r.periodo_hasta)}"
                )
            for nc in ctx.historial.notas_de_credito_de(r.id) if ctx.historial else []:
                lineas.append(f"- ⚠️ Anulada por la {_corto(nc)} del {fecha_ar(nc.fecha)}.")
            try:
                en_arca = ctx.wsfe.consultar(r.punto_venta, r.cbte_tipo, numero)
                lineas.append(
                    "- **En ARCA:** ✅ confirmado"
                    if en_arca and en_arca.cae == r.cae
                    else "- **En ARCA:** ❌ no coincide con lo registrado"
                )
            except (ArcaError, httpx.HTTPError):
                lineas.append("- **En ARCA:** no se pudo verificar ahora (sin conexión).")
            return self._responder(ctx, "\n".join(lineas))
        except _ERRORES as e:
            return self._fallo(e, ctx)

    def reimprimir_pdf(
        self,
        numero: int,
        perfil: str | None = None,
        punto_venta: int | None = None,
        tipo: int | None = None,
    ) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            r = self._buscar(ctx, numero, punto_venta, tipo)
            ruta = guardar_pdf(config.cargar(), r)
            return self._responder(ctx, f"✅ PDF de {_corto(r)} generado en:\n`{ruta}`")
        except _ERRORES as e:
            return self._fallo(e, ctx)

    def resumen_facturacion(
        self, perfil: str | None = None, desde: str | None = None, hasta: str | None = None
    ) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            assert ctx.historial
            hoy = self.hoy
            if bool(desde) != bool(hasta):
                raise ValueError("Indicá las dos fechas (desde y hasta), o ninguna.")
            if desde and hasta:
                d, h = armado.parsear_fecha(desde), armado.parsear_fecha(hasta)
            else:
                d, h = date(hoy.year, 1, 1), hoy
            nf, tf, nn, tn = ctx.historial.resumen(ctx.perfil.cuit, ctx.ambiente, d, h)
            lineas = [
                f"**{ctx.perfil.razon_social}** — del {fecha_ar(d)} al {fecha_ar(h)}",
                f"- Facturas emitidas: {nf} por **$ {money.formatear_ar(tf)}**",
                f"- Notas de crédito: {nn} por $ {money.formatear_ar(tn)}",
                f"- **Facturado neto: $ {money.formatear_ar(tf - tn)}**",
            ]
            if "monotribut" in ctx.perfil.condicion_iva.lower():
                inicio12 = armado.sumar_mes(hoy.replace(day=1), -11)
                nf2, tf2, _, tn2 = ctx.historial.resumen(
                    ctx.perfil.cuit, ctx.ambiente, inicio12, hoy
                )
                lineas.append(
                    f"- Últimos 12 meses ({fecha_ar(inicio12)} al {fecha_ar(hoy)}): "
                    f"$ {money.formatear_ar(tf2 - tn2)} netos en {nf2} facturas "
                    "(para comparar con el tope de tu categoría; la comparación automática "
                    "llega más adelante)."
                )
            if not ctx.es_produccion:
                lineas.append(
                    "\nℹ️ Son comprobantes de **homologación** (pruebas): no son ventas reales."
                )
            return self._responder(ctx, "\n".join(lineas))
        except _ERRORES as e:
            return self._fallo(e, ctx)

    # --- preparar --------------------------------------------------------------------------

    def preparar_comprobante(
        self,
        perfil: str | None = None,
        cliente: str | None = None,
        receptor: ReceptorEntrada | None = None,
        tipo: int | None = None,
        concepto: str | None = None,
        items: list[ItemEntrada] | None = None,
        importe: str | float | int | None = None,
        descripcion: str | None = None,
        periodo: str | None = None,
        vto_pago: str | None = None,
        repetir_ultima: bool = False,
        condicion_venta: str = "Contado",
        modo_periodo: str | None = None,
    ) -> str:
        ctx = None
        try:
            ctx = self._contexto(perfil)
            hoy = self.hoy
            pv = ctx.perfil.punto_venta
            if not pv:
                raise ValueError(
                    "El perfil no tiene punto de venta: `factumono init --punto-venta N`."
                )
            t = tipo or ctx.perfil.cbte_tipo_default
            if t != ctx.perfil.cbte_tipo_default:
                raise ValueError("Por ahora solo se emite Factura C.")
            desde = hasta = None
            if periodo:
                desde, hasta = armado.parsear_periodo(periodo, hoy)
            vto = armado.parsear_fecha(vto_pago) if vto_pago else None
            nota = None
            if cliente:
                assert ctx.historial
                cli = ctx.clientes.obtener(ctx.perfil_nombre, cliente)
                if repetir_ultima:
                    ultima = armado.ultima_factura(
                        ctx.historial, ctx.perfil.cuit, ctx.ambiente, cli
                    )
                    c = armado.repetir_ultima(
                        ultima,
                        hoy=hoy,
                        importe=_num(importe) if importe is not None else None,
                        desde=desde,
                        hasta=hasta,
                        vto_dias=cli.vto_dias,
                        modo=modo_periodo or cli.modo_periodo,
                    )
                    if vto:
                        c = replace(c, vto_pago=vto)
                    nota = (
                        f"Es la de siempre: repite la {_corto(ultima)} "
                        f"(período {fecha_ar(ultima.periodo_desde)} al "
                        f"{fecha_ar(ultima.periodo_hasta)})"
                        + (
                            f" y la corre al período {fecha_ar(c.periodo_desde)} al "
                            f"{fecha_ar(c.periodo_hasta)}."
                            if c.periodo_desde
                            else "."
                        )
                    )
                else:
                    rec = armado.receptor_de_cliente(ctx.wsfe, cli)
                    if items:
                        its = tuple(_item(i) for i in items)
                    elif importe is not None:
                        its = (armado.item_de_cliente(cli, _num(importe), descripcion),)
                    elif armado.items_de_plantilla(cli):
                        its = armado.items_de_plantilla(cli)
                        nota = f"Uso la plantilla de «{cli.alias}» ({len(its)} ítem/s)."
                    else:
                        raise ValueError("Falta el importe (o los ítems) a facturar.")
                    c = armado.armar_comprobante(
                        perfil=ctx.perfil,
                        punto_venta=pv,
                        cbte_tipo=t,
                        receptor=rec,
                        items=its,
                        concepto=concepto or cli.concepto,
                        hoy=hoy,
                        desde=desde,
                        hasta=hasta,
                        vto_pago=vto,
                        vto_dias=cli.vto_dias,
                        condicion_venta=condicion_venta,
                        modo_periodo=modo_periodo or cli.modo_periodo,
                    )
            else:
                if repetir_ultima:
                    raise ValueError("Para repetir la última hace falta indicar el cliente.")
                if not items:
                    raise ValueError("Falta el cliente guardado o los ítems a facturar.")
                r = receptor or ReceptorEntrada()
                cond = ctx.wsfe.resolver_condicion_iva(r.condicion_iva, "C")
                if r.cuit:
                    td, tn = int(DocTipo.CUIT), int(cuit_mod.normalizar(r.cuit))
                elif r.dni:
                    td, tn = int(DocTipo.DNI), int(r.dni)
                else:
                    td, tn = int(DocTipo.CONSUMIDOR_FINAL), 0
                c = armado.armar_comprobante(
                    perfil=ctx.perfil,
                    punto_venta=pv,
                    cbte_tipo=t,
                    receptor=Receptor(td, tn, r.nombre, cond.id, cond.descripcion, r.domicilio),
                    items=tuple(_item(i) for i in items),
                    concepto=concepto or "productos",
                    hoy=hoy,
                    desde=desde,
                    hasta=hasta,
                    vto_pago=vto,
                    condicion_venta=condicion_venta,
                    modo_periodo=modo_periodo or "mes",
                )
            return self._responder(ctx, self._preparar(ctx, c, nota=nota))
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    def nota_de_credito(
        self,
        comprobante_original: int,
        motivo: str,
        perfil: str | None = None,
        punto_venta: int | None = None,
        tipo: int | None = None,
    ) -> str:
        """Prepara (no emite) la nota de crédito que anula por completo una factura."""
        ctx = None
        try:
            ctx = self._contexto(perfil)
            assert ctx.historial
            original = self._buscar(ctx, comprobante_original, punto_venta, tipo)
            nc = service.nota_de_credito(ctx.historial, original, motivo, self.hoy)
            previas = ctx.historial.notas_de_credito_de(original.id)
            extra = [
                f"ESTA FACTURA YA FUE ANULADA por la {_corto(p)} del {fecha_ar(p.fecha)}. "
                "Confirmá con el usuario que quiere otra nota de crédito."
                for p in previas
            ]
            return self._responder(ctx, self._preparar(ctx, nc, avisos_extra=extra))
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    # --- importar facturas anteriores (siempre con preview y confirmación) ---------------------

    _TEXTO_CRUDO_MAX = 6000

    def _pendiente_de_importacion(
        self,
        ctx: Contexto,
        lecturas: list[FacturaLeida | NoReconocido],
        mis: LecturaMisComprobantes | None,
    ) -> str:
        plan = importacion.planear(ctx, lecturas, mis)
        cuerpo = [plan.texto()]
        for n in plan.no_reconocidos:
            crudo = (n.texto or "(sin texto: es una imagen)")[: self._TEXTO_CRUDO_MAX]
            cuerpo.append(
                f"\n**No reconocido: {n.archivo}** — {n.motivo}\n"
                "Interpretá vos el texto y cargalo con `guardar_plantilla`:\n"
                f"```\n{crudo}\n```"
            )
        if not plan.hay_algo_para_guardar:
            cuerpo.append("\nNo hay nada para guardar todavía.")
            return "\n".join(cuerpo)
        p = ctx.importaciones.crear(
            ctx.perfil_nombre, ctx.ambiente, importacion.a_payload(lecturas, mis), self._ahora()
        )
        cuerpo.append(
            f"\nTodavía NO se guardó nada. Mostrale esto al usuario y, solo si confirma, llamá a "
            f'`confirmar_importacion` con `importacion_id="{p.id}"` y `confirmo=true` '
            f"(vence a las {p.vence:%H:%M})."
        )
        return "\n".join(cuerpo)

    def importar_factura(self, ruta: str, perfil: str | None = None) -> str:
        """Lee una factura anterior (PDF de ARCA o de Factumono, carpeta, o Excel/CSV de «Mis
        Comprobantes») de forma determinística y prepara qué se guardaría. No guarda nada."""
        ctx = None
        try:
            ctx = self._contexto(perfil)
            lecturas, mis = importacion.leer_rutas([Path(ruta).expanduser()])
            return self._responder(ctx, self._pendiente_de_importacion(ctx, lecturas, mis))
        except (*_ERRORES, httpx.HTTPError, OSError) as e:
            return self._fallo(e, ctx)

    def guardar_plantilla(self, datos: PlantillaEntrada, perfil: str | None = None) -> str:
        """Prepara guardar como cliente + plantilla los datos que la IA extrajo de una factura
        que no se pudo leer sola. No guarda nada hasta `confirmar_importacion`."""
        ctx = None
        try:
            ctx = self._contexto(perfil)
            factura = importacion.factura_de_datos(datos.model_dump())
            return self._responder(ctx, self._pendiente_de_importacion(ctx, [factura], None))
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    def confirmar_importacion(
        self, importacion_id: str, confirmo: bool = False, perfil: str | None = None
    ) -> str:
        """Guarda lo que `importar_factura` / `guardar_plantilla` prepararon. Exige `confirmo=true`
        (solo si el usuario vio el preview y dijo que sí)."""
        ctx = None
        try:
            ctx = self._contexto(perfil)
            if confirmo is not True:
                return self._responder(
                    ctx,
                    "🛑 No se guardó nada: falta la confirmación explícita del usuario "
                    "(`confirmo=true`). Mostrale el preview y preguntale.",
                )
            p = ctx.importaciones.obtener(importacion_id)
            if p is None:
                raise ValueError(f"No existe la importación {importacion_id}. Prepará una nueva.")
            if p.vencida(self._ahora()):
                raise ValueError("La importación venció (duran 15 minutos). Prepará una nueva.")
            if p.ambiente != ctx.ambiente or p.perfil != ctx.perfil_nombre:
                raise ValueError("Cambió el perfil o el ambiente: prepará la importación de nuevo.")
            lecturas, mis = importacion.de_payload(p.datos)
            plan = importacion.planear(ctx, lecturas, mis)
            res = importacion.aplicar(ctx, plan)
            ctx.importaciones.borrar(p.id)
            return self._responder(ctx, f"✅ Listo: {res.texto()}.")
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    # --- emitir (único punto de emisión) ---------------------------------------------------

    def emitir_comprobante(
        self, borrador_id: str, confirmo: bool = False, confirmo_duplicado: bool = False
    ) -> str:
        ctx = None
        try:
            b = Borradores().obtener(borrador_id)
            if b is None:
                return self._fallo(
                    ValueError(
                        f"No existe el borrador {borrador_id}. Preparalo de nuevo con "
                        "`preparar_comprobante`."
                    )
                )
            ctx = self._contexto(b.perfil)
            if ctx.ambiente != b.ambiente:
                raise ValueError(
                    "El ambiente del perfil cambió desde que se preparó el borrador: "
                    "prepará uno nuevo."
                )
            if confirmo is not True:
                return self._responder(
                    ctx,
                    "🛑 No se emitió nada: falta la confirmación explícita del usuario "
                    "(`confirmo=true`). Mostrale el preview y preguntale si lo emito.",
                )
            if b.estado != EMITIDO:
                if b.vencido(self._ahora()):
                    raise ValueError(
                        f"El borrador {b.id} venció (los borradores duran 15 minutos). "
                        "Preparalo de nuevo."
                    )
                if b.requiere_extra and confirmo_duplicado is not True:
                    return self._responder(
                        ctx,
                        "🛑 No se emitió nada: este borrador tiene avisos que piden una "
                        "confirmación extra (`confirmo_duplicado=true`):\n\n"
                        + "\n".join(f"- ⚠️ {a}" for a in b.avisos),
                    )
            assert ctx.historial
            em = service.emitir(
                ctx.historial,
                ctx.wsfe,
                perfil=b.perfil,
                ambiente=b.ambiente,
                c=b.comprobante,
                clave=f"mcp-{b.id}",
            )
            ctx.borradores.marcar_emitido(b.id)
            return self._responder(ctx, self._resultado(em))
        except EstadoIncierto as e:
            return self._fallo(e, ctx)
        except (*_ERRORES, httpx.HTTPError) as e:
            return self._fallo(e, ctx)

    def _resultado(self, em: service.Emision) -> str:
        r = em.registro
        assert r.numero is not None and r.estado == AUTORIZADO
        lineas = [
            f"✅ **{NOMBRES_COMPROBANTE.get(r.cbte_tipo, 'Comprobante')} autorizada** — "
            f"{r.punto_venta:05d}-{r.numero:08d}",
            f"- **Receptor:** {r.receptor_nombre}",
            f"- **Importe:** $ {money.formatear_ar(r.importe)}",
            f"- **CAE:** {r.cae} (vence {fecha_ar(r.cae_vto)})",
        ]
        if em.reutilizado:
            lineas.append("- ℹ️ Este borrador ya se había emitido: no se emitió otro comprobante.")
        if em.reconciliado:
            lineas.append(
                "- ✅ Se cortó la comunicación, pero ARCA confirmó que quedó autorizado "
                "(no se emitió dos veces)."
            )
        lineas += [f"- ⚠️ {o}" for o in r.observaciones]
        try:
            lineas.append(f"- **PDF:** `{guardar_pdf(config.cargar(), r)}`")
        except Exception as e:
            lineas.append(f"- ⚠️ No se pudo generar el PDF ({e}). Probá con `reimprimir_pdf`.")
        return "\n".join(lineas)


def _item(i: ItemEntrada) -> Item:
    return Item(
        descripcion=i.descripcion,
        cantidad=_num(i.cantidad),
        precio_unitario=_num(i.precio_unitario),
        unidad=i.unidad,
        codigo=i.codigo,
    )
