"""Importar facturas anteriores: de lo leído (PDF, «Mis Comprobantes» o datos cargados por una IA)
a clientes + plantilla y, si el emisor es el perfil, comprobantes históricos externos.

Siempre en dos pasos: `planear` (no escribe nada, devuelve qué se haría) y `aplicar` (guarda).
Lo importado como histórico queda marcado `externo`: no lo emitió Factumono, pero cuenta para el
tope del monotributo, para detectar huecos de numeración y para «la de siempre».
"""

import re
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from factumono.contexto import Contexto
from factumono.core import armado, money
from factumono.core import cuit as cuit_mod
from factumono.core.armado import fin_de_mes
from factumono.core.lectura import (
    FacturaLeida,
    FilaMisComprobantes,
    ItemLeido,
    LecturaMisComprobantes,
    NoReconocido,
    leer_mis_comprobantes,
    leer_pdf,
)
from factumono.core.models import (
    CONCEPTO_TEXTO,
    NOMBRES_COMPROBANTE,
    Cliente,
    Comprobante,
    Concepto,
    DocTipo,
    Item,
    Receptor,
)
from factumono.storage.historial import AUTORIZADO, Registro

# --- el plan -----------------------------------------------------------------------------------


@dataclass
class AccionCliente:
    accion: str  # 'crear' | 'actualizar' | 'sin_cambios'
    cliente: Cliente
    antes: Cliente | None = None
    cambios: list[str] = field(default_factory=list)


@dataclass
class AccionComprobante:
    accion: str  # 'registrar' | 'ya_estaba' | 'omitir'
    descripcion: str
    motivo: str = ""
    comprobante: Comprobante | None = None
    numero: int | None = None
    cae: str = ""
    cae_vto: date | None = None
    total: Decimal = Decimal(0)


@dataclass
class AccionArchivo:
    nombre: str
    factura: FacturaLeida | None = None
    cliente: AccionCliente | None = None
    comprobante: AccionComprobante | None = None
    avisos: list[str] = field(default_factory=list)


@dataclass
class Plan:
    ambiente: str
    archivos: list[AccionArchivo] = field(default_factory=list)
    no_reconocidos: list[NoReconocido] = field(default_factory=list)
    mis_comprobantes: list[AccionComprobante] = field(default_factory=list)
    avisos_mis_comprobantes: list[str] = field(default_factory=list)

    @property
    def hay_algo_para_guardar(self) -> bool:
        return any(
            (a.cliente and a.cliente.accion != "sin_cambios")
            or (a.comprobante and a.comprobante.accion == "registrar")
            for a in self.archivos
        ) or any(m.accion == "registrar" for m in self.mis_comprobantes)

    @property
    def clientes(self) -> list[AccionCliente]:
        return [a.cliente for a in self.archivos if a.cliente]

    def texto(self) -> str:
        """El resumen para la CLI y el MCP (el de la GUI sale de las mismas estructuras)."""
        out: list[str] = []
        for a in self.archivos:
            out.append(f"📄 {a.nombre}")
            f = a.factura
            if f:
                tipo = NOMBRES_COMPROBANTE.get(f.cbte_tipo or 0, f"tipo {f.cbte_tipo}")
                nro = (
                    f"{f.punto_venta:05d}-{f.numero:08d}"
                    if f.punto_venta is not None and f.numero is not None
                    else "?"
                )
                out.append(
                    f"   {tipo} {nro} del {f.fecha.strftime('%d/%m/%Y') if f.fecha else '?'} — "
                    f"{f.receptor_nombre or '?'} — total "
                    f"{_pesos(f.total) if f.total is not None else '?'}"
                )
                out.append(
                    "   QR: "
                    + (
                        "no coincide con el texto ⚠️"
                        if f.inconsistente
                        else ("leído y coincide ✅" if f.qr else "no se pudo leer")
                    )
                )
            if a.cliente:
                c = a.cliente
                etiqueta = {
                    "crear": "se CREA el cliente",
                    "actualizar": "se ACTUALIZA el cliente",
                    "sin_cambios": "el cliente ya está igual",
                }[c.accion]
                out.append(f"   → {etiqueta} «{c.cliente.alias}» ({c.cliente.nombre})")
                out += [f"        · {x}" for x in c.cambios]
            if a.comprobante:
                m = a.comprobante
                out.append(f"   → {_etiqueta_comprobante(m)}")
            out += [f"   ⚠️ {x}" for x in a.avisos]
        for n in self.no_reconocidos:
            out.append(f"❓ {n.archivo}: no reconocido — {n.motivo}")
        if self.mis_comprobantes:
            nuevos = [m for m in self.mis_comprobantes if m.accion == "registrar"]
            ya = [m for m in self.mis_comprobantes if m.accion == "ya_estaba"]
            omitidos = [m for m in self.mis_comprobantes if m.accion == "omitir"]
            out.append(
                f"📊 Mis Comprobantes: {len(nuevos)} para registrar, {len(ya)} que ya estaban, "
                f"{len(omitidos)} omitidos."
            )
            out += [f"   ⚠️ {x}" for x in self.avisos_mis_comprobantes]
        return "\n".join(out)


