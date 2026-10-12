"""Asistente interactivo de `facturitas init`: de cero a «estado OK», paso a paso y retomable.

Pasos: 1) tus datos, 2) clave y CSR, 3) trámites en ARCA, 4) el certificado (.crt), 5) guardar y
punto de venta, 6) verificar con `estado`. El avance se guarda en `asistente.json` (directorio de
datos) después de cada paso: si se corta, al volver a correr `init` se retoma donde quedó.
La entrada/salida está inyectada (`IO`) para poder probarlo sin consola.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes, PublicKeyTypes

from facturitas import config
from facturitas.arca import ambientes
from facturitas.arca.csr import carpeta_certificados, cuit_del_certificado, generar_clave_y_csr
from facturitas.core import cuit as cuit_mod
from facturitas.core.models import Credencial, Perfil

TOTAL_PASOS = 6
URL_ARCA_LOGIN = "https://auth.afip.gob.ar/contribuyente_/login.xhtml"
URL_WSASS_HOMO = "https://wsass-homo.afip.gob.ar/wsass/portal/main.aspx"
CONDICIONES = {
    "1": "Responsable Monotributo",
    "2": "IVA Sujeto Exento",
}


class IO(Protocol):
    def preguntar(self, texto: str, defecto: str | None = None) -> str: ...
    def confirmar(self, texto: str, defecto: bool = True) -> bool: ...
    def mostrar(self, texto: str = "") -> None: ...
    def abrir(self, url: str) -> None: ...


class AsistenteCancelado(Exception):
    """El usuario salió del asistente (el avance queda guardado)."""


@dataclass
class Avance:
    """Lo que ya se hizo. Se guarda en disco por ambiente para retomar."""

    ambiente: str
    paso: int = 0  # último paso completado (0 = nada)
    perfil: str = "default"
    cuit: str = ""
    razon_social: str = ""
    condicion_iva: str = ""
    domicilio: str = ""
    iibb: str = ""
    nombre_fantasia: str = ""
    inicio_actividades: str = ""  # ISO
    alias: str = ""
    key: str = ""
    csr: str = ""
    crt: str = ""
    punto_venta: int | None = None


def ruta_avance() -> Path:
    return config.directorio_datos() / "asistente.json"


def cargar_avance(ambiente: str) -> Avance | None:
    try:
        datos = json.loads(ruta_avance().read_text(encoding="utf-8"))
        return Avance(**datos[ambiente])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def guardar_avance(a: Avance) -> None:
    try:
        todo = json.loads(ruta_avance().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        todo = {}
    todo[a.ambiente] = asdict(a)
    ruta_avance().write_text(json.dumps(todo, indent=2, ensure_ascii=False), encoding="utf-8")


def olvidar_avance(ambiente: str) -> None:
    try:
        todo = json.loads(ruta_avance().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    todo.pop(ambiente, None)
    ruta_avance().write_text(json.dumps(todo, indent=2, ensure_ascii=False), encoding="utf-8")


def limpiar_ruta(texto: str) -> Path:
    """Acepta una ruta escrita o arrastrada a la consola (con comillas, o `& 'ruta'`)."""
    t = texto.strip()
    if t.startswith("&"):
        t = t[1:].strip()
    t = t.strip("\"'").strip()
    return Path(t).expanduser()


def _publica(k: PublicKeyTypes | PrivateKeyTypes) -> bytes:
    return k.public_bytes(  # type: ignore[union-attr]
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def validar_certificado(crt: Path, key: Path) -> x509.Certificate:
    """Lee el .crt y comprueba que corresponda a la clave y no esté vencido."""
    if not crt.is_file():
        raise ValueError(f"No encuentro el archivo: {crt}")
    try:
        cert = x509.load_pem_x509_certificate(crt.read_bytes())
    except ValueError:
        raise ValueError(
            "Ese archivo no parece un certificado (.crt) en formato PEM. Descargalo de nuevo "
            "desde ARCA (tiene que empezar con «-----BEGIN CERTIFICATE-----»)."
        ) from None
    clave = serialization.load_pem_private_key(key.read_bytes(), password=None)
    if _publica(cert.public_key()) != _publica(clave.public_key()):
        raise ValueError(
            "Este certificado NO corresponde a la clave generada por el asistente. Seguro "
            "descargaste el de otro pedido. Volvé a ARCA y bajá el que pusiste con este CSR."
        )
    if cert.not_valid_after_utc < datetime.now(cert.not_valid_after_utc.tzinfo):
        raise ValueError("El certificado está vencido.")
    return cert


def guardar_configuracion(av: Avance) -> None:
    """Escribe credencial y perfil en la config (lo usan el asistente de consola y el de la GUI)."""
    cfg = config.cargar()
    previo = cfg.perfiles.get(av.perfil)
    if av.ambiente == "homo":
        inicio = date.fromisoformat(av.inicio_actividades) if av.inicio_actividades else None
        base = Perfil(
            cuit=av.cuit,
            razon_social=av.razon_social,
            condicion_iva=av.condicion_iva,
            domicilio=av.domicilio,
            credencial=av.perfil,
            iibb=av.iibb,
            inicio_actividades=inicio,
            punto_venta=av.punto_venta,
            nombre_fantasia=av.nombre_fantasia,
        )
        if previo:
            base = replace(
                base,
                credencial_prod=previo.credencial_prod,
                punto_venta_prod=previo.punto_venta_prod,
                aviso_duplicados=previo.aviso_duplicados,
                categoria_monotributo=previo.categoria_monotributo,
            )
        base.cbte_tipo_default  # noqa: B018 - valida que el tipo de emisor esté soportado
        cfg.credenciales[av.perfil] = Credencial(
            cuit=av.cuit, cert=av.crt, key=av.key, ambiente="homo"
        )
        cfg.perfiles[av.perfil] = base
        if not previo and len(cfg.perfiles) == 1:
            cfg.perfil_default = av.perfil
    else:
        assert previo is not None, "producción necesita un perfil"
        nombre_cred = f"{av.perfil}-prod"
        cfg.credenciales[nombre_cred] = Credencial(
            cuit=av.cuit, cert=av.crt, key=av.key, ambiente="prod"
        )
        cfg.perfiles[av.perfil] = replace(
            previo, credencial_prod=nombre_cred, punto_venta_prod=av.punto_venta
        )
    config.guardar(cfg)


class Asistente:
    def __init__(
        self,
        io: IO,
        verificar: Callable[[str, str], bool],
        hoy: Callable[[], date] = date.today,
    ) -> None:
        self.io = io
        self.verificar = verificar
        self.hoy = hoy

    # --- utilidades ------------------------------------------------------------------------

    def _paso(self, n: int, titulo: str) -> None:
        self.io.mostrar()
        self.io.mostrar(f"━━ Paso {n} de {TOTAL_PASOS}: {titulo} ━━")

    def _pedir(self, texto: str, defecto: str | None = None, obligatorio: bool = True) -> str:
        while True:
            r = self.io.preguntar(texto, defecto).strip()
            if r.lower() in {"salir", "q"}:
                raise AsistenteCancelado
            if r or not obligatorio:
                return r
            self.io.mostrar("  Este dato es obligatorio.")

    # --- flujo -----------------------------------------------------------------------------

    def correr(self, ambiente: str | None = None) -> bool:
        """Devuelve True si terminó con todo en orden."""
        io = self.io
        io.mostrar("Facturitas — asistente de configuración")
        io.mostrar("Te guío paso a paso. Podés escribir «salir» en cualquier pregunta: el avance")
        io.mostrar("queda guardado y la próxima vez seguimos desde ahí.")
        try:
            av = self._elegir_ambiente_y_retomar(ambiente)
            if av.paso < 1:
                self._datos(av)
            if av.paso < 2:
                self._clave_y_csr(av)
            if av.paso < 3:
                self._tramites_arca(av)
            if av.paso < 4:
                self._certificado(av)
            if av.paso < 5:
                self._guardar(av)
            ok = self._verificar(av)
        except AsistenteCancelado:
            io.mostrar()
            io.mostrar("Listo, salimos. Volvé a correr `facturitas init` para retomar.")
            return False
        if ok:
            olvidar_avance(av.ambiente)
        return ok

    def _elegir_ambiente_y_retomar(self, ambiente: str | None) -> Avance:
        io = self.io
        if ambiente is None:
            io.mostrar()
            io.mostrar("¿Con qué ambiente querés empezar?")
            io.mostrar("  1) Homologación — pruebas, sin validez fiscal (recomendado para empezar)")
            io.mostrar("  2) Producción — comprobantes reales con validez fiscal")
            ambiente = "prod" if self._pedir("Elegí 1 o 2", "1") == "2" else "homo"
        amb = ambientes.obtener(ambiente)
        cfg = config.cargar()
        if amb.es_produccion and not cfg.perfiles:
            io.mostrar()
            io.mostrar("Para producción primero hay que dejar andando homologación (es la prueba")
            io.mostrar("que pide ARCA y nos asegura que todo funcione). Empezamos por ahí.")
            amb = ambientes.HOMO
        previo = cargar_avance(amb.nombre)
        if previo and previo.paso < TOTAL_PASOS:
            io.mostrar()
            io.mostrar(
                f"Encontré un asistente a medias ({amb.titulo}, hecho hasta el paso {previo.paso} "
                f"de {TOTAL_PASOS})."
            )
            if io.confirmar("¿Querés seguir desde ahí?", True):
                return previo
            olvidar_avance(amb.nombre)
        return Avance(ambiente=amb.nombre, perfil=cfg.perfil_default if cfg.perfiles else "default")

    def _datos(self, av: Avance) -> None:
        io = self.io
        self._paso(1, "Tus datos")
        cfg = config.cargar()
        previo = cfg.perfiles.get(av.perfil)
        if av.ambiente == "prod" and previo:
            # Producción cuelga de un perfil que ya existe: no se vuelven a pedir sus datos.
            av.cuit, av.razon_social = previo.cuit, previo.razon_social
            av.condicion_iva, av.domicilio = previo.condicion_iva, previo.domicilio
            io.mostrar(f"Perfil: {previo.razon_social} — CUIT {cuit_mod.formatear(previo.cuit)}")
        else:
            while True:
                texto = self._pedir("Tu CUIT (con o sin guiones)", previo.cuit if previo else None)
                try:
                    av.cuit = cuit_mod.normalizar(texto)
                    break
                except ValueError as e:
                    io.mostrar(f"  {e}")
            av.razon_social = self._pedir(
                "Tu nombre o razón social (como figura en ARCA)",
                previo.razon_social if previo else None,
            )
            io.mostrar("Tu condición frente al IVA:")
            io.mostrar("  1) Monotributista")
            io.mostrar("  2) IVA Sujeto Exento")
            io.mostrar("  (Responsable Inscripto: Factura A/B todavía no está disponible.)")
            while True:
                eleccion = self._pedir("Elegí 1 o 2", "1")
                if eleccion in CONDICIONES:
                    av.condicion_iva = CONDICIONES[eleccion]
                    break
                io.mostrar("  Escribí 1 o 2.")
            av.domicilio = self._pedir(
                "Tu domicilio comercial (sale impreso en la factura)",
                previo.domicilio if previo else None,
            )
            av.iibb = self._pedir(
                "Ingresos Brutos (ej.: Exento, o tu número) — Enter para dejar en blanco",
                None,
                obligatorio=False,
            )
            av.nombre_fantasia = self._pedir(
                "Nombre de fantasía (opcional) — Enter para omitir", None, obligatorio=False
            )
            while True:
                inicio = self._pedir(
                    "Inicio de actividades DD/MM/AAAA (opcional) — Enter para omitir",
                    None,
                    obligatorio=False,
                )
                if not inicio:
                    break
                try:
                    av.inicio_actividades = datetime.strptime(inicio, "%d/%m/%Y").date().isoformat()
                    break
                except ValueError:
                    io.mostrar("  Usá el formato DD/MM/AAAA.")
        sufijo = "prod" if av.ambiente == "prod" else "homo"
        av.alias = self._pedir("Alias para el certificado", f"facturitas-{sufijo}")
        av.paso = 1
        guardar_avance(av)

    def _clave_y_csr(self, av: Avance) -> None:
        io = self.io
        self._paso(2, "Clave privada y pedido de certificado")
        key, csr, nueva = generar_clave_y_csr(
            carpeta_certificados(av.ambiente), av.alias, av.cuit, av.razon_social
        )
        av.key, av.csr = str(key), str(csr)
        io.mostrar(
            f"✓ {'Generé' if nueva else 'Reutilizo'} tu clave privada: {key}\n"
            "  Es secreta: no la compartas ni la subas a ningún lado."
        )
        io.mostrar(f"✓ Pedido de certificado (CSR): {csr}")
        io.mostrar("  Esto es lo único que se sube a ARCA.")
        av.paso = 2
        guardar_avance(av)

    def _tramites_arca(self, av: Avance) -> None:
        io = self.io
        self._paso(3, "Trámites en ARCA")
        csr_texto = Path(av.csr).read_text(encoding="utf-8")
        if av.ambiente == "homo":
            pasos = [
                f"Entrá a ARCA con tu CUIT y clave fiscal ({URL_ARCA_LOGIN}).",
                "Abrí el servicio «WSASS - Autogestión Certificados Homologación». Si no te",
                "   aparece, agregalo en «Administrador de Relaciones de Clave Fiscal».",
                f"Elegí «Nuevo certificado», poné el alias «{av.alias}» y pegá el CSR de abajo.",
                "Descargá el certificado (.crt) que te devuelve ARCA.",
                "En el mismo sitio, «Crear autorización a servicio»: elegí tu CUIT, el servicio",
                f"   «wsfe» (Facturación Electrónica) y el alias «{av.alias}».",
            ]
            url = URL_WSASS_HOMO
        else:
            pasos = [
                f"Entrá a ARCA con tu CUIT y clave fiscal ({URL_ARCA_LOGIN}).",
                "Abrí «Administración de Certificados Digitales» (la de producción, no WSASS).",
                f"Agregá un alias nuevo («{av.alias}») y pegá el CSR de abajo.",
                "Descargá el certificado (.crt).",
                "Abrí «Administrador de Relaciones de Clave Fiscal» → Nueva relación → servicio",
                f"   «Facturación Electrónica» (wsfe) → asocialo al alias «{av.alias}».",
                "Abrí «Administración de puntos de venta y domicilios» → Agregar punto de venta",
                "   de tipo «Factura Electrónica - Monotributo - Web Services» (los de",
                "   «Comprobantes en línea» no sirven). Anotá el número que te asigne.",
            ]
            url = URL_ARCA_LOGIN
        n = 0
        for p in pasos:
            if p.startswith("   "):  # continuación del paso anterior
                io.mostrar(f"     {p.strip()}")
            else:
                n += 1
                io.mostrar(f"  {n}. {p}")
        io.mostrar()
        io.mostrar("Este es tu pedido de certificado (CSR), pegalo completo en ARCA:")
        io.mostrar(csr_texto)
        if io.confirmar("¿Abro ARCA en tu navegador?", True):
            io.abrir(url)
        io.mostrar("Cuando tengas el archivo .crt descargado, seguimos.")
        io.preguntar("Presioná Enter para continuar", "")
        av.paso = 3
        guardar_avance(av)

    def _certificado(self, av: Avance) -> None:
        io = self.io
        self._paso(4, "El certificado de ARCA")
        io.mostrar("Pasame el archivo .crt: escribí la ruta, o arrastralo a esta ventana y Enter.")
        while True:
            texto = self._pedir("Ruta del .crt")
            ruta = limpiar_ruta(texto)
            try:
                cert = validar_certificado(ruta, Path(av.key))
            except ValueError as e:
                io.mostrar(f"✗ {e}")
                continue
            en_cert = cuit_del_certificado(cert.public_bytes(serialization.Encoding.PEM))
            if en_cert and en_cert != av.cuit:
                io.mostrar(
                    f"⚠ El certificado es del CUIT {cuit_mod.formatear(en_cert)}, distinto del "
                    f"que cargaste ({cuit_mod.formatear(av.cuit)})."
                )
                if not io.confirmar(
                    "¿Es a propósito (facturás en nombre de otra persona con tu certificado)?",
                    False,
                ):
                    continue
            break
        destino = Path(av.key).with_suffix(".crt")
        if ruta.resolve() != destino.resolve():
            destino.write_bytes(ruta.read_bytes())
        av.crt = str(destino)
        io.mostrar(f"✓ Certificado válido y vigente hasta {cert.not_valid_after_utc:%d/%m/%Y}.")
        av.paso = 4
        guardar_avance(av)

    def _guardar(self, av: Avance) -> None:
        io = self.io
        self._paso(5, "Guardar la configuración")
        cfg = config.cargar()
        if av.ambiente == "homo":
            otro = cfg.perfiles.get(av.perfil)
            if otro and otro.cuit != av.cuit:
                av.perfil = self._pedir("Ya hay otro perfil «default». Nombre para este", av.cuit)
            if av.punto_venta is None:
                io.mostrar("En homologación cualquier punto de venta sirve; usamos el 1.")
                av.punto_venta = int(self._numero("Punto de venta", 1))
        else:
            if av.punto_venta is None:
                io.mostrar("Poné el número del punto de venta web services que creaste en ARCA.")
                av.punto_venta = int(self._numero("Punto de venta de producción", None))
        guardar_configuracion(av)
        io.mostrar(f"✓ Configuración guardada ({config.ruta_config()}).")
        if av.ambiente == "prod":
            io.mostrar("  Producción NO queda activa todavía: eso lo hacés vos con")
            io.mostrar("  `facturitas ambiente usar prod` cuando `estado` te dé todo OK.")
        av.paso = 5
        guardar_avance(av)

    def _numero(self, texto: str, defecto: int | None) -> int:
        while True:
            r = self._pedir(texto, None if defecto is None else str(defecto))
            if re.fullmatch(r"\d{1,5}", r) and int(r) > 0:
                return int(r)
            self.io.mostrar("  Escribí un número, por ejemplo 1.")

    def _verificar(self, av: Avance) -> bool:
        io = self.io
        self._paso(6, "Probar la conexión con ARCA")
        while True:
            ok = self.verificar(av.perfil, av.ambiente)
            if ok:
                av.paso = TOTAL_PASOS
                guardar_avance(av)
                io.mostrar()
                io.mostrar("🎉 ¡Todo listo!")
                if av.ambiente == "homo":
                    io.mostrar("Ya podés emitir pruebas: `facturitas emitir`, o desde tu IA.")
                    io.mostrar("Cuando quieras pasar a producción: `facturitas init` otra vez y")
                    io.mostrar("elegí «Producción».")
                else:
                    io.mostrar("Activá producción cuando quieras: `facturitas ambiente usar prod`.")
                return True
            io.mostrar()
            io.mostrar("✗ Algo no está en orden (mirá el detalle de arriba).")
            io.mostrar("  Revisá en ARCA que el alias esté asociado al servicio wsfe.")
            if not io.confirmar("¿Reintento la prueba?", True):
                io.mostrar("Queda guardado. Cuando lo arregles: `facturitas estado`.")
                return False
