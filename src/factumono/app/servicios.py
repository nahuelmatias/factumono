"""Lógica de la GUI (sin depender de FastAPI, para probarla directo).

Reutiliza el mismo motor que la CLI y el MCP: `Contexto`, `armado`, `service.emitir`, el historial
y los borradores. Regla de oro (igual que en el MCP): **nada se emite sin un borrador preparado,
vigente y confirmado** con el botón «Emitir» después de ver el preview.
"""

import contextlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from factumono import config, migracion
from factumono.arca import ambientes
from factumono.arca.csr import carpeta_certificados, generar_clave_y_csr
from factumono.cli import asistente as asis
from factumono.contexto import Contexto, construir
from factumono.core import armado, money, service, topes
from factumono.core import cuit as cuit_mod
from factumono.core.diagnostico import Diagnostico, diagnosticar, problemas_para_activar_produccion
from factumono.core.fechas import mostrar as fecha_ar
from factumono.core.models import (
    NOMBRES_COMPROBANTE,
    NOMBRES_CONCEPTO,
    Cliente,
    Comprobante,
    Concepto,
    DocTipo,
    Item,
    Receptor,
    parsear_concepto,
    parsear_modo_periodo,
)
from factumono.core.sincronia import Brecha, brechas, recuperar_brechas, total_faltantes
from factumono.mcp import instalar as mcp_instalar
from factumono.pdf.salida import guardar_pdf, ruta_pdf
from factumono.storage import backup as backup_mod
from factumono.storage.borradores import Borrador
from factumono.storage.clientes import ClienteNoEncontrado
from factumono.storage.historial import AUTORIZADO, Registro

CONDICIONES_RECEPTOR = (
    "IVA Responsable Inscripto",
    "Consumidor Final",
    "Responsable Monotributo",
    "IVA Sujeto Exento",
)
CONSUMIDOR_FINAL = "__consumidor_final__"
_ABREV = {1: "FA", 3: "NCA", 6: "FB", 8: "NCB", 11: "FC", 13: "NCC"}


class ErrorUsuario(ValueError):
    """Un error que se le muestra tal cual a la persona (ya está en español)."""


def numero_corto(r: Registro) -> str:
    nro = f"{r.numero:08d}" if r.numero is not None else "--------"
    return f"{_ABREV.get(r.cbte_tipo, r.cbte_tipo)} {r.punto_venta:05d}-{nro}"


def pesos(x: Decimal) -> str:
    return "$ " + money.formatear_ar(x)


# --- vistas (lo que usan las plantillas) ------------------------------------------------------


@dataclass(frozen=True)
class Resumen:
    ambiente: str
    es_produccion: bool
    banner: str
    razon_social: str
    cuit: str
    condicion_iva: str
    punto_venta: int | None


@dataclass
class BarraTope:
    categoria: str
    tope: str
    facturado: str
    disponible: str
    porcentaje: int  # 0-100 (la barra no se pasa)
    porcentaje_real: str
    nivel: str
    desde: str
    hasta: str
    vigencia: str
    fuente: str
    desactualizado: bool
    sin_historial_completo: bool = False


@dataclass
class VistaPreview:
    borrador_id: str
    vence: str
    titulo: str
    numero: str
    emisor: str
    receptor: str
    receptor_doc: str
    condicion_iva: str
    concepto: str
    fecha: str
    periodo: str | None
    vto_pago: str | None
    items: list[dict[str, str]]
    total: str
    avisos: list[str]
    info: list[str]
    requiere_extra: bool
    anula: str | None
    motivo: str | None
    es_produccion: bool


@dataclass
class Emitido:
    registro: Registro
    titulo: str
    numero: str
    cae: str
    cae_vto: str
    receptor: str
    importe: str
    pdf: Path | None
    aviso_pdf: str | None
    observaciones: list[str]
    reutilizado: bool
    es_homologacion: bool


@dataclass
class FilaFactura:
    id: int
    numero: str
    fecha: str
    receptor: str
    importe: str
    estado: str
    cae: str
    es_nc: bool
    anulada: bool
    homologacion: bool


@dataclass
class EstadoWizard:
    ambiente: str
    paso: int
    avance: asis.Avance
    csr_texto: str = ""
    problemas: list[str] = field(default_factory=list)


@dataclass
class ResultadoAjustes:
    mensaje: str
    ok: bool = True