def _pesos(x: Decimal) -> str:
    from factumono.core.money import formatear_ar

    return "$ " + formatear_ar(x)


def _etiqueta_comprobante(m: AccionComprobante) -> str:
    base = {
        "registrar": "se REGISTRA como comprobante histórico externo (cuenta para el tope)",
        "ya_estaba": "ya estaba en el historial",
        "omitir": "no se registra",
    }[m.accion]
    return base + (f": {m.motivo}" if m.motivo else "")


# --- armado del cliente ------------------------------------------------------------------------


def _slug(nombre: str) -> str:
    sin = "".join(
        c for c in unicodedata.normalize("NFD", nombre) if unicodedata.category(c) != "Mn"
    )
    palabras = re.findall(r"[a-z0-9]+", sin.lower())
    palabras = [p for p in palabras if p not in ("srl", "sa", "s", "r", "l", "de", "la", "el")]
    return (palabras[0] if palabras else "cliente")[:20]


def _modo_periodo(f: FacturaLeida) -> str:
    if f.concepto == Concepto.PRODUCTOS or not (f.periodo_desde and f.periodo_hasta):
        return "sin_periodo"
    if f.periodo_desde.day == 1 and f.periodo_hasta == fin_de_mes(f.periodo_desde):
        return "mes_completo"
    if f.periodo_desde == f.periodo_hasta == f.fecha:
        return "dia_emision"
    return "rango"


def cliente_de_factura(f: FacturaLeida, alias: str) -> Cliente | None:
    """El cliente (con su plantilla) que sale de una factura. None si no hay a quién guardar
    (consumidor final sin identificar)."""
    if f.receptor_doc_tipo == int(DocTipo.CONSUMIDOR_FINAL) or not f.receptor_doc_nro:
        return None
    items = tuple(_item(i) for i in f.items)
    primero = f.items[0] if f.items else None
    vto = (f.vto_pago - f.fecha).days if f.vto_pago and f.fecha else 4
    return Cliente(
        alias=alias,
        nombre=f.receptor_nombre,
        doc_tipo=f.receptor_doc_tipo,
        doc_nro=f.receptor_doc_nro,
        condicion_iva=f.receptor_condicion_iva or "Consumidor Final",
        domicilio=f.receptor_domicilio,
        descripcion=primero.descripcion if primero else "",
        unidad=primero.unidad if primero and primero.unidad else "otras unidades",
        concepto=CONCEPTO_TEXTO[f.concepto],
        vto_dias=max(vto, 0),
        modo_periodo=_modo_periodo(f),
        condicion_venta=f.condicion_venta or "Contado",
        plantilla=armado.plantilla_a_json(items) if items else "",
    )


def _item(i: ItemLeido) -> Item:
    return Item(
        descripcion=i.descripcion,
        cantidad=i.cantidad,
        precio_unitario=i.precio_unitario,
        unidad=i.unidad or "unidades",
        codigo=i.codigo or "1",
        bonificacion_pct=i.bonificacion_pct,
    )


_CAMPOS_A_COMPARAR = (
    "nombre", "condicion_iva", "domicilio", "descripcion", "unidad", "concepto", "vto_dias",
    "modo_periodo", "condicion_venta", "plantilla",
)  # fmt: skip


