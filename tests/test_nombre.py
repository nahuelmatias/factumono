"""El nombre anterior del proyecto no aparece en el código, los textos ni los docs.

Chocaba con un producto comercial existente. Solo `migracion.py` (que mueve los datos viejos) y
sus tests pueden conocerlo; está armado por partes para que este mismo archivo tampoco lo contenga.
"""

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
VIEJO = "factu" + "ritas"
PERMITIDOS = {
    RAIZ / "src" / "factumono" / "migracion.py",
    RAIZ / "tests" / "test_migracion.py",
    RAIZ / "tests" / "test_nombre.py",
}
EXTENSIONES = {
    ".py",
    ".md",
    ".toml",
    ".yml",
    ".yaml",
    ".ps1",
    ".iss",
    ".spec",
    ".html",
    ".css",
    ".json",
    ".txt",
}
IGNORAR = {
    ".git",
    ".venv",
    ".venv-build",
    "build",
    "referencia_privada",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def _archivos() -> list[Path]:
    salida = []
    for p in RAIZ.rglob("*"):
        if any(parte in IGNORAR for parte in p.relative_to(RAIZ).parts):
            continue
        if (
            p.is_file()
            and (p.suffix in EXTENSIONES or p.name == ".gitignore")
            and p.name != "uv.lock"
        ):
            salida.append(p)
    return salida


def test_el_nombre_anterior_no_aparece_en_ningun_archivo_del_proyecto() -> None:
    encontrados = [
        str(p.relative_to(RAIZ))
        for p in _archivos()
        if p not in PERMITIDOS and VIEJO in p.read_text(encoding="utf-8", errors="ignore").lower()
    ]
    assert encontrados == [], f"Aparece el nombre anterior en: {encontrados}"


def test_ningun_archivo_ni_carpeta_lo_lleva_en_el_nombre() -> None:
    con_nombre = [
        str(p.relative_to(RAIZ))
        for p in RAIZ.rglob("*")
        if not any(parte in IGNORAR for parte in p.relative_to(RAIZ).parts)
        and VIEJO in p.name.lower()
    ]
    assert con_nombre == []
