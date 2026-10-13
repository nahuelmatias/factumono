"""CLI de Factumono (mensajes y ayuda en español)."""

import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, NoReturn

import httpx
import typer
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes, PublicKeyTypes
from typer import rich_utils

from factumono import __version__, config, migracion
from factumono.arca import ambientes
from factumono.arca.errors import ArcaError, traducir_error_red
from factumono.contexto import Contexto, construir
from factumono.core import armado, money, service
from factumono.core import cuit as cuit_mod
from factumono.core.diagnostico import (
    Diagnostico,
    diagnosticar,
    esta_dado_de_baja,
    problemas_para_activar_produccion,
)
from factumono.core.fechas import mostrar as fecha_ar
from factumono.core.models import (
    FACTURA_C,
    MODOS_PERIODO,
    Cliente,
    Comprobante,
    Credencial,
    DocTipo,
    Item,
    Perfil,
    Receptor,
)
from factumono.core.preview import texto_preview
from factumono.core.sincronia import (
    DESCRIPCION_RECUPERADO,
    Brecha,
    brechas,
    recuperar_brechas,
    recuperar_numero,
    total_faltantes,
)
from factumono.pdf.salida import guardar_pdf
from factumono.storage import backup as backup_mod
from factumono.storage.historial import AUTORIZADO, Historial, Registro

# Títulos de la ayuda en español (Typer los trae en inglés).
rich_utils.OPTIONS_PANEL_TITLE = "Opciones"
rich_utils.COMMANDS_PANEL_TITLE = "Comandos"
rich_utils.ARGUMENTS_PANEL_TITLE = "Argumentos"
rich_utils.ERRORS_PANEL_TITLE = "Error"

app = typer.Typer(
    help="Factumono: facturación electrónica ARCA fácil, local y abierta.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

PerfilOpt = Annotated[
    str | None, typer.Option("--perfil", help="Nombre del perfil (por defecto, el principal).")
]


def _version(value: bool) -> None:
    if value:
        typer.echo(f"factumono {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Muestra la versión."),
    ] = False,
) -> None:
    """Factumono: facturación electrónica ARCA fácil, local y abierta."""


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
        _fallar(traducir_error_red(e))


def _banner_prod(ambiente: str) -> None:
    """En producción, toda salida de la CLI arranca con este aviso (en homo no hace falta)."""
    amb = ambientes.obtener(ambiente)
    if amb.es_produccion:
        typer.secho(amb.banner, fg=typer.colors.RED, bold=True)


def _contexto(nombre_perfil: str | None, ambiente: str | None = None) -> Contexto:
    ctx = construir(nombre_perfil, ambiente)
    _banner_prod(ctx.ambiente)
    return ctx


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
    ambiente: Annotated[
        str | None,
        typer.Option(
            help="'homo' (por defecto) o 'prod': registra el certificado y el punto de venta "
            "de producción. No activa producción: eso es `factumono ambiente usar prod`."
        ),
    ] = None,
    aviso_duplicados: Annotated[
        bool | None,
        typer.Option(
            "--aviso-duplicados/--sin-aviso-duplicados",
            help="Avisar (solo informativo) si un comprobante se parece a uno ya emitido.",
        ),
    ] = None,
) -> None:
    """Asistente de configuración (sin opciones) o registro directo de credencial y perfil.

    Sin opciones guía paso a paso: genera la clave y el CSR, explica los trámites en ARCA, valida
    el certificado y prueba la conexión. Con opciones (--cert, --key, ...) registra los datos
    directamente. Se puede cortar y retomar.
    """

    def run() -> None:
        sin_opciones = all(
            v is None
            for v in (cuit, cuit_operador, cert, key, razon_social, condicion_iva, domicilio,
                      iibb, inicio_actividades, punto_venta, nombre_fantasia, aviso_duplicados)
        ) and nombre == "default"  # fmt: skip
        if sin_opciones:
            _asistente(ambiente)
            return
        cfg = config.cargar()
        previo = cfg.perfiles.get(nombre)
        if ambientes.obtener(ambiente or "homo").es_produccion:
            _init_produccion(
                cfg, nombre, previo, cuit_operador, cert, key, punto_venta, aviso_duplicados
            )
            return
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
            credencial_prod=previo.credencial_prod if previo else "",
            punto_venta_prod=previo.punto_venta_prod if previo else None,
            aviso_duplicados=aviso_duplicados
            if aviso_duplicados is not None
            else (previo.aviso_duplicados if previo else True),
            categoria_monotributo=previo.categoria_monotributo if previo else "",
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
        typer.echo("  Ambiente: HOMOLOGACIÓN. Siguiente paso: `factumono estado`.")

    _ejecutar(run)


class _Consola:
    """Entrada/salida del asistente sobre la terminal."""

    def preguntar(self, texto: str, defecto: str | None = None) -> str:
        return str(
            typer.prompt(
                texto, default=defecto if defecto is not None else "", show_default=bool(defecto)
            )
        )

    def confirmar(self, texto: str, defecto: bool = True) -> bool:
        r = typer.prompt(f"{texto} [{'S/n' if defecto else 's/N'}]", default="", show_default=False)
        r = str(r).strip().lower()
        if not r:
            return defecto
        return r in ("s", "si", "sí", "y", "yes")

    def mostrar(self, texto: str = "") -> None:
        typer.echo(texto)

    def abrir(self, url: str) -> None:
        import webbrowser

        webbrowser.open(url)


def _verificar_estado(perfil: str, ambiente: str) -> bool:
    """Corre `estado` (solo lectura) y dice si salió bien."""
    try:
        estado(perfil=perfil, offline=False, ambiente=ambiente)
    except typer.Exit as e:
        return e.exit_code == 0
    return True


def _asistente(ambiente: str | None) -> None:
    from factumono.cli.asistente import Asistente

    if not sys.stdin.isatty():
        raise ValueError(
            "El asistente es interactivo. En un script usá las opciones: `factumono init "
            "--cuit ... --cert ... --key ... --razon-social ... --condicion-iva ... "
            "--domicilio ...`."
        )
    Asistente(_Consola(), _verificar_estado).correr(ambiente)