def _fusionar(existente: Cliente, nuevo: Cliente) -> tuple[Cliente, list[str]]:
    """El cliente guardado con lo que trae la factura (conserva alias y email)."""
    cambios = []
    datos = {}
    for campo in _CAMPOS_A_COMPARAR:
        v_nuevo = getattr(nuevo, campo)
        if campo == "domicilio" and not v_nuevo:
            continue  # no se pisa un domicilio con vacío
        if v_nuevo != getattr(existente, campo):
            datos[campo] = v_nuevo
            etiqueta = "plantilla de ítems" if campo == "plantilla" else campo.replace("_", " ")
            cambios.append(f"{etiqueta} actualizado")
    return replace(existente, **datos), cambios


# --- leer archivos y carpetas -----------------------------------------------------------------

EXTENSIONES_TABLA = (".xlsx", ".xlsm", ".csv", ".txt")


def leer_rutas(
    rutas: list[Path],
) -> tuple[list[FacturaLeida | NoReconocido], LecturaMisComprobantes | None]:
    """PDF sueltos, carpetas (con sus PDF) y planillas de «Mis Comprobantes»."""
    pdfs: list[Path] = []
    tablas: list[Path] = []
    for r in rutas:
        if not r.exists():
            raise ValueError(f"No existe: {r}")
        if r.is_dir():
            pdfs += sorted(r.rglob("*.pdf")) + sorted(r.rglob("*.PDF"))
            tablas += [p for p in sorted(r.rglob("*")) if p.suffix.lower() in EXTENSIONES_TABLA]
        elif r.suffix.lower() == ".pdf":
            pdfs.append(r)
        elif r.suffix.lower() in EXTENSIONES_TABLA:
            tablas.append(r)
        elif r.exists():
            raise ValueError(f"No sé importar «{r.name}»: uso PDF, Excel (.xlsx) o CSV.")
        else:
            raise ValueError(f"No existe: {r}")
    vistos: set[Path] = set()
    lecturas: list[FacturaLeida | NoReconocido] = []
    for p in pdfs:
        if p.resolve() in vistos:
            continue
        vistos.add(p.resolve())
        lecturas.append(leer_pdf(p))
    mis: LecturaMisComprobantes | None = None
    for t in tablas:
        try:
            leida = leer_mis_comprobantes(t)
        except ValueError as e:
            if len(tablas) == 1 and not pdfs:
                raise
            lecturas.append(NoReconocido(str(e), archivo=t.name))
            continue
        if mis is None:
            mis = leida
        else:
            mis.filas += leida.filas
            mis.advertencias += leida.advertencias
            mis.cuit_emisor = mis.cuit_emisor or leida.cuit_emisor
    return lecturas, mis


def autochequeo() -> str:
    """Genera un PDF ficticio, lo vuelve a leer y valida su QR: prueba que el lector (pdfium,
    zxing, reportlab) funciona, por ejemplo dentro del ejecutable empaquetado."""
    import tempfile

    from factumono.core.models import Perfil
    from factumono.pdf.render import renderizar

    perfil = Perfil(
        cuit="20000000001",
        razon_social="EMISOR FICTICIO",
        condicion_iva="Responsable Monotributo",
        domicilio="Calle Falsa 123",
    )
    c = Comprobante(
        perfil=perfil,
        punto_venta=1,
        cbte_tipo=11,
        concepto=Concepto.PRODUCTOS,
        receptor=Receptor(80, 30000000007, "RECEPTOR FICTICIO", 1, "IVA Responsable Inscripto"),
        items=(Item("Prueba", Decimal(1), Decimal("123.45"), "unidades"),),
        fecha=date(2026, 1, 15),
    )
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "prueba.pdf"
        ruta.write_bytes(renderizar(c, 1, "70000000000001", date(2026, 1, 25), homologacion=False))
        f = leer_pdf(ruta)
    if not isinstance(f, FacturaLeida) or f.total != Decimal("123.45") or f.qr is None:
        raise RuntimeError("El lector de facturas no funciona (PDF, QR o texto).")
    if f.inconsistente:
        raise RuntimeError("El QR leído no coincide con el texto.")
    return "OK: el lector de facturas anteriores funciona (PDF, texto y QR)."


# --- planear ------------------------------------------------------------------------------------


