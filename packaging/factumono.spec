# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller (onedir): `factumono.exe` (CLI + MCP, con consola) y `factumono-gui.exe` (la
interfaz gráfica, sin consola) en una sola carpeta.

    uv run pyinstaller packaging/factumono.spec --noconfirm --distpath build/dist --workpath build/work

Qué hay que resolver a mano (ver CLAUDE.md, «Empaquetado»):
- `mcp` importa módulos de forma dinámica (transportes, pydantic): se incluye entero.
- `reportlab` trae fuentes y datos que carga por ruta: se incluyen sus datos.
- `factumono` se importa por nombre desde el spec (los submódulos no se detectan solos) y se
  incluyen sus datos (plantillas, estáticos, topes del monotributo, ícono).
- `uvicorn` elige protocolos y loops por nombre: submódulos enteros. `webview` (pywebview) trae
  pythonnet y su DLL de WebView2: `collect_all`.
- No se usan WSDL (el cliente ARCA arma los XML a mano), así que no hay nada que embeber.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

raiz = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH lo define PyInstaller
icono = os.path.join(SPECPATH, "factumono.ico")  # noqa: F821

datas = collect_data_files("factumono")
binaries, hiddenimports = [], collect_submodules("factumono") + collect_submodules("uvicorn")
for paquete in ("mcp", "reportlab", "webview", "pythonnet", "clr_loader", "multipart"):
    try:
        d, b, h = collect_all(paquete)
    except Exception:  # un paquete opcional que no esté instalado no frena el build
        continue
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["clr", "python_multipart"]

excludes = ["tkinter", "pytest", "mypy", "ruff"]
cli = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "entry.py")],  # noqa: F821
    pathex=[os.path.join(raiz, "src")], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, excludes=excludes,
)
gui = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "entry_gui.py")],  # noqa: F821
    pathex=[os.path.join(raiz, "src")], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, excludes=excludes,
)
MERGE((cli, "factumono", "factumono"), (gui, "factumono-gui", "factumono-gui"))  # noqa: F821

pyz_cli = PYZ(cli.pure)  # noqa: F821
pyz_gui = PYZ(gui.pure)  # noqa: F821
exe_cli = EXE(  # noqa: F821
    pyz_cli, cli.scripts, [], exclude_binaries=True, name="factumono", console=True, icon=icono,
)
exe_gui = EXE(  # noqa: F821
    pyz_gui, gui.scripts, [], exclude_binaries=True, name="factumono-gui", console=False, icon=icono,
)
coll = COLLECT(  # noqa: F821
    exe_cli, cli.binaries, cli.datas, exe_gui, gui.binaries, gui.datas, name="factumono",
)
