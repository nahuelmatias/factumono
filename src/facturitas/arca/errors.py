"""Errores de ARCA traducidos a mensajes claros en español."""

# código -> (explicación, acción sugerida)
MAPA_ERRORES: dict[int, tuple[str, str]] = {
    600: (
        "No hay un ticket de acceso válido.",
        "Reintentá; si persiste, revisá la autorización del certificado para wsfe.",
    ),
    601: (
        "El CUIT del representado no coincide con el del ticket de acceso.",
        "Revisá que el CUIT del perfil sea el que autorizó el certificado.",
    ),
    602: (
        "No hay puntos de venta habilitados para web services.",
        "Creá un punto de venta de tipo 'RECE para aplicativo y web services' en ARCA. "
        "Los de 'Comprobantes en línea' no sirven.",
    ),
    10015: (
        "El número de documento del receptor es inválido para el tipo de documento.",
        "Revisá el CUIT/DNI del receptor.",
    ),
    10016: (
        "El número de comprobante no es el siguiente al último autorizado.",
        "Consultá el último autorizado (`facturitas ultimo`) y reintentá.",
    ),
    10238: ("La CUIT del receptor no existe.", "Verificá la CUIT del receptor."),
    10242: (
        "La condición frente al IVA del receptor no es un valor permitido.",
        "Consultá las opciones con `facturitas condiciones-iva`.",
    ),
    10243: (
        "La condición frente al IVA del receptor no es válida para esta clase de comprobante.",
        "Elegí una condición válida para Factura C (ver `facturitas condiciones-iva`).",
    ),
    10245: (
        "Falta la condición frente al IVA del receptor (RG 5616).",
        "Indicá la condición del receptor.",
    ),
    10246: (
        "La condición frente al IVA del receptor es obligatoria (RG 5616).",
        "Indicá la condición del receptor.",
    ),
    10247: (
        "La CUIT del receptor está inactiva o es inválida.",
        "Verificá la CUIT del receptor.",
    ),
}

# fragmento del faultstring del WSAA -> explicación
FALLAS_WSAA: dict[str, str] = {
    "coe.alreadyAuthenticated": (
        "ARCA ya emitió un ticket de acceso válido para este certificado y servicio, y no "
        "permite pedir otro hasta que venza (unas 12 horas)."
    ),
    "cms.cert.untrusted": (
        "El certificado no es de confianza para este ambiente. ¿Es un certificado de "
        "homologación usado contra producción, o al revés?"
    ),
    "cms.cert.expired": "El certificado está vencido. Generá uno nuevo.",
    "cms.sign.invalid": "La firma del pedido es inválida: la clave no corresponde al certificado.",
    "cms.bad": "El pedido de acceso está mal formado.",
    "coe.notAuthorized": (
        "El certificado no está autorizado para el servicio wsfe. Asocialo al servicio en WSASS "
        "(homologación) o en el Administrador de Relaciones (producción)."
    ),
    "xml.source.invalid": "El pedido de acceso (TRA) no es un XML válido.",
    "xml.bad": "El pedido de acceso (TRA) está mal formado.",
}


class ArcaError(Exception):
    """Error devuelto por ARCA (o al hablar con ARCA)."""

    def __init__(self, mensaje: str, codigo: int | None = None) -> None:
        super().__init__(mensaje)
        self.codigo = codigo


class WsaaError(ArcaError):
    pass


class ComprobanteRechazado(ArcaError):
    """ARCA devolvió Resultado = R."""


class EstadoIncierto(ArcaError):
    """Se cortó la comunicación después de enviar FECAESolicitar: no se sabe si se emitió."""


def traducir(codigo: int, mensaje_arca: str) -> str:
    if codigo in MAPA_ERRORES:
        explicacion, accion = MAPA_ERRORES[codigo]
        return f"[{codigo}] {explicacion} {accion}"
    return f"[{codigo}] {mensaje_arca}"


def traducir_falla_wsaa(faultstring: str) -> str:
    for clave, texto in FALLAS_WSAA.items():
        if clave in faultstring:
            return f"{texto} (ARCA: {faultstring})"
    return f"ARCA rechazó el pedido de acceso: {faultstring}"