def planear(
    ctx: Contexto,
    lecturas: list[FacturaLeida | NoReconocido],
    mis: LecturaMisComprobantes | None = None,
    *,
    registrar_historico: bool = True,
) -> Plan:
    """Qué se haría con lo leído. No escribe nada."""
    plan = Plan(ambiente=ctx.ambiente)
    facturas = [x for x in lecturas if isinstance(x, FacturaLeida)]
    plan.no_reconocidos = [x for x in lecturas if isinstance(x, NoReconocido)]
    clientes_guardados = ctx.clientes.listar(ctx.perfil_nombre)
    por_doc = {(c.doc_tipo, c.doc_nro): c for c in clientes_guardados}
    alias_usados = {c.alias for c in clientes_guardados}
    # de la más vieja a la más nueva: la última factura de cada cliente es la que manda
    for f in sorted(facturas, key=lambda x: (x.fecha or date.min, x.numero or 0)):
        acc = AccionArchivo(nombre=f.archivo or "(datos)", factura=f, avisos=list(f.advertencias))
        propio = _es_del_perfil(ctx, f)
        if f.origen != "datos" and not propio:
            acc.avisos.append(
                f"El emisor ({cuit_mod.formatear(f.emisor_cuit) if f.emisor_cuit else '?'}) no es "
                f"el de este perfil ({cuit_mod.formatear(ctx.perfil.cuit)}): no lo importo."
            )
            plan.archivos.append(acc)
            continue
        nuevo = cliente_de_factura(f, "")
        if nuevo is not None:
            existente = por_doc.get((nuevo.doc_tipo, nuevo.doc_nro))
            if existente:
                fusionado, cambios = _fusionar(existente, replace(nuevo, alias=existente.alias))
                acc.cliente = AccionCliente(
                    "actualizar" if cambios else "sin_cambios", fusionado, existente, cambios
                )
                por_doc[(nuevo.doc_tipo, nuevo.doc_nro)] = fusionado
            else:
                alias = _alias_libre(_slug(nuevo.nombre), alias_usados)
                alias_usados.add(alias)
                nuevo = replace(nuevo, alias=alias)
                acc.cliente = AccionCliente("crear", nuevo, None, ["datos y plantilla de ítems"])
                por_doc[(nuevo.doc_tipo, nuevo.doc_nro)] = nuevo
        else:
            acc.avisos.append("Consumidor final sin identificar: no hay cliente para guardar.")
        if registrar_historico and f.origen != "datos":
            acc.comprobante = _plan_comprobante(ctx, f)
        plan.archivos.append(acc)
    if mis is not None:
        plan.mis_comprobantes, plan.avisos_mis_comprobantes = _plan_mis_comprobantes(ctx, mis)
    return plan


def _es_del_perfil(ctx: Contexto, f: FacturaLeida) -> bool:
    return bool(f.emisor_cuit) and f.emisor_cuit == ctx.perfil.cuit


def _alias_libre(base: str, usados: set[str]) -> str:
    if base not in usados:
        return base
    n = 2
    while f"{base}{n}" in usados:
        n += 1
    return f"{base}{n}"


def _plan_comprobante(ctx: Contexto, f: FacturaLeida) -> AccionComprobante:
    assert ctx.historial is not None
    nombre = NOMBRES_COMPROBANTE.get(f.cbte_tipo or 0, f"tipo {f.cbte_tipo}")
    desc = (
        f"{nombre} {f.punto_venta:05d}-{f.numero:08d}"
        if f.punto_venta is not None and f.numero is not None
        else nombre
    )
    if f.inconsistente:
        return AccionComprobante(
            "omitir", desc, "el QR no coincide con el texto: revisá el PDF a mano"
        )
    if not f.es_externo_completo:
        return AccionComprobante("omitir", desc, "faltan datos (número, fecha, importe o CAE)")
    assert f.punto_venta and f.numero and f.cbte_tipo and f.fecha and f.total is not None
    if ctx.historial.por_numero(
        ctx.perfil.cuit, ctx.ambiente, f.punto_venta, f.cbte_tipo, f.numero
    ):
        return AccionComprobante("ya_estaba", desc)
    comp, aviso = _comprobante_de_factura(ctx, f)
    return AccionComprobante("registrar", desc, aviso, comp, f.numero, f.cae, f.cae_vto, comp.total)


