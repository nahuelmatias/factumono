"""Historial de comprobantes en SQLite (todo lo emitido, incluida la homologación).

Una fila nace en estado `pendiente` *antes* de pedir el CAE, con el número que se va a usar: si
se corta la red, ese registro permite reconciliar con ARCA en vez de emitir dos veces.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from facturitas import config
from facturitas.core import serial
from facturitas.core.models import Comprobante

PENDIENTE = "pendiente"  # número reservado, todavía no se pidió (o no se sabe) el CAE
INCIERTO = "incierto"  # se pidió el CAE y se cortó la comunicación: hay que reconciliar
AUTORIZADO = "autorizado"
RECHAZADO = "rechazado"  # ARCA contestó que no
FALLIDO = "fallido"  # reconciliado: ARCA confirma que NO se emitió

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS comprobantes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    perfil TEXT NOT NULL,
    ambiente TEXT NOT NULL,
    cuit_emisor TEXT NOT NULL,
    punto_venta INTEGER NOT NULL,
    cbte_tipo INTEGER NOT NULL,
    numero INTEGER,
    fecha TEXT NOT NULL,
    importe TEXT NOT NULL,
    receptor_doc_tipo INTEGER NOT NULL,
    receptor_doc_nro TEXT NOT NULL,
    receptor_nombre TEXT NOT NULL,
    periodo_desde TEXT,
    periodo_hasta TEXT,
    estado TEXT NOT NULL,
    cae TEXT NOT NULL DEFAULT '',
    cae_vto TEXT,
    clave TEXT NOT NULL UNIQUE,
    asociado_id INTEGER REFERENCES comprobantes(id),
    motivo TEXT NOT NULL DEFAULT '',
    datos TEXT NOT NULL,
    observaciones TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    recuperado INTEGER NOT NULL DEFAULT 0,
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_autorizado
    ON comprobantes (ambiente, cuit_emisor, punto_venta, cbte_tipo, numero)
    WHERE estado = 'autorizado';
CREATE INDEX IF NOT EXISTS ix_receptor ON comprobantes (cuit_emisor, receptor_doc_nro);
"""


@dataclass(frozen=True)
class Registro:
    id: int
    perfil: str
    ambiente: str
    cuit_emisor: str
    punto_venta: int
    cbte_tipo: int
    numero: int | None
    fecha: date
    importe: Decimal
    receptor_doc_tipo: int
    receptor_doc_nro: int
    receptor_nombre: str
    periodo_desde: date | None
    periodo_hasta: date | None
    estado: str
    cae: str
    cae_vto: date | None
    clave: str
    asociado_id: int | None
    motivo: str
    comprobante: Comprobante
    observaciones: list[str] = field(default_factory=list)
    error: str = ""
    recuperado: bool = False
    creado: datetime = field(default_factory=datetime.now)

    @property
    def es_homologacion(self) -> bool:
        return self.ambiente != "prod"


