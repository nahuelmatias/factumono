"""Borradores: lo que se preparó y todavía no se emitió. Vencen a los 15 minutos.

Nada se emite sin un borrador previo: `emitir_comprobante` del MCP trabaja siempre sobre un
borrador existente, vigente y confirmado explícitamente.
"""

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from factumono import config
from factumono.core import serial
from factumono.core.models import Comprobante

VIGENCIA = timedelta(minutes=15)

ABIERTO = "abierto"
EMITIDO = "emitido"

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS borradores (
    id TEXT PRIMARY KEY,
    perfil TEXT NOT NULL,
    ambiente TEXT NOT NULL,
    datos TEXT NOT NULL,
    avisos TEXT NOT NULL DEFAULT '[]',
    requiere_extra INTEGER NOT NULL DEFAULT 0,
    estado TEXT NOT NULL DEFAULT 'abierto',
    creado TEXT NOT NULL,
    vence TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Borrador:
    id: str
    perfil: str
    ambiente: str
    comprobante: Comprobante
    avisos: list[str] = field(default_factory=list)
    requiere_extra: bool = False  # posible duplicado / factura ya anulada
    estado: str = ABIERTO
    creado: datetime = field(default_factory=datetime.now)
    vence: datetime = field(default_factory=datetime.now)

    def vencido(self, ahora: datetime | None = None) -> bool:
        return (ahora or datetime.now()) > self.vence


class Borradores:
    def __init__(self, ruta: Path | None = None) -> None:
        self.ruta = ruta or config.directorio_datos() / "historial.sqlite"
        with self._con() as con:
            con.executescript(_ESQUEMA)

    @contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.ruta, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def crear(
        self,
        *,
        perfil: str,
        ambiente: str,
        c: Comprobante,
        avisos: list[str],
        requiere_extra: bool,
        ahora: datetime | None = None,
    ) -> Borrador:
        ahora = ahora or datetime.now()
        id_ = "B-" + secrets.token_hex(3).upper()
        with self._con() as con:
            con.execute(
                "INSERT INTO borradores (id, perfil, ambiente, datos, avisos, requiere_extra,"
                " estado, creado, vence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    id_,
                    perfil,
                    ambiente,
                    json.dumps(serial.a_dict(c), ensure_ascii=False),
                    json.dumps(avisos, ensure_ascii=False),
                    int(requiere_extra),
                    ABIERTO,
                    ahora.isoformat(timespec="seconds"),
                    (ahora + VIGENCIA).isoformat(timespec="seconds"),
                ),
            )
        return Borrador(id_, perfil, ambiente, c, avisos, requiere_extra, ABIERTO, ahora,
                        ahora + VIGENCIA)  # fmt: skip

    def obtener(self, id_: str) -> Borrador | None:
        with self._con() as con:
            f = con.execute(
                "SELECT * FROM borradores WHERE id = ?", (id_.strip().upper(),)
            ).fetchone()
        if not f:
            return None
        return Borrador(
            id=f["id"],
            perfil=f["perfil"],
            ambiente=f["ambiente"],
            comprobante=serial.de_dict(json.loads(f["datos"])),
            avisos=json.loads(f["avisos"]),
            requiere_extra=bool(f["requiere_extra"]),
            estado=f["estado"],
            creado=datetime.fromisoformat(f["creado"]),
            vence=datetime.fromisoformat(f["vence"]),
        )

    def marcar_emitido(self, id_: str) -> None:
        with self._con() as con:
            con.execute("UPDATE borradores SET estado = ? WHERE id = ?", (EMITIDO, id_))