def _comprobante_de_factura(ctx: Contexto, f: FacturaLeida) -> tuple[Comprobante, str]:
    assert f.punto_venta and f.cbte_tipo and f.fecha and f.total is not None
    items = tuple(_item(i) for i in f.items)
    aviso = ""
    if not items or sum((i.subtotal for i in items), Decimal(0)) != f.total:
        items = (
            Item(
                descripcion="Detalle no disponible (importado)",
                cantidad=Decimal(1),
                precio_unitario=f.total,
                unidad="unidades",
            ),
        )
        aviso = "no pude leer el detalle completo: se registra con el total"
    receptor = Receptor(
        doc_tipo=f.receptor_doc_tipo,
        doc_nro=f.receptor_doc_nro,
        nombre=f.receptor_nombre or "Consumidor Final",
        condicion_iva_id=0,  # no hace falta para un histórico; el texto es lo que se muestra
        condicion_iva=f.receptor_condicion_iva,
        domicilio=f.receptor_domicilio,
    )
    return (
        Comprobante(
            perfil=ctx.perfil,
            punto_venta=f.punto_venta,
            cbte_tipo=f.cbte_tipo,
            concepto=f.concepto,
            receptor=receptor,
            items=items,
            fecha=f.fecha,
            periodo_desde=f.periodo_desde,
            periodo_hasta=f.periodo_hasta,
            vto_pago=f.vto_pago,
            condicion_venta=f.condicion_venta,
        ),
        aviso,
    )


def _plan_mis_comprobantes(
    ctx: Contexto, mis: LecturaMisComprobantes
) -> tuple[list[AccionComprobante], list[str]]:
    assert ctx.historial is not None
    avisos = list(mis.advertencias)
    if mis.cuit_emisor and mis.cuit_emisor != ctx.perfil.cuit:
        avisos.append(
            f"El archivo parece ser del CUIT {cuit_mod.formatear(mis.cuit_emisor)}, no del perfil "
            f"({cuit_mod.formatear(ctx.perfil.cuit)}): no registro nada."
        )
        return [], avisos
    acciones = []
    for fila in mis.filas:
        nombre = NOMBRES_COMPROBANTE.get(fila.cbte_tipo, f"tipo {fila.cbte_tipo}")
        desc = f"{nombre} {fila.punto_venta:05d}-{fila.numero:08d} del {fila.fecha:%d/%m/%Y}"
        if ctx.historial.por_numero(
            ctx.perfil.cuit, ctx.ambiente, fila.punto_venta, fila.cbte_tipo, fila.numero
        ):
            acciones.append(AccionComprobante("ya_estaba", desc))
            continue
        if not fila.cae:
            acciones.append(AccionComprobante("omitir", desc, "sin código de autorización"))
            continue
        comp = _comprobante_de_fila(ctx, fila)
        acciones.append(
            AccionComprobante("registrar", desc, "", comp, fila.numero, fila.cae, None, fila.total)
        )
    return acciones, avisos


def _comprobante_de_fila(ctx: Contexto, fila: FilaMisComprobantes) -> Comprobante:
    return Comprobante(
        perfil=ctx.perfil,
        punto_venta=fila.punto_venta,
        cbte_tipo=fila.cbte_tipo,
        concepto=Concepto.PRODUCTOS,
        receptor=Receptor(
            doc_tipo=fila.receptor_doc_tipo,
            doc_nro=fila.receptor_doc_nro,
            nombre=fila.receptor_nombre,
            condicion_iva_id=0,
        ),
        items=(
            Item(
                descripcion="Importado de «Mis Comprobantes» (sin detalle)",
                cantidad=Decimal(1),
                precio_unitario=fila.total,
                unidad="unidades",
            ),
        ),
        fecha=fila.fecha,
    )


# --- aplicar --------------------------------------------------------------------------------------


@dataclass
class Resultado:
    clientes_creados: list[str] = field(default_factory=list)
    clientes_actualizados: list[str] = field(default_factory=list)
    comprobantes: list[Registro] = field(default_factory=list)

    def texto(self) -> str:
        partes = []
        if self.clientes_creados:
            partes.append(f"clientes creados: {', '.join(self.clientes_creados)}")
        if self.clientes_actualizados:
            partes.append(f"clientes actualizados: {', '.join(self.clientes_actualizados)}")
        if self.comprobantes:
            partes.append(f"{len(self.comprobantes)} comprobante(s) histórico(s) registrado(s)")
        return "; ".join(partes) or "no había nada para guardar"