def _f(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _registro(fila: sqlite3.Row) -> Registro:
    return Registro(
        id=fila["id"],
        perfil=fila["perfil"],
        ambiente=fila["ambiente"],
        cuit_emisor=fila["cuit_emisor"],
        punto_venta=fila["punto_venta"],
        cbte_tipo=fila["cbte_tipo"],
        numero=fila["numero"],
        fecha=date.fromisoformat(fila["fecha"]),
        importe=Decimal(fila["importe"]),
        receptor_doc_tipo=fila["receptor_doc_tipo"],
        receptor_doc_nro=int(fila["receptor_doc_nro"]),
        receptor_nombre=fila["receptor_nombre"],
        periodo_desde=_f(fila["periodo_desde"]),
        periodo_hasta=_f(fila["periodo_hasta"]),
        estado=fila["estado"],
        cae=fila["cae"],
        cae_vto=_f(fila["cae_vto"]),
        clave=fila["clave"],
        asociado_id=fila["asociado_id"],
        motivo=fila["motivo"],
        comprobante=serial.de_dict(json.loads(fila["datos"])),
        observaciones=json.loads(fila["observaciones"]),
        error=fila["error"],
        recuperado=bool(fila["recuperado"]),
        creado=datetime.fromisoformat(fila["creado"]),
    )


def _columnas(c: Comprobante) -> dict[str, Any]:
    r = c.receptor
    return {
        "punto_venta": c.punto_venta,
        "cbte_tipo": c.cbte_tipo,
        "fecha": c.fecha.isoformat(),
        "importe": str(c.total),
        "receptor_doc_tipo": r.doc_tipo,
        "receptor_doc_nro": str(r.doc_nro),
        "receptor_nombre": r.nombre,
        "periodo_desde": c.periodo_desde.isoformat() if c.periodo_desde else None,
        "periodo_hasta": c.periodo_hasta.isoformat() if c.periodo_hasta else None,
        "motivo": c.motivo,
        "datos": json.dumps(serial.a_dict(c), ensure_ascii=False),
    }


class Historial:
    def __init__(self, ruta: Path | None = None) -> None:
        self.ruta = ruta or config.directorio_datos() / "historial.sqlite"
        nueva = not self.ruta.exists()
        with self._con() as con:
            con.executescript(_ESQUEMA)
        if nueva:
            config.restringir_permisos(self.ruta)

    @contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.ruta, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            with con:  # commit / rollback
                yield con
        finally:
            con.close()

    # --- escritura --------------------------------------------------------------------------

    def crear(
        self,
        *,
        perfil: str,
        ambiente: str,
        c: Comprobante,
        clave: str,
        asociado_id: int | None = None,
        estado: str = PENDIENTE,
        numero: int | None = None,
        recuperado: bool = False,
    ) -> Registro:
        ahora = datetime.now().isoformat(timespec="seconds")
        cols: dict[str, Any] = {
            "perfil": perfil,
            "ambiente": ambiente,
            "cuit_emisor": c.perfil.cuit,
            "numero": numero,
            "estado": estado,
            "clave": clave,
            "asociado_id": asociado_id,
            "recuperado": int(recuperado),
            "creado": ahora,
            "actualizado": ahora,
            **_columnas(c),
        }
        nombres = ", ".join(cols)
        marcas = ", ".join("?" for _ in cols)
        with self._con() as con:
            cur = con.execute(
                f"INSERT INTO comprobantes ({nombres}) VALUES ({marcas})", list(cols.values())
            )
            fila_id = cur.lastrowid
        assert fila_id is not None
        return self._existente(fila_id)

    def actualizar(self, id_: int, **campos: Any) -> Registro:
        """Actualiza columnas. `comprobante=` reescribe los datos completos del comprobante."""
        if "comprobante" in campos:
            campos.update(_columnas(campos.pop("comprobante")))
        if "observaciones" in campos:
            campos["observaciones"] = json.dumps(campos["observaciones"], ensure_ascii=False)
        if isinstance(campos.get("cae_vto"), date):
            campos["cae_vto"] = campos["cae_vto"].isoformat()
        campos["actualizado"] = datetime.now().isoformat(timespec="seconds")
        asignaciones = ", ".join(f"{k} = ?" for k in campos)
        with self._con() as con:
            con.execute(
                f"UPDATE comprobantes SET {asignaciones} WHERE id = ?", [*campos.values(), id_]
            )
        return self._existente(id_)

    # --- lectura ----------------------------------------------------------------------------

    def _uno(self, sql: str, args: tuple[Any, ...]) -> Registro | None:
        with self._con() as con:
            fila = con.execute(sql, args).fetchone()
        return _registro(fila) if fila else None

    def _varios(self, sql: str, args: tuple[Any, ...]) -> list[Registro]:
        with self._con() as con:
            return [_registro(f) for f in con.execute(sql, args).fetchall()]

    def _existente(self, id_: int) -> Registro:
        r = self.por_id(id_)
        assert r is not None
        return r

    def por_id(self, id_: int) -> Registro | None:
        return self._uno("SELECT * FROM comprobantes WHERE id = ?", (id_,))

    def por_clave(self, clave: str) -> Registro | None:
        return self._uno("SELECT * FROM comprobantes WHERE clave = ?", (clave,))

    def por_numero(
        self, cuit: str, ambiente: str, punto_venta: int, cbte_tipo: int, numero: int
    ) -> Registro | None:
        """El comprobante autorizado con ese número (los pendientes no cuentan)."""
        return self._uno(
            "SELECT * FROM comprobantes WHERE cuit_emisor = ? AND ambiente = ? AND punto_venta = ?"
            " AND cbte_tipo = ? AND numero = ? AND estado = 'autorizado'",
            (cuit, ambiente, punto_venta, cbte_tipo, numero),
        )

    def autorizados(
        self, cuit: str, ambiente: str, punto_venta: int, cbte_tipo: int
    ) -> list[Registro]:
        return self._varios(
            "SELECT * FROM comprobantes WHERE cuit_emisor = ? AND ambiente = ? AND punto_venta = ?"
            " AND cbte_tipo = ? AND estado = 'autorizado'",
            (cuit, ambiente, punto_venta, cbte_tipo),
        )

    def sin_resolver(self, cuit: str, ambiente: str) -> list[Registro]:
        """Pendientes e inciertos: comprobantes de los que no se sabe si ARCA los autorizó."""
        return self._varios(
            "SELECT * FROM comprobantes WHERE cuit_emisor = ? AND ambiente = ?"
            " AND estado IN ('pendiente', 'incierto') ORDER BY id",
            (cuit, ambiente),
        )

    def notas_de_credito_de(self, id_: int) -> list[Registro]:
        return self._varios(
            "SELECT * FROM comprobantes WHERE asociado_id = ? AND estado = 'autorizado'", (id_,)
        )

    def posibles_duplicados(self, cuit: str, ambiente: str, c: Comprobante) -> list[Registro]:
        """Facturas ya autorizadas con el mismo receptor, importe y período (y sin anular).

        Sin período (productos) se compara la fecha de emisión. No se avisa para consumidor
        final sin identificar (doc. 99): ahí todos los tickets se parecen.
        """
        if c.receptor.doc_tipo == 99 or c.es_nota_de_credito:
            return []
        candidatos = self._varios(
            "SELECT * FROM comprobantes WHERE cuit_emisor = ? AND ambiente = ? AND cbte_tipo = ?"
            " AND receptor_doc_tipo = ? AND receptor_doc_nro = ? AND importe = ?"
            " AND estado = 'autorizado'",
            (
                cuit,
                ambiente,
                c.cbte_tipo,
                c.receptor.doc_tipo,
                str(c.receptor.doc_nro),
                str(c.total),
            ),
        )
        out = []
        for r in candidatos:
            if c.periodo_desde:
                igual = (r.periodo_desde, r.periodo_hasta) == (c.periodo_desde, c.periodo_hasta)
            else:
                igual = r.periodo_desde is None and r.fecha == c.fecha
            if igual and not self.notas_de_credito_de(r.id):
                out.append(r)
        return out

    def listar(
        self,
        *,
        perfil: str | None = None,
        texto: str | None = None,
        limite: int = 20,
    ) -> list[Registro]:
        sql = "SELECT * FROM comprobantes WHERE 1 = 1"
        args: list[Any] = []
        if perfil:
            sql += " AND perfil = ?"
            args.append(perfil)
        if texto:
            sql += (
                " AND (receptor_nombre LIKE ? OR receptor_doc_nro LIKE ? OR cae LIKE ?"
                " OR printf('%08d', numero) LIKE ?)"
            )
            args += [f"%{texto}%"] * 4
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limite)
        return self._varios(sql, tuple(args))
