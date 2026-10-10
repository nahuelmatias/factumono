"""CLI de Facturitas (mensajes y ayuda en español)."""

import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, NoReturn

import httpx
import typer
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes, PublicKeyTypes
from typer import rich_utils

from facturitas import __version__, config
from facturitas.arca import ambientes
from facturitas.arca.errors import ArcaError
from facturitas.arca.wsaa import Wsaa
from facturitas.arca.wsfe import Wsfe
from facturitas.core import cuit as cuit_mod
from facturitas.core import money
from facturitas.core.models import (
    FACTURA_C,
    Comprobante,
    Concepto,
    Credencial,
    DocTipo,
    Item,
    Perfil,
    Receptor,
)
from facturitas.core.preview import texto_preview

# Títulos de la ayuda en español (Typer los trae en inglés).
rich_utils.OPTIONS_PANEL_TITLE = "Opciones"
rich_utils.COMMANDS_PANEL_TITLE = "Comandos"
rich_utils.ARGUMENTS_PANEL_TITLE = "Argumentos"
rich_utils.ERRORS_PANEL_TITLE = "Error"

app = typer.Typer(
    help="Facturitas: facturación electrónica ARCA fácil, local y abierta.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

PerfilOpt = Annotated[
    str | None, typer.Option("--perfil", help="Nombre del perfil (por defecto, el principal).")
]


def _version(value: bool) -> None:
    if value:
        typer.echo(f"facturitas {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Muestra la versión."),
    ] = False,
) -> None:
    """Facturitas: facturación electrónica ARCA fácil, local y abierta."""