def aplicar(ctx: Contexto, plan: Plan) -> Resultado:
    """Guarda lo que `planear` dijo. Es idempotente: lo que ya está no se duplica."""
    assert ctx.historial is not None
    res = Resultado()
    acciones: list[AccionComprobante] = []
    for a in plan.archivos:
        if a.cliente and a.cliente.accion != "sin_cambios":
            ctx.clientes.guardar(ctx.perfil_nombre, a.cliente.cliente, reemplazar=True)
            (
                res.clientes_creados if a.cliente.accion == "crear" else res.clientes_actualizados
            ).append(a.cliente.cliente.alias)
        if a.comprobante:
            acciones.append(a.comprobante)
    acciones += plan.mis_comprobantes
    for m in acciones:
        if m.accion != "registrar" or m.comprobante is None or m.numero is None:
            continue
        c = m.comprobante
        if ctx.historial.por_numero(
            ctx.perfil.cuit, ctx.ambiente, c.punto_venta, c.cbte_tipo, m.numero
        ):
            continue
        reg = ctx.historial.crear(
            perfil=ctx.perfil_nombre,
            ambiente=ctx.ambiente,
            c=c,
            clave=f"externo-{ctx.ambiente}-{ctx.perfil.cuit}-{c.punto_venta}-{c.cbte_tipo}-{m.numero}",
            estado=AUTORIZADO,
            numero=m.numero,
            externo=True,
        )
        res.comprobantes.append(ctx.historial.actualizar(reg.id, cae=m.cae, cae_vto=m.cae_vto))
    return res


# --- (de)serializar lo leído, para el paso intermedio de la GUI y del MCP -----------------------


def lectura_a_dict(f: FacturaLeida) -> dict[str, Any]:
    d = asdict(f)
    for k in (
        "fecha",
        "periodo_desde",
        "periodo_hasta",
        "vto_pago",
        "cae_vto",
        "emisor_inicio_actividades",
    ):
        d[k] = d[k].isoformat() if d[k] else None
    d["concepto"] = int(f.concepto)
    d["total"] = str(f.total) if f.total is not None else None
    for i in d["items"]:
        for k in (
            "cantidad",
            "precio_unitario",
            "bonificacion_pct",
            "bonificacion_importe",
            "subtotal",
        ):
            i[k] = str(i[k])
    return d


def lectura_de_dict(d: dict[str, Any]) -> FacturaLeida:
    def f(k: str) -> date | None:
        return date.fromisoformat(d[k]) if d.get(k) else None

    datos = dict(d)
    for k in (
        "fecha",
        "periodo_desde",
        "periodo_hasta",
        "vto_pago",
        "cae_vto",
        "emisor_inicio_actividades",
    ):
        datos[k] = f(k)
    datos["concepto"] = Concepto(int(d["concepto"]))
    datos["total"] = Decimal(d["total"]) if d.get("total") is not None else None
    datos["items"] = [
        ItemLeido(
            descripcion=i["descripcion"],
            cantidad=Decimal(i["cantidad"]),
            unidad=i["unidad"],
            precio_unitario=Decimal(i["precio_unitario"]),
            codigo=i.get("codigo", "1"),
            bonificacion_pct=Decimal(i.get("bonificacion_pct", "0")),
            bonificacion_importe=Decimal(i.get("bonificacion_importe", "0")),
            subtotal=Decimal(i.get("subtotal", "0")),
        )
        for i in d.get("items", [])
    ]
    return FacturaLeida(**datos)


def fila_a_dict(f: FilaMisComprobantes) -> dict[str, Any]:
    d = asdict(f)
    d["fecha"] = f.fecha.isoformat()
    d["total"] = str(f.total)
    return d


def fila_de_dict(d: dict[str, Any]) -> FilaMisComprobantes:
    return FilaMisComprobantes(
        **{**d, "fecha": date.fromisoformat(d["fecha"]), "total": Decimal(d["total"])}
    )


