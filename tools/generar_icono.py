"""Dibuja el ícono de Factumono: la cara de un monito simple y sonriente.

Es un dibujo original hecho a mano con Pillow (sin logos ni imágenes de terceros).
    uv run python tools/generar_icono.py
Genera packaging/factumono.ico y src/factumono/app/static/icono.png.
"""

from pathlib import Path

from PIL import Image, ImageDraw

RAIZ = Path(__file__).resolve().parents[1]
N = 1024  # se dibuja grande y se reduce (bordes suaves)
FONDO = (214, 236, 214)  # verde selva suave
PELO = (139, 90, 43)
PELO_OSCURO = (101, 62, 24)
CARA = (244, 205, 160)
ORAJA = (226, 170, 125)
TINTA = (52, 32, 14)
BLANCO = (255, 255, 255)
BOCA = (176, 82, 70)


def circulo(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]) -> None:
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def dibujar() -> Image.Image:
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([20, 20, N - 20, N - 20], radius=210, fill=FONDO)
    cx, cy = N / 2, N * 0.53
    # orejas
    for lado in (-1, 1):
        x = cx + lado * 330
        circulo(d, x, cy - 70, 150, PELO)
        circulo(d, x, cy - 70, 92, ORAJA)
    # cabeza
    d.ellipse([cx - 360, cy - 360, cx + 360, cy + 330], fill=PELO)
    # sombra suave bajo el flequillo
    d.ellipse([cx - 335, cy - 335, cx + 335, cy + 305], outline=PELO_OSCURO, width=10)
    # máscara de la cara: dos lóbulos arriba (ojos) y un óvalo abajo (hocico)
    circulo(d, cx - 118, cy - 40, 150, CARA)
    circulo(d, cx + 118, cy - 40, 150, CARA)
    d.ellipse([cx - 250, cy - 10, cx + 250, cy + 300], fill=CARA)
    # ojos
    for lado in (-1, 1):
        x = cx + lado * 118
        circulo(d, x, cy - 50, 52, TINTA)
        circulo(d, x + 14, cy - 66, 17, BLANCO)
    # nariz y sonrisa
    circulo(d, cx - 34, cy + 100, 17, TINTA)
    circulo(d, cx + 34, cy + 100, 17, TINTA)
    d.arc([cx - 125, cy + 85, cx + 125, cy + 245], start=25, end=155, fill=BOCA, width=26)
    return img


def main() -> None:
    img = dibujar()
    png = RAIZ / "src" / "factumono" / "app" / "static" / "icono.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(png)
    ico = RAIZ / "packaging" / "factumono.ico"
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("OK:", png, ico)


if __name__ == "__main__":
    main()
