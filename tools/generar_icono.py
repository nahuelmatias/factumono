"""Dibuja el ícono de Facturitas: una medialuna (la factura de panadería) con las puntas dobladas.

Es un dibujo original hecho a mano con Pillow (sin logos ni imágenes de terceros).
    uv run python tools/generar_icono.py
Genera packaging/facturitas.ico y src/facturitas/app/static/icono.png.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

RAIZ = Path(__file__).resolve().parents[1]
N = 1024  # se dibuja grande y se reduce (bordes suaves)
FONDO = (255, 244, 224)
MASA_CLARA = (247, 190, 84)
MASA = (229, 149, 44)
MASA_OSCURA = (176, 98, 24)
BRILLO = (255, 226, 150)


def _punto(s: float, radio: float, cx: float, cy: float, abertura: float) -> tuple[float, float, float]:
    """Punto de la línea media y su ángulo, con s en [-1, 1] sobre un arco abierto hacia abajo."""
    t = math.radians(abertura) * s
    return cx + math.sin(t) * radio, cy - math.cos(t) * radio, t


def _banda(radio: float, cx: float, cy: float, abertura: float, grosor, desfase: float = 0.0):
    """Polígono de una banda curva cuyo grosor depende de s (más gruesa al medio, puntas finas)."""
    izq, der = [], []
    for i in range(121):
        s = -1 + 2 * i / 120
        x, y, t = _punto(s, radio, cx, cy, abertura)
        w = grosor(s)
        nx, ny = math.sin(t), -math.cos(t)  # normal hacia afuera
        izq.append((x + nx * (w + desfase), y + ny * (w + desfase)))
        der.append((x - nx * (w - desfase), y - ny * (w - desfase)))
    return izq + der[::-1]


def dibujar() -> Image.Image:
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([20, 20, N - 20, N - 20], radius=210, fill=FONDO)
    cx, cy, radio, abertura = N / 2, N * 0.60, N * 0.30, 112  # las puntas se doblan hacia adentro

    def grosor(s: float) -> float:
        return 36 + 92 * (1 - abs(s) ** 1.7)

    sombra = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(sombra).polygon(
        [(x, y + 34) for x, y in _banda(radio, cx, cy, abertura, grosor)], fill=(90, 50, 10, 80)
    )
    img.alpha_composite(sombra.filter(ImageFilter.GaussianBlur(20)))
    d = ImageDraw.Draw(img)
    d.polygon(_banda(radio, cx, cy, abertura, lambda s: grosor(s) + 12), fill=MASA_OSCURA)
    d.polygon(_banda(radio, cx, cy, abertura, grosor), fill=MASA)
    d.polygon(_banda(radio, cx, cy, abertura, lambda s: grosor(s) * 0.62, desfase=22), fill=MASA_CLARA)
    d.polygon(_banda(radio, cx, cy, abertura, lambda s: grosor(s) * 0.22, desfase=44), fill=BRILLO)
    # los pliegues de la medialuna: cortes oscuros a lo ancho de la banda
    for s in (-0.66, -0.36, -0.12, 0.12, 0.36, 0.66):
        x, y, t = _punto(s, radio, cx, cy, abertura)
        w = grosor(s)
        nx, ny = math.sin(t), -math.cos(t)
        tx, ty = math.cos(t), math.sin(t)  # tangente
        a = (x + nx * (w + 6) - tx * 14, y + ny * (w + 6) - ty * 14)
        b = (x - nx * (w + 6) + tx * 14, y - ny * (w + 6) + ty * 14)
        d.line([a, b], fill=MASA_OSCURA, width=14)
    return img


def main() -> None:
    img = dibujar()
    png = RAIZ / "src" / "facturitas" / "app" / "static" / "icono.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(png)
    ico = RAIZ / "packaging" / "facturitas.ico"
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("OK:", png, ico)


if __name__ == "__main__":
    main()