def a_payload(
    lecturas: list[FacturaLeida | NoReconocido], mis: LecturaMisComprobantes | None
) -> dict[str, Any]:
    """Lo leído, en forma de JSON, para guardarlo hasta que la persona confirme."""
    return {
        "facturas": [lectura_a_dict(x) for x in lecturas if isinstance(x, FacturaLeida)],
        "mis": [fila_a_dict(f) for f in (mis.filas if mis else [])],
        "mis_cuit": mis.cuit_emisor if mis else "",
        "mis_avisos": mis.advertencias if mis else [],
    }


def de_payload(
    p: dict[str, Any],
) -> tuple[list[FacturaLeida | NoReconocido], LecturaMisComprobantes | None]:
    lecturas: list[FacturaLeida | NoReconocido] = [lectura_de_dict(d) for d in p["facturas"]]
    mis = None
    if p["mis"]:
        mis = LecturaMisComprobantes(
            [fila_de_dict(d) for d in p["mis"]],
            p.get("mis_cuit", ""),
            list(p.get("mis_avisos", [])),
        )
    return lecturas, mis


def factura_de_datos(datos: dict[str, Any]) -> FacturaLeida:
    """Arma una `FacturaLeida` con lo que extrajo una IA de una factura que no se pudo leer.

    Solo hace falta el receptor (nombre, documento, condición IVA) y los ítems; todo lo demás es
    opcional. Estos datos no se validan contra un QR, por eso nunca se registran como histórico.
    """
    try:
        doc = re.sub(r"\D", "", str(datos.get("cuit") or datos.get("dni") or ""))
        tipo = (
            DocTipo.CUIT
            if datos.get("cuit")
            else DocTipo.DNI
            if datos.get("dni")
            else DocTipo.CONSUMIDOR_FINAL
        )
        items = [
            ItemLeido(
                descripcion=str(i["descripcion"]),
                cantidad=money.parsear(str(i.get("cantidad", 1))),
                unidad=str(i.get("unidad", "unidades")),
                precio_unitario=money.parsear(str(i["precio_unitario"])),
                codigo=str(i.get("codigo", "1")),
                bonificacion_pct=money.parsear(str(i.get("bonificacion_pct", 0))),
            )
            for i in datos.get("items", [])
        ]
        concepto = Concepto(int(datos.get("concepto", 2)))
        fecha = date.fromisoformat(datos["fecha"]) if datos.get("fecha") else date.today()
        desde = date.fromisoformat(datos["periodo_desde"]) if datos.get("periodo_desde") else None
        hasta = date.fromisoformat(datos["periodo_hasta"]) if datos.get("periodo_hasta") else None
        vto = date.fromisoformat(datos["vto_pago"]) if datos.get("vto_pago") else None
    except (KeyError, ValueError, TypeError, ArithmeticError) as e:
        raise ValueError(f"Los datos de la factura no son válidos: {e}") from None
    if not datos.get("nombre"):
        raise ValueError("Falta el nombre del cliente (`nombre`).")
    if not items:
        raise ValueError(
            "Faltan los ítems (`items`: descripcion, cantidad, unidad, precio_unitario)."
        )
    if tipo != DocTipo.CONSUMIDOR_FINAL and not doc:
        raise ValueError("El documento del cliente no es válido.")
    if tipo == DocTipo.CUIT and not cuit_mod.es_valido(doc):
        raise ValueError(f"El CUIT {doc} no tiene un dígito verificador válido.")
    f = FacturaLeida(
        origen="datos",
        archivo="(datos cargados por la IA)",
        receptor_doc_tipo=int(tipo),
        receptor_doc_nro=int(doc or 0),
        receptor_nombre=str(datos["nombre"]),
        receptor_condicion_iva=str(datos.get("condicion_iva", "Consumidor Final")),
        receptor_domicilio=str(datos.get("domicilio", "")),
        concepto=concepto,
        periodo_desde=desde,
        periodo_hasta=hasta,
        vto_pago=vto,
        condicion_venta=str(datos.get("condicion_venta", "Contado")),
        items=items,
        fecha=fecha,
        total=sum((i.cantidad * i.precio_unitario for i in items), Decimal(0)),
    )
    f.advertencias.append("Datos cargados por una IA: no se validaron contra un QR.")
    return f