def _init_produccion(
    cfg: config.Config,
    nombre: str,
    previo: Perfil | None,
    cuit_operador: str | None,
    cert: Path | None,
    key: Path | None,
    punto_venta: int | None,
    aviso_duplicados: bool | None,
) -> None:
    """Registra el certificado real y el punto de venta de producción (sin activar producción)."""
    if previo is None:
        raise ValueError(
            "Primero configurá el perfil con `factumono init` (homologación); después "
            "agregás producción con `--ambiente prod`."
        )
    cred_prev = cfg.credenciales.get(previo.credencial_prod)
    cred_homo = cfg.credenciales.get(previo.credencial)
    carpeta = Path.home() / ".factumono" / "prod"

    def ruta(valor: Path | None, anterior: str | None, etiqueta: str, por_defecto: Path) -> Path:
        if valor:
            return valor.expanduser().resolve()
        if anterior:
            return Path(anterior)
        if por_defecto.is_file():
            return por_defecto
        return Path(str(typer.prompt(etiqueta))).expanduser().resolve()

    ruta_cert = ruta(
        cert,
        cred_prev.cert if cred_prev else None,
        "Ruta al certificado de PRODUCCIÓN (.crt)",
        carpeta / "factumono-prod.crt",
    )
    ruta_key = ruta(
        key,
        cred_prev.key if cred_prev else None,
        "Ruta a la clave privada de PRODUCCIÓN (.key)",
        carpeta / "factumono-prod.key",
    )
    _validar_certificado(ruta_cert, ruta_key)
    cuit_op = cuit_mod.normalizar(
        cuit_operador
        or (cred_prev.cuit if cred_prev else (cred_homo.cuit if cred_homo else previo.cuit))
    )
    pv = punto_venta or previo.punto_venta_prod
    if not pv:
        pv = int(typer.prompt("Punto de venta de PRODUCCIÓN (web services)"))
    nombre_cred = f"{nombre}-prod"
    cfg.credenciales[nombre_cred] = Credencial(
        cuit=cuit_op, cert=str(ruta_cert), key=str(ruta_key), ambiente="prod"
    )
    cfg.perfiles[nombre] = replace(
        previo,
        credencial_prod=nombre_cred,
        punto_venta_prod=pv,
        aviso_duplicados=previo.aviso_duplicados if aviso_duplicados is None else aviso_duplicados,
    )
    config.guardar(cfg)
    typer.secho(
        f"✓ Producción registrada para el perfil '{nombre}' (punto de venta {pv}).",
        fg=typer.colors.GREEN,
    )
    typer.echo(
        "  Producción NO está activa: el ambiente sigue siendo "
        f"{ambientes.obtener(cfg.ambiente).titulo}.\n"
        "  Siguiente paso: `factumono estado --ambiente prod` y, si da todo OK, "
        "`factumono ambiente usar prod`."
    )


# --- estado / ultimo / condiciones-iva ---------------------------------------------------------


AmbienteOpt = Annotated[
    str | None,
    typer.Option("--ambiente", help="'homo' o 'prod' (por defecto, el ambiente activo)."),
]


def _mostrar_puntos_de_venta(d: Diagnostico) -> None:
    typer.echo("Puntos de venta (web services):")
    for pv in d.pvs:
        marca = "   ← configurado" if pv.nro == d.pv_configurado else ""
        baja = f", baja {pv.fecha_baja}" if esta_dado_de_baja(pv) else ""
        bloq = ", BLOQUEADO" if pv.bloqueado else ""
        typer.echo(
            f"  - {pv.nro:05d}  {pv.emision_tipo or '(sin tipo informado)'}{bloq}{baja}{marca}"
        )


@app.command()
def estado(
    perfil: PerfilOpt = None,
    offline: Annotated[bool, typer.Option("--offline", help="No consulta ARCA.")] = False,
    ambiente: AmbienteOpt = None,
) -> None:
    """Chequea la conexión con ARCA (FEDummy), el acceso y los puntos de venta."""

    def run() -> None:
        cfg = config.cargar()
        amb = ambientes.obtener(ambiente or cfg.ambiente)
        p, _ = cfg.perfil_para(perfil, amb.nombre)
        ctx = None if offline else _contexto(perfil, amb.nombre)  # imprime el aviso de prod
        if offline:
            _banner_prod(amb.nombre)
        typer.echo(f"Ambiente: {amb.etiqueta}")
        if amb.nombre != cfg.ambiente:
            typer.echo(
                f"(el ambiente activo es {ambientes.obtener(cfg.ambiente).titulo}; "
                "esto es solo un chequeo)"
            )
        typer.echo(
            f"Perfil:   {p.razon_social} — CUIT {cuit_mod.formatear(p.cuit)} ({p.condicion_iva})"
        )
        typer.echo(
            f"Punto de venta configurado: {p.punto_venta if p.punto_venta else 'sin definir'}"
        )
        if offline:
            typer.echo("(modo offline: no se consultó ARCA)")
            return
        assert ctx is not None
        d = diagnosticar(ctx)
        if d.dummy:
            typer.secho(
                f"{'✓' if d.dummy_ok else '✗'} FEDummy: AppServer={d.dummy['AppServer']} "
                f"DbServer={d.dummy['DbServer']} AuthServer={d.dummy['AuthServer']}",
                fg=typer.colors.GREEN if d.dummy_ok else typer.colors.RED,
            )
        if d.ticket_vence:
            typer.secho(
                f"✓ Autenticación OK (ticket vigente hasta {d.ticket_vence:%d/%m/%Y %H:%M})",
                fg=typer.colors.GREEN,
            )
        if d.pvs:
            _mostrar_puntos_de_venta(d)
        elif d.ticket_vence and not amb.es_produccion:
            typer.secho(
                "ℹ Sin puntos de venta listados: en homologación es lo habitual y no impide "
                "emitir (ARCA acepta cualquier número).",
                fg=typer.colors.CYAN,
            )
        if d.ticket_vence:
            _avisar_brecha(ctx, True)
        for aviso in d.avisos:
            typer.secho(f"⚠ {aviso}", fg=typer.colors.YELLOW)
        for problema in d.problemas:
            typer.secho(f"✗ {problema}", fg=typer.colors.RED)
        if amb.es_produccion:
            if d.ok:
                typer.secho(
                    "✓ Producción lista: FEDummy, autenticación y punto de venta en orden.",
                    fg=typer.colors.GREEN,
                )
            else:
                raise typer.Exit(1)

    _ejecutar(run)


