"""La web local de Facturitas (FastAPI + Jinja2 + htmx, sin Node).

Seguridad (CLAUDE.md, sección 5): escucha solo en 127.0.0.1 y ninguna otra página del navegador
puede usarla:
- `Host` tiene que ser el nuestro (contra DNS rebinding);
- hace falta la cookie de sesión (`HttpOnly`, `SameSite=Strict`) que se entrega una sola vez con
  el token del enlace de arranque (`/?t=...`);
- los POST además exigen que `Origin`, si viene, sea el nuestro.
"""

import hmac
import secrets
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from facturitas import __version__, config
from facturitas.app.servicios import (
    CONDICIONES_RECEPTOR,
    CONSUMIDOR_FINAL,
    ErrorUsuario,
    Servicios,
    pesos,
)
from facturitas.arca.errors import ArcaError
from facturitas.arca.errors import traducir_error_red as _red
from facturitas.cli import asistente as asis

AQUI = Path(__file__).parent
COOKIE = "facturitas_sesion"
LEMA = "Si sos monotributista, no te vuelvas mono con las facturas 🐒"


class Seguridad(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str, hosts: set[str]) -> None:
        super().__init__(app)
        self.token = token
        self.hosts = hosts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = (request.headers.get("host") or "").lower()
        if host not in self.hosts:
            return Response("Host no permitido.", status_code=400)
        # Primer ingreso: el enlace de arranque trae el token y entrega la cookie.
        t = request.query_params.get("t")
        if t is not None:
            if not hmac.compare_digest(t, self.token):
                return Response("Enlace inválido.", status_code=403)
            resp = RedirectResponse(request.url.path or "/", status_code=303)
            resp.set_cookie(COOKIE, self.token, httponly=True, samesite="strict")
            return resp
        cookie = request.cookies.get(COOKIE, "")
        if not hmac.compare_digest(cookie, self.token):
            return Response(
                "Abrí Facturitas desde su ventana o con `facturitas app`.", status_code=403
            )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origen = request.headers.get("origin")
            if origen and origen.lower() not in {f"http://{h}" for h in self.hosts}:
                return Response("Origen no permitido.", status_code=403)
        return await call_next(request)


