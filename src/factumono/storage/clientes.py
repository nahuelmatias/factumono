"""Clientes guardados por perfil (SQLite, en el directorio de datos del usuario)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from factumono import config
from factumono.core.models import Cliente

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    perfil TEXT NOT NULL,
    alias TEXT NOT NULL,
    nombre TEXT NOT NULL,
    doc_tipo INTEGER NOT NULL,
    doc_nro TEXT NOT NULL,
    condicion_iva TEXT NOT NULL,
    domicilio TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    descripcion TEXT NOT NULL DEFAULT '',
    unidad TEXT NOT NULL DEFAULT 'otras unidades',
    concepto TEXT NOT NULL DEFAULT 'servicios',
    vto_dias INTEGER NOT NULL DEFAULT 4,
    PRIMARY KEY (perfil, alias)
);
"""


class ClienteNoEncontrado(ValueError):
    pass


def normalizar_alias(texto: str) -> str:
    return texto.strip().lower()


class Clientes:
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

    @staticmethod
    def _cliente(f: sqlite3.Row) -> Cliente:
        d = {k: f[k] for k in f.keys() if k != "perfil"}  # noqa: SIM118 - sqlite3.Row
        d["doc_nro"] = int(d["doc_nro"])
        return Cliente(**d)

    def guardar(self, perfil: str, c: Cliente, *, reemplazar: bool = False) -> None:
        d = asdict(c)
        d["alias"] = normalizar_alias(c.alias)
        d["doc_nro"] = str(c.doc_nro)
        if not reemplazar and self.buscar(perfil, d["alias"]):
            raise ValueError(f"Ya existe un cliente '{d['alias']}'. Usá `cliente editar`.")
        cols = ["perfil", *d]
        with self._con() as con:
            con.execute(
                f"INSERT OR REPLACE INTO clientes ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [perfil, *d.values()],
            )

    def buscar(self, perfil: str, alias: str) -> Cliente | None:
        with self._con() as con:
            f = con.execute(
                "SELECT * FROM clientes WHERE perfil = ? AND alias = ?",
                (perfil, normalizar_alias(alias)),
            ).fetchone()
        return self._cliente(f) if f else None

    def obtener(self, perfil: str, texto: str) -> Cliente:
        """Por alias exacto, o por parte del alias/nombre si hay una sola coincidencia."""
        c = self.buscar(perfil, texto)
        if c:
            return c
        t = normalizar_alias(texto)
        coincidencias = [
            x for x in self.listar(perfil) if t in x.alias or t in normalizar_alias(x.nombre)
        ]
        if len(coincidencias) == 1:
            return coincidencias[0]
        if coincidencias:
            raise ClienteNoEncontrado(
                f"'{texto}' coincide con varios clientes: "
                + ", ".join(f"{x.alias} ({x.nombre})" for x in coincidencias)
            )
        raise ClienteNoEncontrado(
            f"No hay un cliente guardado '{texto}'. Se agrega con `factumono cliente agregar`."
        )

    def listar(self, perfil: str) -> list[Cliente]:
        with self._con() as con:
            filas = con.execute(
                "SELECT * FROM clientes WHERE perfil = ? ORDER BY alias", (perfil,)
            ).fetchall()
        return [self._cliente(f) for f in filas]

    def borrar(self, perfil: str, alias: str) -> bool:
        with self._con() as con:
            cur = con.execute(
                "DELETE FROM clientes WHERE perfil = ? AND alias = ?",
                (perfil, normalizar_alias(alias)),
            )
        return cur.rowcount > 0


CAMPOS_EDITABLES = [f.name for f in fields(Cliente) if f.name != "alias"]