def _pto_vta(ctx: Contexto, pto_vta: int | None) -> int:
    pv = pto_vta or ctx.perfil.punto_venta
    if not pv:
        raise ValueError(
            "Falta el punto de venta: pasalo con --pto-vta o definilo con `factumono init"
            f"{' --ambiente prod' if ctx.es_produccion else ''} --punto-venta N` "
            "(podés ver los disponibles con `factumono estado`)."
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


# --- emisión ------------------------------------------------------------------------------

_ABREVIATURAS = {1: "FA", 3: "NCA", 6: "FB", 8: "NCB", 11: "FC", 13: "NCC"}


def _nombre_corto(tipo: int, pv: int, numero: int | None) -> str:
    nro = f"{numero:08d}" if numero is not None else "--------"
    return f"{_ABREVIATURAS.get(tipo, str(tipo))} {pv:05d}-{nro}"


def _historial(ctx: Contexto) -> Historial:
    assert ctx.historial is not None
    return ctx.historial


def _resumen(r: Registro) -> str:
    return (
        f"{_nombre_corto(r.cbte_tipo, r.punto_venta, r.numero)} del {fecha_ar(r.fecha)}, "
        f"$ {money.formatear_ar(r.importe)} a {r.receptor_nombre}"
    )


def _pdf_o_aviso(reg: Registro) -> None:
    try:
        typer.echo(f"  PDF: {guardar_pdf(config.cargar(), reg)}")
    except Exception as e:  # el CAE ya está: un problema con el PDF no tiene que taparlo
        typer.secho(
            f"  ⚠ No se pudo generar el PDF ({e}). Reintentá con `factumono pdf {reg.numero}`.",
            fg=typer.colors.YELLOW,
        )


def _emitir_y_mostrar(ctx: Contexto, c: Comprobante, clave: str) -> None:
    """Emite con idempotencia + reconciliación, muestra el resultado y guarda el PDF."""
    em = service.emitir(
        _historial(ctx), ctx.wsfe, perfil=ctx.perfil_nombre, ambiente=ctx.ambiente, c=c, clave=clave
    )
    reg = em.registro
    if em.reutilizado:
        typer.secho(
            "ℹ Esa clave de idempotencia ya estaba autorizada: no se emitió otro comprobante.",
            fg=typer.colors.CYAN,
        )
    elif em.reconciliado:
        typer.secho(
            "✓ ARCA confirma que el comprobante ya había quedado autorizado (se cortó la "
            "comunicación, pero no se emitió dos veces).",
            fg=typer.colors.GREEN,
        )
    assert reg.numero is not None
    typer.secho(
        f"✓ Comprobante autorizado — Nro. {reg.numero:08d}"
        f"{' (HOMOLOGACIÓN)' if reg.es_homologacion else ''}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  CAE: {reg.cae}   Vto. CAE: {fecha_ar(reg.cae_vto)}")
    for o in reg.observaciones:
        typer.secho(f"  ⚠ {o}", fg=typer.colors.YELLOW)
    _pdf_o_aviso(reg)


def _confirmar_emision(
    ctx: Contexto,
    preview: str,
    yes: bool,
    duplicados: list[Registro],
    que: str = "este comprobante",
) -> None:
    """Muestra el preview y pide confirmación. Los parecidos son solo informativos."""
    typer.echo(preview)
    if duplicados:
        typer.secho(
            "ℹ Ojo: ya emitiste algo parecido (mismo receptor, importe y período). "
            "Es solo un aviso:",
            fg=typer.colors.CYAN,
        )
        for d in duplicados:
            typer.secho(f"    - {_resumen(d)}", fg=typer.colors.CYAN)
    if yes:
        return
    if not _confirmar(f"¿Emitir {que} en {ctx.nombre_ambiente}?"):
        typer.echo("Cancelado: no se emitió nada.")
        raise typer.Exit(0)


@app.command()
def emitir(
    descripcion: Annotated[
        str | None, typer.Option("--descripcion", help="Producto / servicio.")
    ] = None,
    precio: Annotated[
        str | None, typer.Option("--precio", help="Precio unitario (ej.: 1500 o 1.500,50).")
    ] = None,
    perfil: PerfilOpt = None,
    cliente: Annotated[
        str | None,
        typer.Option("--cliente", help="Alias de un cliente guardado: usa sus datos y defaults."),
    ] = None,
    repetir_ultima: Annotated[
        bool,
        typer.Option(
            "--repetir-ultima",
            help="Repite la última factura a ese cliente con el período corrido al mes que sigue.",
        ),
    ] = False,
    periodo: Annotated[
        str | None,
        typer.Option(
            help="Período: 'septiembre', '09/2026' o 'DD/MM/AAAA al DD/MM/AAAA' (servicios)."
        ),
    ] = None,
    pto_vta: Annotated[int | None, typer.Option(help="Punto de venta.")] = None,
    cantidad: Annotated[str, typer.Option(help="Cantidad.")] = "1",
    unidad: Annotated[
        str | None, typer.Option(help="Unidad de medida (por defecto la del cliente).")
    ] = None,
    codigo: Annotated[str, typer.Option(help="Código del ítem.")] = "1",
    concepto: Annotated[
        str | None,
        typer.Option(
            help="1 = productos, 2 = servicios (con período), 3 = productos y servicios "
            "(con período). Por defecto, el del cliente."
        ),
    ] = None,
    modo_periodo: Annotated[
        str | None,
        typer.Option(
            "--modo-periodo",
            help="Cómo armar el período: mes, dia (día de emisión), rango o sin. Por defecto, "
            "el del cliente.",
        ),
    ] = None,
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
    vto_dias: Annotated[
        int | None, typer.Option(help="Si no hay --vto-pago: días desde hoy (por defecto 4).")
    ] = None,
    condicion_venta: Annotated[str, typer.Option(help="Condición de venta.")] = "Contado",
    clave: Annotated[
        str | None,
        typer.Option(
            "--clave-idempotencia",
            help="Clave para no emitir dos veces lo mismo (por defecto, una nueva).",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pregunta (para scripts y tests).")] = False,
) -> None:
    """Prepara una Factura C, muestra el preview y, tras tu confirmación, pide el CAE."""

    def run() -> None:
        ctx = _contexto(perfil)
        historial_ = _historial(ctx)
        hoy = date.today()
        pv = _pto_vta(ctx, pto_vta)
        if ctx.perfil.cbte_tipo_default != FACTURA_C:
            raise ValueError("Por ahora solo se emite Factura C.")
        if periodo and (desde or hasta):
            raise ValueError("Usá --periodo o --desde/--hasta, no los dos.")
        p_desde = _parsear_fecha(desde) if desde else None
        p_hasta = _parsear_fecha(hasta) if hasta else None
        if periodo:
            p_desde, p_hasta = armado.parsear_periodo(periodo, hoy)
        p_vto = _parsear_fecha(vto_pago) if vto_pago else None
        precio_dec = money.parsear(precio) if precio else None
        if repetir_ultima and not cliente:
            raise ValueError("--repetir-ultima necesita --cliente.")

        if cliente:
            cli = ctx.clientes.obtener(ctx.perfil_nombre, cliente)
            if repetir_ultima:
                ultima = armado.ultima_factura(historial_, ctx.perfil.cuit, ctx.ambiente, cli)
                c = armado.repetir_ultima(
                    ultima,
                    hoy=hoy,
                    importe=precio_dec,
                    desde=p_desde,
                    hasta=p_hasta,
                    vto_dias=vto_dias if vto_dias is not None else cli.vto_dias,
                    modo=modo_periodo or cli.modo_periodo,
                )
                if p_vto:
                    c = replace(c, vto_pago=p_vto)
                typer.echo(
                    f"Repitiendo {_resumen(ultima)}"
                    + (
                        f"\nPeríodo corrido a {fecha_ar(c.periodo_desde)} al "
                        f"{fecha_ar(c.periodo_hasta)}."
                        if c.periodo_desde
                        else ""
                    )
                )
            else:
                if precio_dec is None:
                    raise ValueError("Falta --precio (o usá --repetir-ultima).")
                item = armado.item_de_cliente(cli, precio_dec, descripcion)
                item = replace(
                    item,
                    cantidad=Decimal(money.parsear(cantidad)),
                    unidad=unidad or cli.unidad,
                    codigo=codigo,
                )
                c = armado.armar_comprobante(
                    perfil=ctx.perfil,
                    punto_venta=pv,
                    cbte_tipo=FACTURA_C,
                    receptor=armado.receptor_de_cliente(ctx.wsfe, cli),
                    items=(item,),
                    concepto=concepto or cli.concepto,
                    hoy=hoy,
                    desde=p_desde,
                    hasta=p_hasta,
                    vto_pago=p_vto,
                    vto_dias=vto_dias if vto_dias is not None else cli.vto_dias,
                    condicion_venta=condicion_venta,
                    modo_periodo=modo_periodo or cli.modo_periodo,
                )
        else:
            if not descripcion or precio_dec is None:
                raise ValueError("Falta --descripcion y --precio (o usá --cliente).")
            cond = ctx.wsfe.resolver_condicion_iva(condicion_iva, "C")
            if receptor_cuit:
                r_tipo, r_nro = DocTipo.CUIT, int(cuit_mod.normalizar(receptor_cuit))
            elif receptor_dni:
                r_tipo, r_nro = DocTipo.DNI, int(receptor_dni)
            else:
                r_tipo, r_nro = DocTipo.CONSUMIDOR_FINAL, 0
            c = armado.armar_comprobante(
                perfil=ctx.perfil,
                punto_venta=pv,
                cbte_tipo=FACTURA_C,
                receptor=Receptor(
                    doc_tipo=int(r_tipo),
                    doc_nro=r_nro,
                    nombre=receptor_nombre,
                    condicion_iva_id=cond.id,
                    condicion_iva=cond.descripcion,
                    domicilio=receptor_domicilio,
                ),
                items=(
                    Item(
                        descripcion=descripcion,
                        cantidad=Decimal(money.parsear(cantidad)),
                        precio_unitario=precio_dec,
                        unidad=unidad or "otras unidades",
                        codigo=codigo,
                    ),
                ),
                concepto=concepto or "servicios",
                hoy=hoy,
                desde=p_desde,
                hasta=p_hasta,
                vto_pago=p_vto,
                vto_dias=vto_dias if vto_dias is not None else 4,
                condicion_venta=condicion_venta,
                modo_periodo=modo_periodo or "mes",
            )
        c.validar()
        _avisar_brecha(ctx, yes)
        numero = ctx.wsfe.proximo_numero(pv, FACTURA_C)
        duplicados = ctx.posibles_duplicados(c)
        preview = texto_preview(c, ctx.etiqueta, numero, ctx.aviso_ambiente)
        _confirmar_emision(ctx, preview, yes, duplicados)
        _emitir_y_mostrar(ctx, c, clave or str(uuid.uuid4()))

    _ejecutar(run)


# --- nota de crédito ----------------------------------------------------------------------


def _buscar(ctx: Contexto, numero: int, pto_vta: int | None, tipo: int | None) -> Registro:
    pv = _pto_vta(ctx, pto_vta)
    t = tipo or ctx.perfil.cbte_tipo_default
    reg = _historial(ctx).por_numero(ctx.perfil.cuit, ctx.ambiente, pv, t, numero)
    if not reg:
        raise ValueError(
            f"No hay un comprobante tipo {t:03d} {pv:05d}-{numero:08d} autorizado en el "
            "historial de este ambiente. Si lo emitiste antes de usar el historial, "
            f"recuperalo con `factumono recuperar {numero}`."
        )
    return reg


@app.command("nota-credito")
def nota_credito(
    numero: Annotated[int, typer.Argument(help="Número de la factura a anular.")],
    motivo: Annotated[str, typer.Option("--motivo", help="Motivo de la anulación.")],
    perfil: PerfilOpt = None,
    pto_vta: Annotated[int | None, typer.Option(help="Punto de venta.")] = None,
    tipo: Annotated[int | None, typer.Option(help="Tipo de la factura (11 = Factura C).")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pregunta (para scripts y tests).")] = False,
) -> None:
    """Anula por completo una factura emitiendo la Nota de Crédito C asociada."""

    def run() -> None:
        ctx = _contexto(perfil)
        historial = _historial(ctx)
        _avisar_brecha(ctx, yes)
        original = _buscar(ctx, numero, pto_vta, tipo)
        previas = historial.notas_de_credito_de(original.id)
        nc = service.nota_de_credito(historial, original, motivo)
        proximo = ctx.wsfe.proximo_numero(nc.punto_venta, nc.cbte_tipo)
        typer.echo(texto_preview(nc, ctx.etiqueta, proximo, ctx.aviso_ambiente))
        if previas:
            typer.secho(
                f"⚠ Esta factura ya tiene una nota de crédito: {_resumen(previas[0])}.",
                fg=typer.colors.YELLOW,
            )
        if not yes:
            if not _confirmar(f"¿Emitir la nota de crédito en {ctx.nombre_ambiente}?"):
                typer.echo("Cancelado: no se emitió nada.")
                raise typer.Exit(0)
            if previas and not _confirmar("Ya fue anulada. ¿Emitir otra nota de crédito igual?"):
                typer.echo("Cancelado: no se emitió nada.")
                raise typer.Exit(0)
        _emitir_y_mostrar(ctx, nc, str(uuid.uuid4()))

    _ejecutar(run)


# --- historial / pdf / reconciliar / recuperar / config -----------------------------------


@app.command()
def historial(
    perfil: PerfilOpt = None,
    buscar: Annotated[
        str | None, typer.Option("--buscar", help="Receptor, CUIT/DNI, número o CAE.")
    ] = None,
    limite: Annotated[int, typer.Option(help="Cuántos mostrar (los más recientes).")] = 20,
    ambiente: AmbienteOpt = None,
) -> None:
    """Lista lo emitido en el ambiente activo (cada ambiente tiene su propio historial)."""

    def run() -> None:
        amb = ambientes.obtener(ambiente or config.cargar().ambiente)
        _banner_prod(amb.nombre)
        filas = Historial(config.ruta_historial(amb.nombre)).listar(
            perfil=perfil, texto=buscar, limite=limite
        )
        if not filas:
            typer.echo("Todavía no hay comprobantes en el historial.")
            return
        typer.echo(
            f"{'ID':>4}  {'Fecha':<10}  {'Amb.':<4}  {'Comprobante':<19}  "
            f"{'Receptor':<24}  {'Importe':>16}  {'Estado':<10}  CAE"
        )
        for r in filas:
            marca_amb = "HOMO" if r.es_homologacion else "PROD"
            typer.echo(
                f"{r.id:>4}  {fecha_ar(r.fecha):<10}  {marca_amb:<4}  "
                f"{_nombre_corto(r.cbte_tipo, r.punto_venta, r.numero):<19}  "
                f"{r.receptor_nombre[:24]:<24}  {money.formatear_ar(r.importe):>16}  "
                f"{r.estado:<10}  {r.cae}"
            )
        sin_resolver = [r for r in filas if r.estado in ("pendiente", "incierto")]
        if sin_resolver:
            typer.secho(
                f"⚠ {len(sin_resolver)} comprobante(s) sin resolver (se cortó la comunicación): "
                "corré `factumono reconciliar`.",
                fg=typer.colors.YELLOW,
            )

    _ejecutar(run)


@app.command()
def pdf(
    numero: Annotated[int, typer.Argument(help="Número del comprobante.")],
    perfil: PerfilOpt = None,
    pto_vta: Annotated[int | None, typer.Option(help="Punto de venta.")] = None,
    tipo: Annotated[int | None, typer.Option(help="Tipo (11 = Factura C, 13 = NC C).")] = None,
    carpeta: Annotated[
        Path | None, typer.Option(help="Carpeta de salida (por defecto, la configurada).")
    ] = None,
) -> None:
    """Genera (o reimprime) el PDF de un comprobante autorizado."""

    def run() -> None:
        ctx = _contexto(perfil)
        reg = _buscar(ctx, numero, pto_vta, tipo)
        ruta = guardar_pdf(config.cargar(), reg, carpeta)
        typer.secho(f"✓ PDF generado: {ruta}", fg=typer.colors.GREEN)

    _ejecutar(run)


@app.command()
def reconciliar(perfil: PerfilOpt = None) -> None:
    """Resuelve los comprobantes que quedaron 'inciertos' tras un corte, consultando a ARCA."""

    def run() -> None:
        ctx = _contexto(perfil)
        historial = _historial(ctx)
        pendientes = historial.sin_resolver(ctx.perfil.cuit, ctx.ambiente)
        if not pendientes:
            typer.echo("No hay nada sin resolver.")
            return
        for p in pendientes:
            r = service.reconciliar(historial, ctx.wsfe, p)
            if r.estado == AUTORIZADO:
                typer.secho(
                    f"✓ {_nombre_corto(r.cbte_tipo, r.punto_venta, r.numero)}: ARCA lo tiene "
                    f"autorizado (CAE {r.cae}). Se registró; no se emitió dos veces.",
                    fg=typer.colors.GREEN,
                )
                _pdf_o_aviso(r)
            else:
                typer.echo(
                    f"• Registro {r.id}: ARCA confirma que NO se emitió. Podés emitirlo de nuevo."
                )

    _ejecutar(run)


@app.command()
def recuperar(
    desde: Annotated[
        int | None,
        typer.Argument(help="Primer número a recuperar (sin argumentos: todo lo que falte)."),
    ] = None,
    hasta: Annotated[
        int | None, typer.Argument(help="Último número (por defecto, el mismo).")
    ] = None,
    perfil: PerfilOpt = None,
    pto_vta: Annotated[int | None, typer.Option(help="Punto de venta.")] = None,
    tipo: Annotated[int | None, typer.Option(help="Tipo (11 = Factura C, 13 = NC C).")] = None,
    descripcion: Annotated[
        str, typer.Option(help="Descripción del ítem (ARCA no guarda el detalle).")
    ] = DESCRIPCION_RECUPERADO,
    receptor_nombre: Annotated[
        str | None, typer.Option(help="Nombre del receptor (ARCA no lo guarda).")
    ] = None,
) -> None:
    """Trae del propio ARCA comprobantes ya emitidos y los registra en el historial.

    Sin números, completa automáticamente todo lo que ARCA tiene y este historial no (por ejemplo,
    lo emitido desde otra PC). ARCA solo guarda los totales: los ítems, el nombre y el domicilio
    del receptor no se pueden recuperar (se completan con valores genéricos).
    """

    def run() -> None:
        ctx = _contexto(perfil)
        if desde is None:
            bs = brechas(ctx)
            if not bs:
                typer.echo("El historial está completo: no falta nada de lo que tiene ARCA.")
                return
            _recuperar_y_contar(ctx, bs)
            return
        pv = _pto_vta(ctx, pto_vta)
        t = tipo or ctx.perfil.cbte_tipo_default
        for n in range(desde, (hasta or desde) + 1):
            nombre = _nombre_corto(t, pv, n)
            if _historial(ctx).por_numero(ctx.perfil.cuit, ctx.ambiente, pv, t, n):
                typer.echo(f"• {nombre}: ya estaba en el historial.")
                continue
            reg = recuperar_numero(
                ctx, pv, t, n, descripcion=descripcion, receptor_nombre=receptor_nombre
            )
            if reg is None:
                typer.secho(f"✗ {nombre}: ARCA no lo tiene.", fg=typer.colors.RED)
            else:
                typer.secho(f"✓ {_resumen(reg)}: recuperado.", fg=typer.colors.GREEN)

    _ejecutar(run)


def _recuperar_y_contar(ctx: Contexto, bs: list[Brecha]) -> None:
    recuperados, no_encontrados = recuperar_brechas(ctx, bs)
    for reg in recuperados:
        typer.secho(f"✓ {_resumen(reg)}: recuperado.", fg=typer.colors.GREEN)
    for n in no_encontrados:
        typer.secho(f"✗ Nro. {n}: ARCA no lo tiene (o no está autorizado).", fg=typer.colors.RED)


def _avisar_brecha(ctx: Contexto, yes: bool) -> None:
    """Antes de emitir: avisa si ARCA tiene comprobantes que este historial no (otra PC u otro
    sistema con el mismo punto de venta) y ofrece traerlos. Si ARCA no responde, no se frena acá:
    la emisión misma va a informar el problema."""
    try:
        bs = brechas(ctx)
        n = total_faltantes(bs)
        if not n:
            return
        typer.secho(
            f"ℹ Hay {n} comprobante(s) emitidos fuera de esta PC (ARCA los tiene y este "
            "historial no).",
            fg=typer.colors.CYAN,
        )
        if yes:
            typer.echo("  Completalo con `factumono recuperar`.")
        elif _confirmar("¿Traerlos ahora de ARCA para completar el historial?"):
            _recuperar_y_contar(ctx, bs)
    except (ArcaError, httpx.HTTPError):
        return


@app.command("config")
def configurar(
    carpeta_pdf: Annotated[
        str | None, typer.Option("--carpeta-pdf", help="Dónde se guardan los PDF.")
    ] = None,
    ambiente: Annotated[
        str | None,
        typer.Option("--ambiente", help="Cambia el ambiente activo (igual que `ambiente usar`)."),
    ] = None,
) -> None:
    """Muestra o cambia la configuración general."""

    def run() -> None:
        cfg = config.cargar()
        if ambiente is not None:
            _activar_ambiente(ambiente, None, yes=False)
            cfg = config.cargar()
        if carpeta_pdf is not None:
            cfg.carpeta_pdf = str(Path(carpeta_pdf).expanduser())
            config.guardar(cfg)
            typer.secho("✓ Carpeta de PDF actualizada.", fg=typer.colors.GREEN)
        _banner_prod(cfg.ambiente)
        typer.echo(f"Ambiente activo: {ambientes.obtener(cfg.ambiente).etiqueta}")
        typer.echo(f"Carpeta de PDF: {cfg.carpeta_base_pdf()}")
        typer.echo(
            "  (cada perfil tiene su subcarpeta; lo de homologación va en 'homologacion/' y "
            "lleva marca de agua)"
        )
        for amb in ("homo", "prod"):
            typer.echo(f"Historial {amb}:".ljust(16) + str(config.ruta_historial(amb)))
        typer.echo(f"Configuración:  {config.ruta_config()}")

    _ejecutar(run)


@app.command("app")
def abrir_app(
    navegador: Annotated[
        bool, typer.Option("--navegador", help="La abre en el navegador en vez de una ventana.")
    ] = False,
    probar: Annotated[
        bool, typer.Option("--probar", help="Autochequeo sin ventana (para el smoke test).")
    ] = False,
) -> None:
    """Abre la interfaz gráfica de Factumono (ventana propia o navegador)."""

    def run() -> None:
        from factumono.app.ventana import lanzar
        from factumono.app.ventana import probar as autochequeo

        if probar:
            typer.echo(autochequeo())
            return

        lanzar(navegador=navegador, avisar=typer.echo)

    _ejecutar(run)


@app.command()
def backup(
    destino: Annotated[
        Path | None,
        typer.Argument(
            help="Carpeta o archivo .zip de destino (por defecto, Documentos/Factumono/_backups)."
        ),
    ] = None,
) -> None:
    """Guarda las bases (homo y prod), los clientes y la config en un zip con fecha.

    NO incluye la clave privada ni el .crt: esos archivos no salen nunca de su lugar.
    """

    def run() -> None:
        r = backup_mod.crear_backup(destino)
        typer.secho(f"✓ Backup creado: {r.ruta}", fg=typer.colors.GREEN)
        typer.echo(f"  Incluye: {', '.join(r.archivos) or '(nada todavía)'}")
        typer.echo("  No incluye la clave privada ni el certificado (guardalos aparte).")

    _ejecutar(run)


@app.command()
def restaurar(
    zip_path: Annotated[Path, typer.Argument(help="Backup creado con `factumono backup`.")],
    con_config: Annotated[
        bool,
        typer.Option(
            "--con-config",
            help="También restaura la config (trae las rutas del certificado de la PC de origen).",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="No pregunta.")] = False,
) -> None:
    """Restaura un backup. Antes guarda una copia de lo que hay (en `backups-previos`)."""

    def run() -> None:
        meta = backup_mod.leer_manifiesto(zip_path)
        typer.echo(f"Backup del {meta.get('fecha')} (Factumono {meta.get('factumono')}).")
        typer.echo(f"Contiene: {', '.join(map(str, meta.get('archivos', [])))}")  # type: ignore[call-overload]
        if not yes and not _confirmar(
            "Esto reemplaza el historial y los clientes de esta PC. ¿Seguir?"
        ):
            typer.echo("Cancelado: no se tocó nada.")
            return
        restaurados, previo = backup_mod.restaurar(zip_path, con_config=con_config)
        typer.secho(f"✓ Restaurado: {', '.join(restaurados)}", fg=typer.colors.GREEN)
        if previo:
            typer.echo(f"  Copia de lo anterior: {previo}")
        if not con_config:
            typer.echo("  La config no se tocó (usá --con-config para restaurarla también).")

    _ejecutar(run)


# --- ambiente -----------------------------------------------------------------------------

ambiente_app = typer.Typer(
    help="Ambiente activo: homologación (pruebas) o producción (validez fiscal).",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(ambiente_app, name="ambiente")


def _activar_ambiente(nombre: str, perfil: str | None, *, yes: bool) -> None:
    """Cambia el ambiente activo. Producción solo si `estado` en prod da todo OK."""
    destino = ambientes.obtener(nombre)
    cfg = config.cargar()
    if destino.nombre == cfg.ambiente:
        typer.echo(f"El ambiente activo ya es {destino.titulo}.")
        return
    if destino.es_produccion:
        ctx = construir(perfil, "prod")  # sin banner: todavía no es el ambiente activo
        typer.echo("Chequeando producción antes de activarla...")
        d, problemas = problemas_para_activar_produccion(ctx)
        if d.dummy:
            typer.echo(f"  FEDummy: {'OK' if d.dummy_ok else d.dummy}")
        if d.ticket_vence:
            typer.echo("  Autenticación: OK")
        if d.pvs:
            _mostrar_puntos_de_venta(d)
        if problemas:
            for pr in problemas:
                typer.secho(f"✗ {pr}", fg=typer.colors.RED)
            _fallar("No se activó producción: corregí lo de arriba y volvé a probar.")
        if not yes and not _confirmar(
            "¿Activar PRODUCCIÓN? Desde ahora cada comprobante tiene validez fiscal"
        ):
            typer.echo("Cancelado: el ambiente no cambió.")
            return
    cfg.ambiente = destino.nombre
    config.guardar(cfg)
    _banner_prod(destino.nombre)
    typer.secho(f"✓ Ambiente activo: {destino.etiqueta}.", fg=typer.colors.GREEN)


@ambiente_app.callback()
def ambiente_mostrar(ctx: typer.Context) -> None:
    """Sin subcomando, muestra el ambiente activo."""
    if ctx.invoked_subcommand is None:
        cfg = config.cargar()
        _banner_prod(cfg.ambiente)
        typer.echo(f"Ambiente activo: {ambientes.obtener(cfg.ambiente).etiqueta}")
        typer.echo("Cambiarlo: `factumono ambiente usar prod|homo`.")


@ambiente_app.command("usar")
def ambiente_usar(
    nombre: Annotated[str, typer.Argument(help="'homo' (pruebas) o 'prod' (validez fiscal).")],
    perfil: PerfilOpt = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pregunta (para scripts).")] = False,
) -> None:
    """Cambia el ambiente activo. Producción exige que `estado --ambiente prod` dé todo OK."""
    _ejecutar(lambda: _activar_ambiente(nombre, perfil, yes=yes))


# --- clientes -----------------------------------------------------------------------------

cliente_app = typer.Typer(
    help="Clientes guardados por perfil (con sus defaults para facturar rápido).",
    no_args_is_help=True,
)
app.add_typer(cliente_app, name="cliente")


def _documento(c: Cliente) -> str:
    return cuit_mod.formatear(str(c.doc_nro).zfill(11)) if c.doc_tipo == 80 else f"DNI {c.doc_nro}"


def _validar_condicion(ctx: Contexto, texto: str) -> None:
    try:
        ctx.wsfe.resolver_condicion_iva(texto, "C")
    except httpx.HTTPError:
        typer.secho(
            "⚠ No pude validar la condición de IVA contra ARCA (sin conexión): se guarda igual.",
            fg=typer.colors.YELLOW,
        )


@cliente_app.command("agregar")
def cliente_agregar(
    alias: Annotated[str, typer.Argument(help="Nombre corto (ej.: acciona).")],
    nombre: Annotated[str, typer.Option(help="Nombre o razón social.")],
    condicion_iva: Annotated[
        str, typer.Option(help="Ej.: 'IVA Responsable Inscripto', 'Consumidor Final'.")
    ],
    cuit: Annotated[str | None, typer.Option(help="CUIT.")] = None,
    dni: Annotated[str | None, typer.Option(help="DNI.")] = None,
    domicilio: Annotated[str, typer.Option(help="Domicilio.")] = "",
    email: Annotated[str, typer.Option(help="Email.")] = "",
    descripcion: Annotated[str, typer.Option(help="Descripción habitual del ítem.")] = "",
    unidad: Annotated[str, typer.Option(help="Unidad habitual.")] = "otras unidades",
    concepto: Annotated[
        str, typer.Option(help="1 = productos, 2 = servicios, 3 = productos y servicios.")
    ] = "servicios",
    vto_dias: Annotated[int, typer.Option(help="Días de vencimiento de pago.")] = 4,
    modo_periodo: Annotated[
        str,
        typer.Option(
            "--modo-periodo",
            help="Período de sus facturas: mes (mes completo), dia (día de emisión), rango "
            "(desde-hasta) o sin (sin período, solo productos).",
        ),
    ] = "mes",
    condicion_venta: Annotated[str, typer.Option(help="Condición de venta habitual.")] = "Contado",
    perfil: PerfilOpt = None,
) -> None:
    """Guarda un cliente nuevo."""

    def run() -> None:
        ctx = _contexto(perfil)
        if bool(cuit) == bool(dni):
            raise ValueError("Indicá el CUIT o el DNI (uno solo).")
        modo, concepto_guardado = armado.validar_modo_y_concepto(modo_periodo, concepto)
        _validar_condicion(ctx, condicion_iva)
        c = Cliente(
            alias=alias,
            nombre=nombre,
            doc_tipo=int(DocTipo.CUIT) if cuit else int(DocTipo.DNI),
            doc_nro=int(cuit_mod.normalizar(cuit)) if cuit else int(dni or 0),
            condicion_iva=condicion_iva,
            domicilio=domicilio,
            email=email,
            descripcion=descripcion,
            unidad=unidad,
            concepto=concepto_guardado,
            vto_dias=vto_dias,
            modo_periodo=modo,
            condicion_venta=condicion_venta,
        )
        ctx.clientes.guardar(ctx.perfil_nombre, c)
        typer.secho(f"✓ Cliente '{alias.lower()}' guardado ({nombre}).", fg=typer.colors.GREEN)

    _ejecutar(run)


@cliente_app.command("listar")
def cliente_listar(perfil: PerfilOpt = None) -> None:
    """Lista los clientes guardados."""

    def run() -> None:
        ctx = _contexto(perfil)
        clientes = ctx.clientes.listar(ctx.perfil_nombre)
        if not clientes:
            typer.echo("No hay clientes guardados. Agregá uno con `factumono cliente agregar`.")
            return
        for c in clientes:
            typer.echo(f"{c.alias:<12} {c.nombre}  —  {_documento(c)}  ({c.condicion_iva})")
            habitual = f"{c.descripcion or '(sin descripción)'} · {c.unidad} · {c.concepto}"
            periodo = MODOS_PERIODO.get(c.modo_periodo, c.modo_periodo).lower()
            typer.echo(
                f"{'':<12} habitual: {habitual} · período: {periodo} · vto. {c.vto_dias} días"
            )
            if c.domicilio or c.email:
                typer.echo(f"{'':<12} {c.domicilio}{'  ✉ ' + c.email if c.email else ''}")

    _ejecutar(run)


@cliente_app.command("editar")
def cliente_editar(
    alias: Annotated[str, typer.Argument(help="Alias del cliente.")],
    nombre: Annotated[str | None, typer.Option(help="Nombre o razón social.")] = None,
    condicion_iva: Annotated[str | None, typer.Option(help="Condición frente al IVA.")] = None,
    cuit: Annotated[str | None, typer.Option(help="CUIT.")] = None,
    dni: Annotated[str | None, typer.Option(help="DNI.")] = None,
    domicilio: Annotated[str | None, typer.Option(help="Domicilio.")] = None,
    email: Annotated[str | None, typer.Option(help="Email.")] = None,
    descripcion: Annotated[str | None, typer.Option(help="Descripción habitual.")] = None,
    unidad: Annotated[str | None, typer.Option(help="Unidad habitual.")] = None,
    concepto: Annotated[
        str | None, typer.Option(help="1 = productos, 2 = servicios, 3 = productos y servicios.")
    ] = None,
    vto_dias: Annotated[int | None, typer.Option(help="Días de vencimiento de pago.")] = None,
    modo_periodo: Annotated[
        str | None,
        typer.Option("--modo-periodo", help="mes, dia, rango o sin (ver `cliente agregar`)."),
    ] = None,
    condicion_venta: Annotated[
        str | None, typer.Option(help="Condición de venta habitual.")
    ] = None,
    perfil: PerfilOpt = None,
) -> None:
    """Cambia los datos de un cliente (solo lo que indiques)."""

    def run() -> None:
        ctx = _contexto(perfil)
        actual = ctx.clientes.buscar(ctx.perfil_nombre, alias)
        if actual is None:
            raise ValueError(f"No hay un cliente '{alias}'.")
        cambios: dict[str, Any] = {
            k: v
            for k, v in dict(
                nombre=nombre,
                condicion_iva=condicion_iva,
                domicilio=domicilio,
                email=email,
                descripcion=descripcion,
                unidad=unidad,
                vto_dias=vto_dias,
                condicion_venta=condicion_venta,
            ).items()
            if v is not None
        }
        if cuit and dni:
            raise ValueError("Indicá el CUIT o el DNI (uno solo).")
        if cuit:
            cambios.update(doc_tipo=int(DocTipo.CUIT), doc_nro=int(cuit_mod.normalizar(cuit)))
        if dni:
            cambios.update(doc_tipo=int(DocTipo.DNI), doc_nro=int(dni))
        if concepto or modo_periodo:
            modo, conc = armado.validar_modo_y_concepto(
                modo_periodo or actual.modo_periodo, concepto or actual.concepto
            )
            if concepto:
                cambios["concepto"] = conc
            if modo_periodo:
                cambios["modo_periodo"] = modo
        if not cambios:
            raise ValueError("No indicaste nada para cambiar.")
        if condicion_iva:
            _validar_condicion(ctx, condicion_iva)
        ctx.clientes.guardar(ctx.perfil_nombre, replace(actual, **cambios), reemplazar=True)
        typer.secho(f"✓ Cliente '{actual.alias}' actualizado.", fg=typer.colors.GREEN)

    _ejecutar(run)


@cliente_app.command("borrar")
def cliente_borrar(
    alias: Annotated[str, typer.Argument(help="Alias del cliente.")],
    perfil: PerfilOpt = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pregunta.")] = False,
) -> None:
    """Borra un cliente guardado (no toca los comprobantes ya emitidos)."""

    def run() -> None:
        ctx = _contexto(perfil)
        if not ctx.clientes.buscar(ctx.perfil_nombre, alias):
            raise ValueError(f"No hay un cliente '{alias}'.")
        if not yes and not _confirmar(f"¿Borrar el cliente '{alias}'?"):
            typer.echo("Cancelado.")
            return
        ctx.clientes.borrar(ctx.perfil_nombre, alias)
        typer.secho(f"✓ Cliente '{alias.lower()}' borrado.", fg=typer.colors.GREEN)

    _ejecutar(run)


# --- mcp ----------------------------------------------------------------------------------

mcp_app = typer.Typer(
    help="Servidor MCP (stdio) para conectar Factumono con una IA.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.callback()
def mcp_servidor(ctx: typer.Context) -> None:
    """Sin subcomando, arranca el servidor MCP por stdio (lo lanza el cliente de IA)."""
    if ctx.invoked_subcommand is None:
        from factumono.mcp.server import ejecutar

        ejecutar()


@mcp_app.command("instalar")
def mcp_instalar(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Ruta del claude_desktop_config.json (si no, se detecta)."),
    ] = None,
    solo_mostrar: Annotated[
        bool, typer.Option("--solo-mostrar", help="No modifica nada: solo muestra los snippets.")
    ] = False,
) -> None:
    """Agrega Factumono a Claude Desktop (con backup) y muestra cómo conectar otros clientes."""
    from factumono.mcp import instalar as inst

    def run() -> None:
        entrada = inst.entrada_mcp()
        if not solo_mostrar:
            rutas = [config_path] if config_path else inst.rutas_claude_desktop()
            existentes = [r for r in rutas if r.exists() or r.parent.exists()]
            destinos = existentes or rutas[:1]
            for ruta in destinos:
                r = inst.instalar_en(ruta, entrada)
                if r.ya_estaba:
                    typer.echo(f"• Claude Desktop ya tenía Factumono configurado: {r.ruta}")
                else:
                    typer.secho(f"✓ Claude Desktop configurado: {r.ruta}", fg=typer.colors.GREEN)
                    if r.reemplazo_viejo:
                        typer.echo("  Reemplacé la entrada con el nombre anterior por la nueva.")
                    if r.backup:
                        typer.echo(f"  Backup del archivo anterior: {r.backup}")
            typer.echo(
                "  Reiniciá Claude Desktop (cerralo del todo desde la bandeja) para verlo.\n"
            )
        typer.echo(inst.snippets(entrada))

    _ejecutar(run)


def _avisar_migracion() -> None:
    """Si el programa migró datos del nombre anterior, lo avisa UNA vez (por stderr). Nunca en
    `mcp` (stdout es el canal del protocolo) ni en `app` (la ventana lo muestra)."""
    if sys.argv[1:2] in (["mcp"], ["app"]):
        return
    try:
        datos = config.directorio_datos()
        aviso = migracion.aviso_pendiente(datos)
        if aviso:
            typer.secho("ℹ " + aviso, fg=typer.colors.CYAN, err=True)
            migracion.marcar_avisado(datos)
    except OSError:
        pass


def main() -> None:
    # Que los acentos salgan bien aunque la consola no esté en UTF-8 (Windows, pipes).
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")
    _avisar_migracion()
    app()
