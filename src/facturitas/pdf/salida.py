"""Dónde se guardan los PDF y cómo se llaman (igual que los de ARCA).

`<carpeta>/<perfil>/<CUIT>_<tipo 3 díg.>_<PV 5 díg.>_<NRO 8 díg.>.pdf`. Los de homologación van
a una subcarpeta aparte: su numeración arranca de cero y chocaría con la de los comprobantes reales.
"""

from pathlib import Path

from facturitas.config import Config
from facturitas.pdf.render import renderizar
from facturitas.storage.historial import Registro


def nombre_archivo(cuit: str, cbte_tipo: int, punto_venta: int, numero: int) -> str:
    return f"{cuit}_{cbte_tipo:03d}_{punto_venta:05d}_{numero:08d}.pdf"


def ruta_pdf(cfg: Config, reg: Registro, carpeta: Path | None = None) -> Path:
    assert reg.numero is not None
    base = carpeta or cfg.carpeta_base_pdf()
    destino = base / reg.perfil
    if reg.es_homologacion:
        destino = destino / "homologacion"
    return destino / nombre_archivo(reg.cuit_emisor, reg.cbte_tipo, reg.punto_venta, reg.numero)


def guardar_pdf(cfg: Config, reg: Registro, carpeta: Path | None = None) -> Path:
    """Genera el PDF de un comprobante autorizado y lo guarda. Devuelve la ruta."""
    if reg.numero is None or reg.cae == "" or reg.cae_vto is None:
        raise ValueError("Solo se puede generar el PDF de un comprobante autorizado (con CAE).")
    ruta = ruta_pdf(cfg, reg, carpeta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(
        renderizar(
            reg.comprobante, reg.numero, reg.cae, reg.cae_vto, homologacion=reg.es_homologacion
        )
    )
    return ruta
