# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller (onedir) de la CLI + servidor MCP de Facturitas.

    uv run pyinstaller packaging/facturitas.spec --noconfirm --distpath build/dist --workpath build/work

Qué hay que resolver a mano (ver CLAUDE.md, «Empaquetado»):
- `mcp` importa módulos de forma dinámica (transportes, pydantic): se incluye entero.
- `reportlab` trae fuentes y datos que carga por ruta: se incluyen sus datos.
- `facturitas` se importa por nombre desde el spec (los submódulos no se detectan solos).
- No se usan WSDL (el cliente ARCA arma los XML a mano), así que no hay nada que embeber.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

raiz = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH lo define PyInstaller

datas, binaries, hiddenimports = [], [], collect_submodules("facturitas")
for paquete in ("mcp", "reportlab"):
    d, b, h = collect_all(paquete)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "entry.py")],  # noqa: F821
    pathex=[os.path.join(raiz, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "mypy", "ruff"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="facturitas",
    console=True,  # es una app de consola (CLI + servidor MCP por stdio)
)
coll = COLLECT(exe, a.binaries, a.datas, name="facturitas")  # noqa: F821
