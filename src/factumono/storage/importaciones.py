"""Importaciones pendientes de confirmar (GUI y MCP): lo leído de las facturas viejas, vigente 15
minutos. La CLI no lo usa: pregunta y guarda en el momento."""

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from factumono import config

VIGENCIA = timedelta(minutes=15)

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS importaciones (
    id TEXT PRIMARY KEY,
    perfil TEXT NOT NULL,
    ambiente TEXT NOT NULL,
    datos TEXT NOT NULL,
    creado TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Pendiente:
    id: str
    perfil: str
    ambiente: str
    datos: dict[str, Any]
    creado: datetime

    @property
    def vence(self) -> datetime:
        return self.creado + VIGENCIA

    def vencida(self, ahora: datetime) -> bool:
        return ahora >= self.vence


class Importaciones:
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
        self, perfil: str, ambiente: str, datos: dict[str, Any], ahora: datetime | None = None
    ) -> Pendiente:
        ahora = ahora or datetime.now()
        id_ = "I-" + secrets.token_hex(3).upper()
        with self._con() as con:
            con.execute(
                "INSERT INTO importaciones (id, perfil, ambiente, datos, creado) "
                "VALUES (?,?,?,?,?)",
                (id_, perfil, ambiente, json.dumps(datos, ensure_ascii=False), ahora.isoformat()),
            )
        return Pendiente(id_, perfil, ambiente, datos, ahora)

    def obtener(self, id_: str) -> Pendiente | None:
        with self._con() as con:
            f = con.execute("SELECT * FROM importaciones WHERE id = ?", (id_,)).fetchone()
        if not f:
            return None
        return Pendiente(
            f["id"],
            f["perfil"],
            f["ambiente"],
            json.loads(f["datos"]),
            datetime.fromisoformat(f["creado"]),
        )

    def borrar(self, id_: str) -> None:
        with self._con() as con:
            con.execute("DELETE FROM importaciones WHERE id = ?", (id_,))