def crear_app(
    servicios: Servicios | None = None, token: str | None = None, puerto: int = 0
) -> FastAPI:
    svc = servicios or Servicios()
    token = token or secrets.token_urlsafe(32)
    app = FastAPI(title="Facturitas", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    hosts = {f"127.0.0.1:{puerto}", f"localhost:{puerto}"} if puerto else {"testserver"}
    app.add_middleware(Seguridad, token=token, hosts=hosts)
    app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
    plantillas = Jinja2Templates(directory=AQUI / "templates")
    plantillas.env.globals["pesos"] = pesos

    def ctx_base(request: Request, **extra: Any) -> dict[str, Any]:
        try:
            banner, prod = svc.banner()
        except Exception:
            banner, prod = "🧪 HOMOLOGACIÓN (pruebas, sin validez fiscal)", False
        return {
            "request": request,
            "banner": banner,
            "es_produccion": prod,
            "version": __version__,
            "lema": LEMA,
            **extra,
        }

    def pagina(request: Request, nombre: str, **extra: Any) -> HTMLResponse:
        return plantillas.TemplateResponse(request, nombre, ctx_base(request, **extra))

    def parcial(request: Request, nombre: str, **extra: Any) -> HTMLResponse:
        return plantillas.TemplateResponse(request, nombre, ctx_base(request, **extra))

    def es_htmx(request: Request) -> bool:
        return request.headers.get("hx-request") == "true"

    def error(request: Request, mensaje: str, status: int = 200) -> HTMLResponse:
        if es_htmx(request):
            return plantillas.TemplateResponse(
                request, "_error.html", ctx_base(request, mensaje=mensaje), status_code=status
            )
        return plantillas.TemplateResponse(
            request, "error.html", ctx_base(request, mensaje=mensaje), status_code=status
        )

    @app.exception_handler(ErrorUsuario)
    async def _eu(request: Request, exc: ErrorUsuario) -> HTMLResponse:
        return error(request, str(exc))

    @app.exception_handler(ArcaError)
    async def _ae(request: Request, exc: ArcaError) -> HTMLResponse:
        return error(request, str(exc))

    @app.exception_handler(config.ConfigError)
    async def _ce(request: Request, exc: config.ConfigError) -> HTMLResponse:
        return error(request, str(exc))

    @app.exception_handler(httpx.HTTPError)
    async def _he(request: Request, exc: httpx.HTTPError) -> HTMLResponse:
        return error(request, _red(exc))

    @app.exception_handler(ValueError)
    async def _ve(request: Request, exc: ValueError) -> HTMLResponse:
        return error(request, str(exc))

    # --- inicio y bienvenida -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def inicio(request: Request) -> Response:
        if not svc.configurado():
            return RedirectResponse("/bienvenida", status_code=303)
        faltan, _ = svc.aviso_brecha()
        return pagina(
            request,
            "inicio.html",
            resumen=svc.resumen(),
            tope=svc.barra_tope(),
            ultimas=svc.listar(limite=5),
            faltan=faltan,
            seccion="inicio",
        )

    @app.get("/bienvenida", response_class=HTMLResponse)
    def bienvenida(request: Request) -> HTMLResponse:
        return pagina(request, "bienvenida.html", configurado=svc.configurado())

    @app.get("/estado", response_class=HTMLResponse)
    def estado(request: Request) -> HTMLResponse:
        return parcial(request, "_estado.html", d=svc.estado_arca(), resumen=svc.resumen())

    @app.post("/recuperar", response_class=HTMLResponse)
    def recuperar(request: Request) -> HTMLResponse:
        return parcial(request, "_ok.html", mensaje=svc.recuperar())

    # --- asistente de primera vez --------------------------------------------------------

    @app.get("/asistente", response_class=HTMLResponse)
    def asistente(request: Request, ambiente: str | None = None) -> HTMLResponse:
        w = svc.wizard(ambiente)
        return pagina(
            request,
            "asistente.html",
            w=w,
            condiciones=list(asis.CONDICIONES.values()),
            categorias=svc.categorias(),
            seccion="asistente",
            url_arca=asis.URL_ARCA_LOGIN,
            url_wsass=asis.URL_WSASS_HOMO,
            total=asis.TOTAL_PASOS,
            capturas=sorted(p.name for p in (AQUI / "static" / "pasos" / w.ambiente).glob("*.png")),
        )

    @app.post("/asistente/datos")
    def asistente_datos(
        ambiente: Annotated[str, Form()],
        cuit: Annotated[str, Form()] = "",
        razon_social: Annotated[str, Form()] = "",
        condicion: Annotated[str, Form()] = "",
        domicilio: Annotated[str, Form()] = "",
        iibb: Annotated[str, Form()] = "",
        nombre_fantasia: Annotated[str, Form()] = "",
        inicio: Annotated[str, Form()] = "",
        alias: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        svc.wizard_datos(
            ambiente=ambiente, cuit=cuit, razon_social=razon_social, condicion=condicion,
            domicilio=domicilio, iibb=iibb, nombre_fantasia=nombre_fantasia, inicio=inicio,
            alias=alias,
        )  # fmt: skip
        return RedirectResponse(f"/asistente?ambiente={ambiente}", status_code=303)

    @app.post("/asistente/tramites")
    def asistente_tramites(ambiente: Annotated[str, Form()]) -> RedirectResponse:
        svc.wizard_tramites_hechos(ambiente)
        return RedirectResponse(f"/asistente?ambiente={ambiente}", status_code=303)

    @app.post("/asistente/certificado")
    async def asistente_certificado(
        ambiente: Annotated[str, Form()],
        archivo: Annotated[UploadFile | None, File()] = None,
        texto: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        contenido = (await archivo.read()) if archivo is not None and archivo.filename else b""
        if not contenido and texto.strip():
            contenido = texto.strip().encode("utf-8")
        if not contenido:
            raise ErrorUsuario("Subí el archivo .crt o pegá su contenido.")
        svc.wizard_certificado(ambiente, contenido)
        return RedirectResponse(f"/asistente?ambiente={ambiente}", status_code=303)

    @app.post("/asistente/guardar")
    def asistente_guardar(
        ambiente: Annotated[str, Form()],
        punto_venta: Annotated[int, Form()] = 1,
        categoria: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        svc.wizard_guardar(ambiente, punto_venta, categoria)
        return RedirectResponse(f"/asistente?ambiente={ambiente}", status_code=303)

    @app.post("/asistente/verificar", response_class=HTMLResponse)
    def asistente_verificar(request: Request, ambiente: Annotated[str, Form()]) -> HTMLResponse:
        d = svc.wizard_verificar(ambiente)
        listo = d.ok or (ambiente == "homo" and d.dummy_ok and d.ticket_vence is not None)
        if listo:
            svc.wizard_terminar(ambiente)
        return parcial(request, "_verificacion.html", d=d, listo=listo, ambiente=ambiente)

    @app.get("/asistente/csr", response_class=Response)
    def descargar_csr(ambiente: str = "homo") -> Response:
        w = svc.wizard(ambiente)
        nombre_csr = w.avance.alias or "facturitas"
        return Response(
            w.csr_texto,
            media_type="application/x-pem-file",
            headers={"Content-Disposition": f'attachment; filename="{nombre_csr}.csr"'},
        )

    # --- nueva factura ---------------------------------------------------------------------

    @app.get("/nueva", response_class=HTMLResponse)
    def nueva(request: Request, cliente: str = "", repetir: int = 0) -> HTMLResponse:
        return pagina(
            request,
            "nueva.html",
            clientes=svc.clientes(),
            seleccionado=cliente,
            repetir=bool(repetir),
            periodo=svc.periodo_sugerido(),
            consumidor_final=CONSUMIDOR_FINAL,
            seccion="nueva",
            resumen=svc.resumen(),
        )

    @app.post("/nueva/preparar", response_class=HTMLResponse)
    def nueva_preparar(
        request: Request,
        cliente: Annotated[str, Form()],
        importe: Annotated[str, Form()] = "",
        descripcion: Annotated[str, Form()] = "",
        periodo: Annotated[str, Form()] = "",
        vto_pago: Annotated[str, Form()] = "",
        repetir: Annotated[str, Form()] = "",
        concepto: Annotated[str, Form()] = "servicios",
    ) -> HTMLResponse:
        v = svc.preparar_factura(
            cliente=cliente, importe=importe, descripcion=descripcion, periodo=periodo,
            vto_pago=vto_pago, repetir=bool(repetir), concepto=concepto,
        )  # fmt: skip
        return parcial(request, "_preview.html", v=v)

    @app.post("/nueva/emitir", response_class=HTMLResponse)
    def nueva_emitir(
        request: Request,
        borrador_id: Annotated[str, Form()],
        confirmo_anulada: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        e = svc.emitir(borrador_id, bool(confirmo_anulada))
        return parcial(request, "_emitido.html", e=e, mailto=svc.mailto(e.registro.id))

    # --- mis facturas ------------------------------------------------------------------------

    @app.get("/facturas", response_class=HTMLResponse)
    def facturas(request: Request, q: str = "") -> HTMLResponse:
        filas = svc.listar(q or None, limite=100)
        if es_htmx(request):
            return parcial(request, "_tabla_facturas.html", filas=filas)
        return pagina(
            request, "facturas.html", filas=filas, q=q, seccion="facturas", resumen=svc.resumen()
        )

    @app.get("/facturas/{id_}/pdf")
    def factura_pdf(id_: int) -> FileResponse:
        ruta = svc.pdf(id_)
        return FileResponse(
            ruta,
            media_type="application/pdf",
            filename=ruta.name,
            content_disposition_type="inline",
        )

    @app.post("/facturas/{id_}/carpeta", response_class=HTMLResponse)
    def factura_carpeta(request: Request, id_: int) -> HTMLResponse:
        svc.abrir_en_el_explorador(id_)
        return parcial(request, "_ok.html", mensaje="Abrí la carpeta con el PDF.")

    @app.get("/facturas/{id_}/anular", response_class=HTMLResponse)
    def anular(request: Request, id_: int) -> HTMLResponse:
        return pagina(
            request, "anular.html", r=svc.registro(id_), seccion="facturas", resumen=svc.resumen()
        )

    @app.post("/facturas/{id_}/anular/preparar", response_class=HTMLResponse)
    def anular_preparar(
        request: Request, id_: int, motivo: Annotated[str, Form()] = ""
    ) -> HTMLResponse:
        return parcial(request, "_preview.html", v=svc.preparar_nota_de_credito(id_, motivo))

    # --- clientes ----------------------------------------------------------------------------

    @app.get("/clientes", response_class=HTMLResponse)
    def clientes(request: Request) -> HTMLResponse:
        return pagina(
            request,
            "clientes.html",
            clientes=svc.clientes(),
            seccion="clientes",
            resumen=svc.resumen(),
        )

    @app.get("/clientes/nuevo", response_class=HTMLResponse)
    def cliente_nuevo(request: Request) -> HTMLResponse:
        return pagina(
            request, "cliente_form.html", c=None, condiciones=CONDICIONES_RECEPTOR,
            seccion="clientes", resumen=svc.resumen(),
        )  # fmt: skip

    @app.get("/clientes/{alias}/editar", response_class=HTMLResponse)
    def cliente_editar(request: Request, alias: str) -> HTMLResponse:
        c = svc.cliente(alias)
        if c is None:
            raise ErrorUsuario("No encuentro ese cliente.")
        return pagina(
            request, "cliente_form.html", c=c, condiciones=CONDICIONES_RECEPTOR,
            seccion="clientes", resumen=svc.resumen(),
        )  # fmt: skip

    @app.post("/clientes/guardar")
    def cliente_guardar(
        alias: Annotated[str, Form()] = "",
        nombre: Annotated[str, Form()] = "",
        documento: Annotated[str, Form()] = "",
        condicion_iva: Annotated[str, Form()] = "",
        domicilio: Annotated[str, Form()] = "",
        email: Annotated[str, Form()] = "",
        descripcion: Annotated[str, Form()] = "",
        unidad: Annotated[str, Form()] = "otras unidades",
        concepto: Annotated[str, Form()] = "servicios",
        vto_dias: Annotated[int, Form()] = 4,
        editando: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        svc.guardar_cliente(
            alias=alias, nombre=nombre, documento=documento, condicion_iva=condicion_iva,
            domicilio=domicilio, email=email, descripcion=descripcion, unidad=unidad,
            concepto=concepto, vto_dias=vto_dias, reemplazar=bool(editando),
        )  # fmt: skip
        return RedirectResponse("/clientes", status_code=303)

    @app.post("/clientes/{alias}/borrar")
    def cliente_borrar(alias: str) -> RedirectResponse:
        svc.borrar_cliente(alias)
        return RedirectResponse("/clientes", status_code=303)

    # --- ajustes -----------------------------------------------------------------------------

    @app.get("/ajustes", response_class=HTMLResponse)
    def ajustes(request: Request) -> HTMLResponse:
        return pagina(
            request, "ajustes.html", a=svc.ajustes(), seccion="ajustes", resumen=svc.resumen()
        )

    @app.post("/ajustes", response_class=HTMLResponse)
    def ajustes_guardar(
        request: Request,
        carpeta_pdf: Annotated[str, Form()] = "",
        categoria: Annotated[str, Form()] = "",
        aviso_duplicados: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        r = svc.guardar_ajustes(
            carpeta_pdf=carpeta_pdf, categoria=categoria, aviso_duplicados=bool(aviso_duplicados)
        )
        return parcial(request, "_ok.html", mensaje=r.mensaje)

    @app.post("/ajustes/ambiente", response_class=HTMLResponse)
    def ajustes_ambiente(request: Request, destino: Annotated[str, Form()]) -> Response:
        r, _ = svc.cambiar_ambiente(destino)
        if es_htmx(request):
            return Response(status_code=204, headers={"HX-Redirect": "/ajustes"})
        return RedirectResponse("/ajustes", status_code=303)

    @app.post("/ajustes/backup", response_class=HTMLResponse)
    def ajustes_backup(request: Request) -> HTMLResponse:
        r = svc.hacer_backup()
        return parcial(
            request, "_ok.html",
            mensaje=f"Backup listo: {r.ruta}. No incluye tu clave privada ni el certificado.",
        )  # fmt: skip

    # --- conectar con mi IA ----------------------------------------------------------------

    @app.get("/ia", response_class=HTMLResponse)
    def ia(request: Request) -> HTMLResponse:
        return pagina(
            request, "ia.html", snippets=svc.snippets(), seccion="ia", resumen=svc.resumen()
        )

    @app.post("/ia/conectar", response_class=HTMLResponse)
    def ia_conectar(request: Request) -> HTMLResponse:
        mensajes, _ = svc.conectar_claude_desktop()
        return parcial(request, "_ok.html", mensaje=" ".join(mensajes))

    @app.get("/acerca", response_class=HTMLResponse)
    def acerca(request: Request) -> HTMLResponse:
        return pagina(request, "acerca.html", seccion="acerca")

    return app