def _fallar(mensaje: str) -> NoReturn:
    typer.secho(f"✗ {mensaje}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _ejecutar(fn: Callable[[], None]) -> None:
    """Convierte las excepciones esperadas en mensajes claros y código de salida 1."""
    try:
        fn()
    except (ArcaError, config.ConfigError, ValueError) as e:
        _fallar(str(e))
    except httpx.HTTPError as e:
        _fallar(f"No se pudo comunicar con ARCA. Revisá tu conexión. ({e})")


@dataclass
class Contexto:
    perfil: Perfil
    credencial: Credencial
    wsaa: Wsaa
    wsfe: Wsfe
    etiqueta: str


def _contexto(nombre_perfil: str | None) -> Contexto:
    cfg = config.cargar()
    perfil = cfg.perfil(nombre_perfil)
    cred = cfg.credencial_de(perfil)
    amb = ambientes.obtener(cred.ambiente)  # en M1 solo existe homologación
    cache = config.directorio_cache()
    wsaa = Wsaa(cred, amb, cache)
    return Contexto(perfil, cred, wsaa, Wsfe(perfil.cuit, wsaa, amb, cache), amb.etiqueta)


def _confirmar(pregunta: str) -> bool:
    """Pregunta s/n en español (por defecto, No). Cualquier otra respuesta se vuelve a pedir."""
    while True:
        r = typer.prompt(f"{pregunta} [s/N]", default="n", show_default=False).strip().lower()
        if r in ("s", "si", "sí", "y", "yes"):
            return True
        if r in ("n", "no", ""):
            return False
        typer.echo("Respondé 's' para sí o 'n' para no.")


def _parsear_fecha(texto: str) -> date:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Fecha inválida: {texto!r}. Usá DD/MM/AAAA.")


def _fin_de_mes(d: date) -> date:
    primero_sig = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return primero_sig - timedelta(days=1)


# --- init ---------------------------------------------------------------------------------


def _publica(k: PublicKeyTypes | PrivateKeyTypes) -> bytes:
    return k.public_bytes(  # type: ignore[union-attr]
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _validar_certificado(cert: Path, key: Path) -> None:
    for ruta in (cert, key):
        if not ruta.is_file():
            raise ValueError(f"No existe el archivo: {ruta}")
    certificado = x509.load_pem_x509_certificate(cert.read_bytes())
    clave = serialization.load_pem_private_key(key.read_bytes(), password=None)
    if _publica(certificado.public_key()) != _publica(clave.public_key()):
        raise ValueError("La clave privada no corresponde a este certificado.")
    if certificado.not_valid_after_utc < datetime.now(certificado.not_valid_after_utc.tzinfo):
        raise ValueError("El certificado está vencido.")


@app.command()
def init(
    cuit: Annotated[
        str | None, typer.Option(help="CUIT representado (el de quien factura).")
    ] = None,
    cuit_operador: Annotated[
        str | None, typer.Option(help="CUIT del dueño del certificado, si es distinto.")
    ] = None,
    cert: Annotated[Path | None, typer.Option(help="Ruta al certificado (.crt).")] = None,
    key: Annotated[Path | None, typer.Option(help="Ruta a la clave privada (.key).")] = None,
    razon_social: Annotated[str | None, typer.Option(help="Razón social / nombre.")] = None,
    condicion_iva: Annotated[
        str | None, typer.Option(help="Condición frente al IVA del emisor.")
    ] = None,
    domicilio: Annotated[str | None, typer.Option(help="Domicilio comercial.")] = None,
    iibb: Annotated[str | None, typer.Option(help="Ingresos Brutos (ej.: Exento).")] = None,
    inicio_actividades: Annotated[
        str | None, typer.Option(help="Inicio de actividades (DD/MM/AAAA).")
    ] = None,
    punto_venta: Annotated[
        int | None, typer.Option(help="Punto de venta por defecto (web services).")
    ] = None,
    nombre_fantasia: Annotated[
        str | None, typer.Option(help="Nombre de fantasía que se imprime en el PDF.")
    ] = None,
    nombre: Annotated[str, typer.Option(help="Nombre con el que se guarda el perfil.")] = "default",
) -> None:
    """Registra la credencial y el perfil (en homologación). Volvé a correrlo para modificar."""

    def run() -> None:
        cfg = config.cargar()
        previo = cfg.perfiles.get(nombre)
        cred_prev = cfg.credenciales.get(previo.credencial) if previo else None

        def pedir(valor: str | None, etiqueta: str, anterior: str | None) -> str:
            if valor:
                return valor
            if anterior:
                return anterior
            return str(typer.prompt(etiqueta))

        cuit_rep = cuit_mod.normalizar(
            pedir(cuit, "CUIT representado", previo.cuit if previo else None)
        )
        cuit_op = cuit_mod.normalizar(cuit_operador or (cred_prev.cuit if cred_prev else cuit_rep))
        ruta_cert = (
            Path(
                pedir(
                    str(cert) if cert else None,
                    "Ruta al certificado (.crt)",
                    cred_prev.cert if cred_prev else None,
                )
            )
            .expanduser()
            .resolve()
        )
        ruta_key = (
            Path(
                pedir(
                    str(key) if key else None,
                    "Ruta a la clave privada (.key)",
                    cred_prev.key if cred_prev else None,
                )
            )
            .expanduser()
            .resolve()
        )
        _validar_certificado(ruta_cert, ruta_key)
        inicio = (
            _parsear_fecha(inicio_actividades)
            if inicio_actividades
            else (previo.inicio_actividades if previo else None)
        )
        perfil = Perfil(
            cuit=cuit_rep,
            razon_social=pedir(
                razon_social, "Razón social", previo.razon_social if previo else None
            ),
            condicion_iva=pedir(
                condicion_iva,
                "Condición frente al IVA (ej.: Responsable Monotributo)",
                previo.condicion_iva if previo else None,
            ),
            domicilio=pedir(domicilio, "Domicilio comercial", previo.domicilio if previo else None),
            credencial=nombre,
            iibb=iibb if iibb is not None else (previo.iibb if previo else ""),
            inicio_actividades=inicio,
            punto_venta=punto_venta
            if punto_venta is not None
            else (previo.punto_venta if previo else None),
            nombre_fantasia=nombre_fantasia
            if nombre_fantasia is not None
            else (previo.nombre_fantasia if previo else ""),
        )
        perfil.cbte_tipo_default  # noqa: B018 - valida que el tipo de emisor esté soportado
        cfg.credenciales[nombre] = Credencial(
            cuit=cuit_op, cert=str(ruta_cert), key=str(ruta_key), ambiente="homo"
        )
        cfg.perfiles[nombre] = perfil
        cfg.perfil_default = cfg.perfil_default if previo else nombre
        config.guardar(cfg)
        typer.secho(
            f"✓ Perfil '{nombre}' guardado en {config.ruta_config()}", fg=typer.colors.GREEN
        )
        typer.echo("  Ambiente: HOMOLOGACIÓN. Siguiente paso: `facturitas estado`.")

    _ejecutar(run)


# --- estado / ultimo / condiciones-iva ---------------------------------------------------------


@app.command()
def estado(
    perfil: PerfilOpt = None,
    offline: Annotated[bool, typer.Option("--offline", help="No consulta ARCA.")] = False,
) -> None:
    """Chequea la conexión con ARCA (FEDummy), el acceso y los puntos de venta."""

    def run() -> None:
        cfg = config.cargar()
        p = cfg.perfil(perfil)
        cred = cfg.credencial_de(p)
        amb = ambientes.obtener(cred.ambiente)
        typer.echo(f"Ambiente: {amb.etiqueta}")
        typer.echo(
            f"Perfil:   {p.razon_social} — CUIT {cuit_mod.formatear(p.cuit)} ({p.condicion_iva})"
        )
        typer.echo(
            f"Punto de venta por defecto: {p.punto_venta if p.punto_venta else 'sin definir'}"
        )
        if offline:
            typer.echo("(modo offline: no se consultó ARCA)")
            return
        ctx = _contexto(perfil)
        d = ctx.wsfe.dummy()
        ok = all(v == "OK" for v in d.values())
        typer.secho(
            f"{'✓' if ok else '✗'} FEDummy: AppServer={d['AppServer']} DbServer={d['DbServer']} "
            f"AuthServer={d['AuthServer']}",
            fg=typer.colors.GREEN if ok else typer.colors.RED,
        )
        ticket = ctx.wsaa.ticket("wsfe")
        typer.secho(
            f"✓ Autenticación OK (ticket vigente hasta {ticket.vencimiento:%d/%m/%Y %H:%M})",
            fg=typer.colors.GREEN,
        )
        pvs = ctx.wsfe.puntos_venta()
        if not pvs:
            typer.secho(
                "✗ No hay puntos de venta habilitados para web services.", fg=typer.colors.YELLOW
            )
            typer.echo(
                "  Hace falta uno de tipo 'RECE para aplicativo y web services'. Los de\n"
                "  'Comprobantes en línea' no sirven. Ver el README (sección de puntos de venta)."
            )
            return
        typer.echo("Puntos de venta (web services):")
        for pv in pvs:
            marca = " (por defecto)" if pv.nro == p.punto_venta else ""
            baja = f", baja {pv.fecha_baja}" if pv.fecha_baja and pv.fecha_baja != "NULL" else ""
            bloq = ", BLOQUEADO" if pv.bloqueado else ""
            typer.echo(f"  - {pv.nro:05d}  {pv.emision_tipo}{bloq}{baja}{marca}")
        if p.punto_venta and p.punto_venta not in {pv.nro for pv in pvs}:
            typer.secho(
                f"⚠ El punto de venta por defecto ({p.punto_venta}) no figura entre los "
                "de web services.",
                fg=typer.colors.YELLOW,
            )

    _ejecutar(run)


def _pto_vta(ctx: Contexto, pto_vta: int | None) -> int:
    pv = pto_vta or ctx.perfil.punto_venta
    if not pv:
        raise ValueError(
            "Falta el punto de venta: pasalo con --pto-vta o definilo con `facturitas init "
            "--punto-venta N` (podés ver los disponibles con `facturitas estado`)."
        )
    return pv


@app.command()
def ultimo(
    perfil: PerfilOpt = None,
    pto_vta: Annotated[int | None, typer.Option(help="Punto de venta.")] = None,
    tipo: Annotated[int | None, typer.Option(help="Tipo de comprobante (11 = Factura C).")] = None,
) -> None:
    """Muestra el último comprobante autorizado de un punto de venta y tipo."""

    def run() -> None:
        ctx = _contexto(perfil)
        pv = _pto_vta(ctx, pto_vta)
        t = tipo or ctx.perfil.cbte_tipo_default
        n = ctx.wsfe.ultimo_autorizado(pv, t)
        typer.echo(f"Ambiente: {ctx.etiqueta}")
        typer.echo(f"Último comprobante tipo {t:03d}, punto de venta {pv:05d}: Nro. {n:08d}")

    _ejecutar(run)


@app.command("condiciones-iva")
def condiciones_iva(perfil: PerfilOpt = None) -> None:
    """Lista las condiciones frente al IVA del receptor válidas para Factura C."""

    def run() -> None:
        ctx = _contexto(perfil)
        for c in ctx.wsfe.condiciones_iva_receptor("C"):
            typer.echo(f"  {c.id:>3}  {c.descripcion}")

    _ejecutar(run)


# --- emitir -------------------------------------------------------------------------------


@app.command()
def emitir(
    descripcion: Annotated[str, typer.Option("--descripcion", help="Producto / servicio.")],
    precio: Annotated[
        str, typer.Option("--precio", help="Precio unitario (ej.: 1500 o 1.500,50).")
    ],
    perfil: PerfilOpt = None,
    pto_vta: Annotated[int | None, typer.Option(help="Punto de venta.")] = None,
    cantidad: Annotated[str, typer.Option(help="Cantidad.")] = "1",
    unidad: Annotated[str, typer.Option(help="Unidad de medida.")] = "otras unidades",
    codigo: Annotated[str, typer.Option(help="Código del ítem.")] = "1",
    concepto: Annotated[
        str, typer.Option(help="'servicios' (con período) o 'productos'.")
    ] = "servicios",
    receptor_cuit: Annotated[str | None, typer.Option(help="CUIT del receptor.")] = None,
    receptor_dni: Annotated[str | None, typer.Option(help="DNI del receptor.")] = None,
    receptor_nombre: Annotated[
        str, typer.Option(help="Nombre o razón social del receptor.")
    ] = "Consumidor Final",
    receptor_domicilio: Annotated[str, typer.Option(help="Domicilio del receptor.")] = "",
    condicion_iva: Annotated[
        str, typer.Option(help="Condición IVA del receptor (texto o código).")
    ] = "Consumidor Final",
    desde: Annotated[
        str | None,
        typer.Option(help="Inicio del período (DD/MM/AAAA). Por defecto, inicio del mes."),
    ] = None,
    hasta: Annotated[
        str | None, typer.Option(help="Fin del período. Por defecto, fin del mes.")
    ] = None,
    vto_pago: Annotated[str | None, typer.Option(help="Vencimiento de pago (DD/MM/AAAA).")] = None,
    vto_dias: Annotated[int, typer.Option(help="Si no hay --vto-pago: días desde hoy.")] = 4,
    condicion_venta: Annotated[str, typer.Option(help="Condición de venta.")] = "Contado",
    yes: Annotated[bool, typer.Option("--yes", help="No pregunta (para scripts y tests).")] = False,
) -> None:
    """Prepara una Factura C, muestra el preview y, tras tu confirmación, pide el CAE."""

    def run() -> None:
        ctx = _contexto(perfil)
        hoy = date.today()
        pv = _pto_vta(ctx, pto_vta)
        if ctx.perfil.cbte_tipo_default != FACTURA_C:
            raise ValueError("Por ahora solo se emite Factura C.")
        cond = ctx.wsfe.resolver_condicion_iva(condicion_iva, "C")
        if receptor_cuit:
            r_tipo, r_nro = DocTipo.CUIT, int(cuit_mod.normalizar(receptor_cuit))
        elif receptor_dni:
            r_tipo, r_nro = DocTipo.DNI, int(receptor_dni)
        else:
            r_tipo, r_nro = DocTipo.CONSUMIDOR_FINAL, 0
        receptor = Receptor(
            doc_tipo=int(r_tipo),
            doc_nro=r_nro,
            nombre=receptor_nombre,
            condicion_iva_id=cond.id,
            condicion_iva=cond.descripcion,
            domicilio=receptor_domicilio,
        )
        es_servicio = concepto.lower().startswith("serv")
        if not es_servicio and not concepto.lower().startswith("prod"):
            raise ValueError("--concepto debe ser 'servicios' o 'productos'.")
        c = Comprobante(
            perfil=ctx.perfil,
            punto_venta=pv,
            cbte_tipo=FACTURA_C,
            concepto=Concepto.SERVICIOS if es_servicio else Concepto.PRODUCTOS,
            receptor=receptor,
            items=(
                Item(
                    descripcion=descripcion,
                    cantidad=Decimal(money.parsear(cantidad)),
                    precio_unitario=money.parsear(precio),
                    unidad=unidad,
                    codigo=codigo,
                ),
            ),
            fecha=hoy,
            condicion_venta=condicion_venta,
        )
        if es_servicio:
            c = replace(
                c,
                periodo_desde=_parsear_fecha(desde) if desde else hoy.replace(day=1),
                periodo_hasta=_parsear_fecha(hasta) if hasta else _fin_de_mes(hoy),
                vto_pago=_parsear_fecha(vto_pago) if vto_pago else hoy + timedelta(days=vto_dias),
            )
        c.validar()
        numero = ctx.wsfe.proximo_numero(pv, FACTURA_C)
        typer.echo(texto_preview(c, ctx.etiqueta, numero))
        if not yes and not _confirmar(
            f"¿Emitir este comprobante en {ctx.etiqueta.split(' (')[0]}?"
        ):
            typer.echo("Cancelado: no se emitió nada.")
            raise typer.Exit(0)
        r = ctx.wsfe.emitir(c)
        typer.secho(f"✓ Comprobante autorizado — Nro. {r.numero:08d}", fg=typer.colors.GREEN)
        typer.echo(f"  CAE: {r.cae}   Vto. CAE: {r.cae_vto}")
        for o in r.observaciones:
            typer.secho(f"  ⚠ {o}", fg=typer.colors.YELLOW)

    _ejecutar(run)


def main() -> None:
    # Que los acentos salgan bien aunque la consola no esté en UTF-8 (Windows, pipes).
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")
    app()
