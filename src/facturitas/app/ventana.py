"""Arranque de la GUI: el servidor local y una ventana nativa (pywebview) o el navegador.

Si pywebview no puede abrir la ventana (por ejemplo, falta WebView2), se cae al navegador.
"""

import contextlib
import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn

from facturitas.app.server import crear_app
from facturitas.app.servicios import Servicios

TITULO = "Facturitas"


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServidorLocal:
    """El servidor web en un hilo, escuchando solo en 127.0.0.1."""

    def __init__(self, servicios: Servicios | None = None) -> None:
        import secrets

        self.token = secrets.token_urlsafe(32)
        self.puerto = puerto_libre()
        self.app = crear_app(servicios, self.token, self.puerto)
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.puerto, log_config=None, access_log=False
        )
        self.servidor = uvicorn.Server(config)
        self.hilo = threading.Thread(target=self.servidor.run, daemon=True)

    @property
    def url(self) -> str:
        """Enlace de arranque: trae el token que entrega la cookie de sesión."""
        return f"http://127.0.0.1:{self.puerto}/?t={self.token}"

    def iniciar(self, espera: float = 15.0) -> None:
        self.hilo.start()
        limite = time.monotonic() + espera
        while not self.servidor.started:
            if time.monotonic() > limite or not self.hilo.is_alive():
                raise RuntimeError("No se pudo iniciar el servidor local de Facturitas.")
            time.sleep(0.05)

    def detener(self) -> None:
        self.servidor.should_exit = True
        with contextlib.suppress(RuntimeError):
            self.hilo.join(timeout=5)


def ruta_icono() -> Path:
    return Path(__file__).parent / "static" / "icono.png"


def probar() -> str:
    """Autochequeo sin ventana (lo usa el smoke test del instalador): levanta el servidor, pide la
    página de inicio, el ícono y htmx, y confirma que las plantillas y los estáticos están."""
    import httpx

    srv = ServidorLocal()
    srv.iniciar()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.puerto}", follow_redirects=True) as c:
            r = c.get(srv.url)
            if r.status_code != 200 or "Facturitas" not in r.text:
                raise RuntimeError(f"La página de inicio falló ({r.status_code}).")
            estaticos = (("/static/icono.png", b"\x89PNG"), ("/static/htmx.min.js", b"var htmx"))
            for ruta, inicio in estaticos:
                x = c.get(ruta)
                if x.status_code != 200 or not x.content.startswith(inicio):
                    raise RuntimeError(f"Falta el archivo estático {ruta}.")
            if httpx.get(f"http://127.0.0.1:{srv.puerto}/").status_code != 403:
                raise RuntimeError("El servidor atiende pedidos sin la cookie de sesión.")
    finally:
        srv.detener()
    return "OK: la interfaz gráfica responde (plantillas, estáticos y seguridad)."


def lanzar(
    navegador: bool = False,
    servicios: Servicios | None = None,
    avisar: Callable[[str], None] = print,
) -> None:
    srv = ServidorLocal(servicios)
    srv.iniciar()
    try:
        if not navegador and _ventana(srv.url, avisar):
            return
        webbrowser.open(srv.url)
        avisar(f"Facturitas está abierto en tu navegador (http://127.0.0.1:{srv.puerto}).")
        avisar("Para cerrarlo, presioná Ctrl+C en esta ventana.")
        with contextlib.suppress(KeyboardInterrupt):
            while srv.hilo.is_alive():
                time.sleep(0.5)
    finally:
        srv.detener()


def _ventana(url: str, avisar: Callable[[str], None]) -> bool:
    """Abre la ventana nativa y espera a que se cierre. False si no se pudo (usar el navegador)."""
    try:
        import webview

        webview.create_window(TITULO, url, width=1120, height=820, min_size=(820, 600))
        webview.start()
        return True
    except Exception as e:
        avisar(f"No pude abrir la ventana propia ({e}); lo abro en el navegador.")
        return False