class Servicios:
    def __init__(
        self,
        contexto: Callable[[str | None, str | None], Contexto] = construir,
        ahora: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._contexto = contexto
        self._ahora = ahora
        self._cache: tuple[tuple[str, float], Contexto] | None = None

    @property
    def hoy(self) -> date:
        return self._ahora().date()

    # --- contexto y estado general ---------------------------------------------------------

    def configurado(self) -> bool:
        cfg = config.cargar()
        if not cfg.perfiles:
            return False
        try:
            cfg.perfil_para(None, cfg.ambiente)
        except config.ConfigError:
            return False
        return True

    def ctx(self) -> Contexto:
        """El contexto del ambiente activo. Se reutiliza mientras no cambie la config (así no se
        abren clientes HTTP nuevos en cada pedido)."""
        ruta = config.ruta_config()
        clave = (config.cargar().ambiente, ruta.stat().st_mtime if ruta.exists() else 0.0)
        if self._cache and self._cache[0] == clave:
            return self._cache[1]
        try:
            ctx = self._contexto(None, None)
        except config.ConfigError as e:
            raise ErrorUsuario(str(e)) from None
        self._cache = (clave, ctx)
        return ctx

    def resumen(self) -> Resumen:
        ctx = self.ctx()
        p = ctx.perfil
        return Resumen(
            ambiente=ctx.nombre_ambiente,
            es_produccion=ctx.es_produccion,
            banner=ctx.aviso_ambiente,
            razon_social=p.nombre_fantasia or p.razon_social,
            cuit=cuit_mod.formatear(p.cuit),
            condicion_iva=p.condicion_iva,
            punto_venta=p.punto_venta,
        )

    def banner(self) -> tuple[str, bool]:
        """(texto, es_producción) del ambiente activo, aunque todavía no haya perfil."""
        cfg = config.cargar()
        amb = ambientes.obtener(cfg.ambiente)
        return amb.banner, amb.es_produccion

    def barra_tope(self) -> BarraTope | None:
        ctx = self.ctx()
        cat = ctx.perfil.categoria_monotributo
        if not cat or "monotribut" not in ctx.perfil.condicion_iva.lower():
            return None
        assert ctx.historial
        t = topes.cargar()
        hoy = self.hoy
        desde = armado.sumar_mes(hoy.replace(day=1), -11)
        _, tf, _, tn = ctx.historial.resumen(ctx.perfil.cuit, ctx.ambiente, desde, hoy)
        av = topes.avance(t, cat, tf - tn, desde, hoy)
        return BarraTope(
            categoria=av.categoria,
            tope=pesos(av.tope),
            facturado=pesos(av.facturado),
            disponible=pesos(av.disponible) if av.disponible >= 0 else "—",
            porcentaje=int(min(av.porcentaje, Decimal(100))),
            porcentaje_real=f"{av.porcentaje:.1f}".replace(".", ","),
            nivel=av.nivel,
            desde=fecha_ar(av.desde),
            hasta=fecha_ar(av.hasta),
            vigencia=fecha_ar(t.vigencia_desde),
            fuente=t.fuente,
            desactualizado=t.desactualizados(hoy),
            sin_historial_completo=not ctx.es_produccion,
        )

    def aviso_migracion(self) -> str | None:
        """El aviso de que se migraron los datos del nombre anterior (se muestra una sola vez)."""
        datos = config.directorio_datos()
        aviso = migracion.aviso_pendiente(datos)
        if aviso:
            migracion.marcar_avisado(datos)
        return aviso

    def aviso_brecha(self) -> tuple[int, list[Brecha]]:
        """Cuántos comprobantes tiene ARCA que este historial no (otra PC). 0 si no se pudo ver."""
        try:
            bs = brechas(self.ctx())
        except Exception:
            return 0, []
        return total_faltantes(bs), bs

    def recuperar(self) -> str:
        ctx = self.ctx()
        bs = brechas(ctx)
        if not bs:
            return "El historial ya está completo."
        recuperados, no = recuperar_brechas(ctx, bs)
        texto = f"Se trajeron {len(recuperados)} comprobante(s) de ARCA."
        if no:
            texto += f" ARCA no devolvió los números {no}."
        return texto

    def estado_arca(self) -> Diagnostico:
        return diagnosticar(self.ctx())

    # --- clientes --------------------------------------------------------------------------

    def clientes(self) -> list[Cliente]:
        ctx = self.ctx()
        return ctx.clientes.listar(ctx.perfil_nombre)

    def cliente(self, alias: str) -> Cliente | None:
        ctx = self.ctx()
        return ctx.clientes.buscar(ctx.perfil_nombre, alias)

    def guardar_cliente(
        self,
        *,
        alias: str,
        nombre: str,
        documento: str,
        condicion_iva: str,
        domicilio: str = "",
        email: str = "",
        descripcion: str = "",
        unidad: str = "otras unidades",
        concepto: str = "2",
        vto_dias: int = 4,
        reemplazar: bool = False,
        modo_periodo: str = "mes",
        condicion_venta: str = "Contado",
    ) -> Cliente:
        ctx = self.ctx()
        alias = alias.strip() or nombre.strip().split(" ")[0]
        if not nombre.strip():
            raise ErrorUsuario("Falta el nombre del cliente.")
        doc = "".join(c for c in documento if c.isdigit())
        if len(doc) == 11:
            try:
                tipo, nro = int(DocTipo.CUIT), int(cuit_mod.normalizar(doc))
            except ValueError as e:
                raise ErrorUsuario(str(e)) from None
        elif 6 <= len(doc) <= 9:
            tipo, nro = int(DocTipo.DNI), int(doc)
        else:
            raise ErrorUsuario("Poné un CUIT (11 números) o un DNI (7 u 8 números).")
        if condicion_iva not in CONDICIONES_RECEPTOR:
            raise ErrorUsuario("Elegí la condición frente al IVA del cliente.")
        try:
            modo, conc = armado.validar_modo_y_concepto(modo_periodo, concepto)
        except ValueError as e:
            raise ErrorUsuario(str(e)) from None
        previo = ctx.clientes.buscar(ctx.perfil_nombre, alias) if reemplazar else None
        c = Cliente(
            alias=alias,
            nombre=nombre.strip(),
            doc_tipo=tipo,
            doc_nro=nro,
            condicion_iva=condicion_iva,
            domicilio=domicilio.strip(),
            email=email.strip(),
            descripcion=descripcion.strip(),
            unidad=unidad.strip() or "otras unidades",
            concepto=conc,
            vto_dias=vto_dias,
            modo_periodo=modo,
            condicion_venta=condicion_venta.strip() or "Contado",
            plantilla=previo.plantilla if previo else "",
        )
        ctx.clientes.guardar(ctx.perfil_nombre, c, reemplazar=reemplazar)
        return c

    def borrar_cliente(self, alias: str) -> None:
        ctx = self.ctx()
        ctx.clientes.borrar(ctx.perfil_nombre, alias)

    # --- facturas --------------------------------------------------------------------------

    def listar(self, buscar: str | None = None, limite: int = 50) -> list[FilaFactura]:
        ctx = self.ctx()
        assert ctx.historial
        filas = []
        for r in ctx.historial.listar(perfil=ctx.perfil_nombre, texto=buscar or None, limite=200):
            if r.ambiente != ctx.ambiente or r.estado != AUTORIZADO:
                continue
            es_nc = r.comprobante.es_nota_de_credito
            filas.append(
                FilaFactura(
                    id=r.id,
                    numero=numero_corto(r),
                    fecha=fecha_ar(r.fecha),
                    receptor=r.receptor_nombre,
                    importe=pesos(r.importe),
                    estado=r.estado,
                    cae=r.cae,
                    es_nc=es_nc,
                    anulada=(not es_nc) and bool(ctx.historial.notas_de_credito_de(r.id)),
                    homologacion=r.es_homologacion,
                )
            )
        return filas[:limite]

    def registro(self, id_: int) -> Registro:
        ctx = self.ctx()
        assert ctx.historial
        r = ctx.historial.por_id(id_)
        if r is None or r.ambiente != ctx.ambiente:
            raise ErrorUsuario("No encuentro ese comprobante en este ambiente.")
        return r

    def pdf(self, id_: int) -> Path:
        r = self.registro(id_)
        ruta = ruta_pdf(config.cargar(), r)
        if not ruta.is_file():
            ruta = guardar_pdf(config.cargar(), r)
        return ruta

    def carpeta_del_pdf(self, id_: int) -> Path:
        return self.pdf(id_).parent

    def mailto(self, id_: int) -> str | None:
        """Enlace para escribir el mail (el PDF hay que adjuntarlo a mano: se abre su carpeta)."""
        r = self.registro(id_)
        ctx = self.ctx()
        try:
            cli = ctx.clientes.obtener(ctx.perfil_nombre, r.receptor_nombre)
        except ClienteNoEncontrado:
            return None
        if not cli.email:
            return None
        asunto = f"{NOMBRES_COMPROBANTE.get(r.cbte_tipo, 'Comprobante')} {numero_corto(r)}"
        cuerpo = f"Hola, te envío adjunta la factura {numero_corto(r)} por {pesos(r.importe)}."
        return f"mailto:{cli.email}?subject={quote(asunto)}&body={quote(cuerpo)}"

    def abrir_en_el_explorador(self, id_: int) -> None:
        ruta = self.pdf(id_)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(ruta)])  # noqa: S603, S607
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(ruta)])  # noqa: S603, S607
        else:
            subprocess.Popen(["xdg-open", str(ruta.parent)])  # noqa: S603, S607

    # --- preparar / emitir -----------------------------------------------------------------

    def _ultimo_mes_completo(self) -> tuple[date, date]:
        primero = self.hoy.replace(day=1) - timedelta(days=1)
        return armado.mes_completo(primero.year, primero.month)

    def periodo_sugerido(self) -> str:
        d, _ = self._ultimo_mes_completo()
        return f"{d.year}-{d.month:02d}"

    def preparar_factura(
        self,
        *,
        cliente: str,
        importe: str,
        descripcion: str = "",
        periodo: str = "",
        vto_pago: str = "",
        repetir: bool = False,
        concepto: str = "",
        modo_periodo: str = "",
        desde: str = "",
        hasta: str = "",
    ) -> VistaPreview:
        ctx = self.ctx()
        assert ctx.historial
        hoy = self.hoy
        if not ctx.perfil.punto_venta:
            raise ErrorUsuario("Falta el punto de venta del perfil (Ajustes).")
        cli: Cliente | None = None
        if cliente != CONSUMIDOR_FINAL:
            cli = ctx.clientes.buscar(ctx.perfil_nombre, cliente)
            if cli is None:
                raise ErrorUsuario("Elegí un cliente de la lista.")
        try:
            conc = parsear_concepto(concepto or (cli.concepto if cli else "productos"))
            modo = parsear_modo_periodo(modo_periodo or (cli.modo_periodo if cli else "mes"))
            p_desde, p_hasta = self._periodo_del_formulario(modo, conc, periodo, desde, hasta, hoy)
        except ValueError as e:
            raise ErrorUsuario(str(e)) from None
        desde_f, hasta_f = p_desde, p_hasta
        vto = None
        if vto_pago.strip():
            try:
                vto = armado.parsear_fecha(vto_pago)
            except ValueError as e:
                raise ErrorUsuario(str(e)) from None
        imp = None
        if importe.strip():
            try:
                imp = money.parsear(importe)
            except ValueError:
                raise ErrorUsuario("El importe no es válido. Escribilo así: 1.500,50") from None
        nota = None
        if cliente == CONSUMIDOR_FINAL:
            if imp is None or not descripcion.strip():
                raise ErrorUsuario("Falta el importe y qué se vendió.")
            cond = ctx.wsfe.resolver_condicion_iva("Consumidor Final", "C")
            c = armado.armar_comprobante(
                perfil=ctx.perfil,
                punto_venta=ctx.perfil.punto_venta,
                cbte_tipo=ctx.perfil.cbte_tipo_default,
                receptor=Receptor(
                    int(DocTipo.CONSUMIDOR_FINAL), 0, "Consumidor Final", cond.id, cond.descripcion
                ),
                items=(
                    Item(
                        descripcion=descripcion.strip(),
                        cantidad=Decimal(1),
                        precio_unitario=imp,
                        unidad="unidades",
                    ),
                ),
                concepto=conc,
                hoy=hoy,
                desde=desde_f,
                hasta=hasta_f,
                vto_pago=vto,
                modo_periodo=modo,
            )
        else:
            assert cli is not None
            if repetir:
                try:
                    ultima = armado.ultima_factura(
                        ctx.historial, ctx.perfil.cuit, ctx.ambiente, cli
                    )
                except ValueError as e:
                    raise ErrorUsuario(str(e)) from None
                c = armado.repetir_ultima(
                    ultima,
                    hoy=hoy,
                    importe=imp,
                    desde=desde_f,
                    hasta=hasta_f,
                    vto_dias=cli.vto_dias,
                    modo=modo,
                )
                if vto:
                    c = replace(c, vto_pago=vto)
                nota = (
                    f"Es la de siempre: repite la {numero_corto(ultima)} y corre el período a "
                    f"{fecha_ar(c.periodo_desde)} al {fecha_ar(c.periodo_hasta)}."
                    if c.periodo_desde and c.periodo_hasta
                    else f"Es la de siempre: repite la {numero_corto(ultima)}."
                )
            else:
                if imp is None:
                    raise ErrorUsuario("Falta el importe.")
                c = armado.armar_comprobante(
                    perfil=ctx.perfil,
                    punto_venta=ctx.perfil.punto_venta,
                    cbte_tipo=ctx.perfil.cbte_tipo_default,
                    receptor=armado.receptor_de_cliente(ctx.wsfe, cli),
                    items=(armado.item_de_cliente(cli, imp, descripcion.strip() or None),),
                    concepto=conc,
                    hoy=hoy,
                    desde=desde_f,
                    hasta=hasta_f,
                    vto_pago=vto,
                    vto_dias=cli.vto_dias,
                    condicion_venta=cli.condicion_venta,
                    modo_periodo=modo,
                )
        try:
            c.validar()
        except ValueError as e:
            raise ErrorUsuario(str(e)) from None
        return self._guardar_borrador(ctx, c, nota=nota)

    @staticmethod
    def _periodo_del_formulario(
        modo: str, concepto: Concepto, periodo: str, desde: str, hasta: str, hoy: date
    ) -> tuple[date | None, date | None]:
        """Lo que se completó en el formulario, según el modo. Si el modo no necesita fechas
        (día de emisión, sin período) las ignora; `armado.resolver_periodo` valida el resto."""
        if concepto == Concepto.PRODUCTOS:
            return None, None
        if modo == "rango":
            if not desde.strip() or not hasta.strip():
                raise ValueError("Poné las dos fechas del período (desde y hasta).")
            return armado.parsear_fecha(desde), armado.parsear_fecha(hasta)
        if modo in ("mes_completo", "auto") and periodo.strip():
            p = armado.parsear_periodo(periodo.strip(), hoy)
            return p[0], p[1]
        return None, None

    def opciones_del_cliente(self, alias: str) -> dict[str, str]:
        """El concepto y el modo de período con los que arranca el formulario para ese cliente."""
        if alias == CONSUMIDOR_FINAL:
            return {"concepto": "1", "modo": "mes_completo"}
        cli = self.cliente(alias)
        if cli is None:
            return {"concepto": "2", "modo": "mes_completo"}
        conc = parsear_concepto(cli.concepto)
        modo = "mes_completo" if cli.modo_periodo == "auto" else cli.modo_periodo
        return {"concepto": str(int(conc)), "modo": modo}

    def preparar_nota_de_credito(self, id_: int, motivo: str) -> VistaPreview:
        ctx = self.ctx()
        assert ctx.historial
        if not motivo.strip():
            raise ErrorUsuario("Contá el motivo de la anulación.")
        original = self.registro(id_)
        if original.estado != AUTORIZADO or original.comprobante.es_nota_de_credito:
            raise ErrorUsuario("Solo se pueden anular facturas autorizadas.")
        nc = service.nota_de_credito(ctx.historial, original, motivo.strip(), self.hoy)
        extra = [
            f"Esta factura YA FUE ANULADA por la {numero_corto(p)} del {fecha_ar(p.fecha)}."
            for p in ctx.historial.notas_de_credito_de(original.id)
        ]
        return self._guardar_borrador(ctx, nc, extra=extra)

    def _guardar_borrador(
        self,
        ctx: Contexto,
        c: Comprobante,
        *,
        nota: str | None = None,
        extra: list[str] | None = None,
    ) -> VistaPreview:
        numero = ctx.wsfe.proximo_numero(c.punto_venta, c.cbte_tipo)
        avisos = list(extra or [])
        info = [
            f"Ojo: ya emitiste {numero_corto(d)} del {fecha_ar(d.fecha)} por {pesos(d.importe)} a "
            f"{d.receptor_nombre} con el mismo período. Es solo un aviso."
            for d in ctx.posibles_duplicados(c)
        ]
        if nota:
            info.insert(0, nota)
        b = ctx.borradores.crear(
            perfil=ctx.perfil_nombre,
            ambiente=ctx.ambiente,
            c=c,
            avisos=avisos,
            requiere_extra=bool(avisos),
            ahora=self._ahora(),
        )
        return self.vista_preview(ctx, b, numero, info)

    def vista_preview(
        self, ctx: Contexto, b: Borrador, numero: int, info: list[str] | None = None
    ) -> VistaPreview:
        c = b.comprobante
        r, p = c.receptor, c.perfil
        doc = (
            f"CUIT {cuit_mod.formatear(str(r.doc_nro).zfill(11))}"
            if r.doc_tipo == 80
            else (f"DNI {r.doc_nro}" if r.doc_tipo == 96 else "sin identificar")
        )
        periodo = None
        if c.concepto in (Concepto.SERVICIOS, Concepto.PRODUCTOS_Y_SERVICIOS) and c.periodo_desde:
            periodo = f"{fecha_ar(c.periodo_desde)} al {fecha_ar(c.periodo_hasta)}"
        anula = None
        if c.cbte_asoc:
            a = c.cbte_asoc
            anula = (
                f"{NOMBRES_COMPROBANTE.get(a.tipo, 'Comprobante')} "
                f"{a.punto_venta:05d}-{a.numero:08d}"
            )
        return VistaPreview(
            borrador_id=b.id,
            vence=b.vence.strftime("%H:%M"),
            titulo=NOMBRES_COMPROBANTE.get(c.cbte_tipo, "Comprobante"),
            numero=f"{c.punto_venta:05d}-{numero:08d}",
            emisor=f"{p.razon_social} — CUIT {cuit_mod.formatear(p.cuit)}",
            receptor=r.nombre,
            receptor_doc=doc,
            condicion_iva=r.condicion_iva,
            concepto=NOMBRES_CONCEPTO[c.concepto],
            fecha=fecha_ar(c.fecha),
            periodo=periodo,
            vto_pago=fecha_ar(c.vto_pago) if periodo and c.vto_pago else None,
            items=[
                {
                    "cantidad": f"{i.cantidad:f}".rstrip("0").rstrip(".") or "0",
                    "unidad": i.unidad,
                    "descripcion": i.descripcion,
                    "unitario": pesos(i.precio_unitario),
                    "subtotal": pesos(i.subtotal),
                }
                for i in c.items
            ],
            total=pesos(c.total),
            avisos=list(b.avisos),
            info=info or [],
            requiere_extra=b.requiere_extra,
            anula=anula,
            motivo=c.motivo or None,
            es_produccion=ctx.es_produccion,
        )

    def emitir(self, borrador_id: str, confirmo_anulada: bool = False) -> Emitido:
        """Único punto de emisión de la GUI: borrador existente, vigente y de este ambiente."""
        ctx = self.ctx()
        assert ctx.historial
        b = ctx.borradores.obtener(borrador_id)
        if b is None:
            raise ErrorUsuario("Ese borrador ya no existe. Preparalo de nuevo.")
        if ctx.ambiente != b.ambiente:
            raise ErrorUsuario("Cambió el ambiente desde que se preparó: prepará uno nuevo.")
        if b.estado != "emitido":
            if b.vencido(self._ahora()):
                raise ErrorUsuario("El borrador venció (duran 15 minutos). Preparalo de nuevo.")
            if b.requiere_extra and not confirmo_anulada:
                raise ErrorUsuario(
                    "Esta factura ya estaba anulada. Tildá la casilla para anularla otra vez."
                )
        em = service.emitir(
            ctx.historial,
            ctx.wsfe,
            perfil=b.perfil,
            ambiente=b.ambiente,
            c=b.comprobante,
            clave=f"gui-{b.id}",
        )
        ctx.borradores.marcar_emitido(b.id)
        r = em.registro
        assert r.numero is not None
        pdf: Path | None = None
        aviso_pdf = None
        try:
            pdf = guardar_pdf(config.cargar(), r)
        except Exception as e:  # el CAE ya está: no tapar el éxito por un problema con el PDF
            aviso_pdf = f"No se pudo generar el PDF ({e}). Reintentá desde «Mis facturas»."
        return Emitido(
            registro=r,
            titulo=NOMBRES_COMPROBANTE.get(r.cbte_tipo, "Comprobante"),
            numero=f"{r.punto_venta:05d}-{r.numero:08d}",
            cae=r.cae,
            cae_vto=fecha_ar(r.cae_vto),
            receptor=r.receptor_nombre,
            importe=pesos(r.importe),
            pdf=pdf,
            aviso_pdf=aviso_pdf,
            observaciones=list(r.observaciones),
            reutilizado=em.reutilizado,
            es_homologacion=r.es_homologacion,
        )

    # --- ajustes ---------------------------------------------------------------------------

    def ajustes(self) -> dict[str, object]:
        cfg = config.cargar()
        p = cfg.perfil()
        return {
            "carpeta_pdf": str(cfg.carpeta_base_pdf()),
            "categoria": p.categoria_monotributo,
            "categorias": list(topes.cargar().categorias),
            "aviso_duplicados": p.aviso_duplicados,
            "ambiente": cfg.ambiente,
            "prod_configurado": bool(p.credencial_prod),
            "monotributista": "monotribut" in p.condicion_iva.lower(),
            "punto_venta": p.punto_venta,
            "punto_venta_prod": p.punto_venta_prod,
            "version": __import__("factumono").__version__,
        }

    def guardar_ajustes(
        self, *, carpeta_pdf: str, categoria: str, aviso_duplicados: bool
    ) -> ResultadoAjustes:
        cfg = config.cargar()
        p = cfg.perfil()
        categoria = categoria.strip().upper()
        if categoria:
            try:
                topes.cargar().tope(categoria)
            except topes.TopesError as e:
                raise ErrorUsuario(str(e)) from None
        cfg.carpeta_pdf = str(Path(carpeta_pdf).expanduser()) if carpeta_pdf.strip() else ""
        cfg.perfiles[cfg.perfil_default] = replace(
            p, categoria_monotributo=categoria, aviso_duplicados=aviso_duplicados
        )
        config.guardar(cfg)
        return ResultadoAjustes("Listo, guardé los ajustes.")

    def cambiar_ambiente(self, destino: str) -> tuple[ResultadoAjustes, Diagnostico | None]:
        """Cambia el ambiente. Producción solo si todo da OK (mismas reglas que la CLI)."""
        amb = ambientes.obtener(destino)
        cfg = config.cargar()
        if amb.nombre == cfg.ambiente:
            return ResultadoAjustes(f"Ya estás en {amb.titulo}."), None
        diag = None
        if amb.es_produccion:
            try:
                ctx = self._contexto(None, "prod")
            except config.ConfigError as e:
                raise ErrorUsuario(str(e)) from None
            diag, problemas = problemas_para_activar_produccion(ctx)
            if problemas:
                raise ErrorUsuario("No se activó producción: " + " ".join(problemas))
        cfg.ambiente = amb.nombre
        config.guardar(cfg)
        return ResultadoAjustes(f"Listo: ahora estás en {amb.titulo}."), diag

    def categorias(self) -> list[str]:
        return list(topes.cargar().categorias)

    def hacer_backup(self) -> backup_mod.ResultadoBackup:
        return backup_mod.crear_backup()

    # --- conectar con la IA ----------------------------------------------------------------

    def conectar_claude_desktop(self) -> tuple[list[str], str]:
        """Agrega Factumono a Claude Desktop (con backup) y devuelve (mensajes, snippets)."""
        entrada = mcp_instalar.entrada_mcp()
        mensajes = []
        rutas = mcp_instalar.rutas_claude_desktop()
        destinos = [r for r in rutas if r.exists() or r.parent.exists()] or rutas[:1]
        for ruta in destinos:
            try:
                r = mcp_instalar.instalar_en(ruta, entrada)
            except mcp_instalar.ConfigInvalida as e:
                raise ErrorUsuario(str(e)) from None
            if r.ya_estaba:
                mensajes.append(f"Claude Desktop ya tenía Factumono conectado ({r.ruta}).")
            else:
                mensajes.append(f"Conecté Claude Desktop ({r.ruta}).")
                if r.reemplazo_viejo:
                    mensajes.append("Reemplacé la conexión con el nombre anterior por la nueva.")
                if r.backup:
                    mensajes.append(f"Guardé una copia del archivo anterior: {r.backup}")
        mensajes.append(
            "Cerrá Claude Desktop del todo (también desde la bandeja) y abrilo de nuevo."
        )
        return mensajes, mcp_instalar.snippets(entrada)

    def snippets(self) -> str:
        return mcp_instalar.snippets(mcp_instalar.entrada_mcp())

    # --- asistente de primera vez ----------------------------------------------------------

    def wizard(self, ambiente: str | None = None) -> EstadoWizard:
        cfg = config.cargar()
        amb = ambiente or ("prod" if cfg.perfiles and cfg.ambiente == "prod" else "homo")
        if amb == "prod" and not cfg.perfiles:
            amb = "homo"
        av = asis.cargar_avance(amb) or asis.Avance(
            ambiente=amb, perfil=cfg.perfil_default if cfg.perfiles else "default"
        )
        csr = ""
        if av.csr and Path(av.csr).is_file():
            csr = Path(av.csr).read_text(encoding="utf-8")
        return EstadoWizard(ambiente=amb, paso=av.paso, avance=av, csr_texto=csr)

    def wizard_datos(
        self,
        *,
        ambiente: str,
        cuit: str,
        razon_social: str,
        condicion: str,
        domicilio: str,
        iibb: str = "",
        nombre_fantasia: str = "",
        inicio: str = "",
        alias: str = "",
    ) -> asis.Avance:
        cfg = config.cargar()
        av = self.wizard(ambiente).avance
        previo = cfg.perfiles.get(av.perfil)
        if ambiente == "prod" and previo:
            av.cuit, av.razon_social = previo.cuit, previo.razon_social
            av.condicion_iva, av.domicilio = previo.condicion_iva, previo.domicilio
        else:
            try:
                av.cuit = cuit_mod.normalizar(cuit)
            except ValueError as e:
                raise ErrorUsuario(str(e)) from None
            if not razon_social.strip() or not domicilio.strip():
                raise ErrorUsuario("Faltan tu nombre o tu domicilio.")
            if condicion not in asis.CONDICIONES.values():
                raise ErrorUsuario("Elegí tu condición frente al IVA.")
            av.razon_social, av.domicilio = razon_social.strip(), domicilio.strip()
            av.condicion_iva = condicion
            av.iibb, av.nombre_fantasia = iibb.strip(), nombre_fantasia.strip()
            av.inicio_actividades = ""
            if inicio.strip():
                try:
                    av.inicio_actividades = (
                        datetime.strptime(inicio.strip(), "%d/%m/%Y").date().isoformat()
                    )
                except ValueError:
                    raise ErrorUsuario("El inicio de actividades va como DD/MM/AAAA.") from None
        sufijo = "prod" if ambiente == "prod" else "homo"
        av.alias = alias.strip() or f"factumono-{sufijo}"
        av.paso = 1
        asis.guardar_avance(av)
        # El paso 2 (clave y CSR) es automático.
        key, csr, _ = generar_clave_y_csr(
            carpeta_certificados(ambiente), av.alias, av.cuit, av.razon_social
        )
        av.key, av.csr = str(key), str(csr)
        av.paso = 2
        asis.guardar_avance(av)
        return av

    def wizard_tramites_hechos(self, ambiente: str) -> asis.Avance:
        av = self.wizard(ambiente).avance
        if av.paso < 2:
            raise ErrorUsuario("Primero completá tus datos.")
        av.paso = max(av.paso, 3)
        asis.guardar_avance(av)
        return av

    def wizard_certificado(self, ambiente: str, contenido: bytes) -> asis.Avance:
        av = self.wizard(ambiente).avance
        if av.paso < 3 or not av.key:
            raise ErrorUsuario("Todavía falta generar la clave (volvé al paso anterior).")
        destino = Path(av.key).with_suffix(".crt")
        temporal = destino.with_suffix(".crt.nuevo")
        temporal.write_bytes(contenido)
        try:
            cert = asis.validar_certificado(temporal, Path(av.key))
        except ValueError as e:
            temporal.unlink(missing_ok=True)
            raise ErrorUsuario(str(e)) from None
        temporal.replace(destino)
        from cryptography.hazmat.primitives import serialization

        from factumono.arca.csr import cuit_del_certificado

        en_cert = cuit_del_certificado(cert.public_bytes(serialization.Encoding.PEM))
        av.crt = str(destino)
        av.paso = 4
        asis.guardar_avance(av)
        if en_cert and en_cert != av.cuit:
            raise ErrorUsuario(
                f"Ojo: el certificado es del CUIT {cuit_mod.formatear(en_cert)}, distinto del "
                f"que cargaste ({cuit_mod.formatear(av.cuit)}). Se guardó igual; si es un error, "
                "volvé a ARCA y bajá el certificado correcto."
            )
        return av

    def wizard_guardar(self, ambiente: str, punto_venta: int, categoria: str = "") -> asis.Avance:
        av = self.wizard(ambiente).avance
        if av.paso < 4:
            raise ErrorUsuario("Primero cargá el certificado.")
        if punto_venta < 1:
            raise ErrorUsuario("El punto de venta tiene que ser un número mayor a cero.")
        av.punto_venta = punto_venta
        try:
            asis.guardar_configuracion(av)
        except ValueError as e:
            raise ErrorUsuario(str(e)) from None
        if categoria.strip():
            cfg = config.cargar()
            p = cfg.perfiles[av.perfil]
            cfg.perfiles[av.perfil] = replace(p, categoria_monotributo=categoria.strip().upper())
            config.guardar(cfg)
        av.paso = 5
        asis.guardar_avance(av)
        return av

    def wizard_verificar(self, ambiente: str) -> Diagnostico:
        av = self.wizard(ambiente).avance
        if av.paso < 5:
            raise ErrorUsuario("Primero guardá la configuración.")
        ctx = self._contexto(av.perfil, ambiente)
        d = diagnosticar(ctx)
        if d.ok or (not ctx.es_produccion and d.dummy_ok and d.ticket_vence):
            av.paso = asis.TOTAL_PASOS
            asis.guardar_avance(av)
        return d

    def wizard_terminar(self, ambiente: str) -> None:
        asis.olvidar_avance(ambiente)


def abrir_carpeta(ruta: Path) -> None:
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(ruta)])  # noqa: S603, S607
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ruta)])  # noqa: S603, S607
        else:
            subprocess.Popen(["xdg-open", str(ruta)])  # noqa: S603, S607
