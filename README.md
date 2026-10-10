# Facturitas

Facturación electrónica de ARCA (ex AFIP), **fácil, local y abierta**. Emití comprobantes sin
entrar al portal: tus claves quedan en tu máquina y el código es MIT.

> ⚠️ **Estado: M1 (motor ARCA en homologación).** Emite Factura C **solo en homologación**
> (pruebas, sin validez fiscal). Producción todavía no está habilitada en el código. El plan
> completo está en [CLAUDE.md](CLAUDE.md).

## Instalación para desarrollo

Requiere Python 3.12 y [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run facturitas --version
uv run pytest
```

## Uso (M1, homologación)

```
uv run facturitas init            # registra certificado y perfil (pide lo que falta)
uv run facturitas estado          # FEDummy, autenticación y puntos de venta
uv run facturitas ultimo          # último comprobante autorizado (Factura C)
uv run facturitas condiciones-iva # condiciones IVA del receptor válidas para Factura C
uv run facturitas emitir --descripcion "Servicios" --precio 1500 ^
    --receptor-cuit 30000000007 --condicion-iva "IVA Responsable Inscripto"
```

`emitir` **siempre** muestra el preview y pregunta `[s/N]` antes de pedir el CAE. `--yes` salta
la pregunta (para scripts y tests), pero igual imprime el preview. Sin `--desde/--hasta` el
período es el mes en curso, y sin `--vto-pago` vence a los 4 días (`--vto-dias`).

La configuración (rutas al certificado, datos del perfil) se guarda en el directorio de datos
del usuario (en Windows, `%LOCALAPPDATA%/facturitas/config.toml`), nunca en el repo.

## Puntos de venta: homologación vs. producción

- Para usar web services hace falta un punto de venta de tipo **"RECE para aplicativo y web
  services"**. Los puntos de venta de **"Comprobantes en línea"** (por ejemplo el 00001 que se
  usa desde el portal) **no sirven** para web services.
- En **homologación** (pruebas) `FEParamGetPtosVenta` suele venir vacío, pero ARCA acepta
  consultar con cualquier número (probado con el 1, 2, 3 y 4000). `facturitas estado` lo
  muestra y, si la lista está vacía, probá con `facturitas init --punto-venta 1`. La numeración
  de homologación es independiente de la real.
- Para **producción** hay que crear un punto de venta nuevo desde "ABM de puntos de venta"
  en ARCA, de tipo web services. Se hace una sola vez, más adelante.

## Nombre de fantasía

El perfil tiene un campo opcional `nombre_fantasia` que se imprime en el PDF (útil para un
comercio, por ejemplo una panadería). Para ser consistente con ARCA, el nombre de fantasía
tiene que estar **declarado y asociado al punto de venta** en "Administración de Puntos de
Venta y Domicilios".

## Seguridad

- Nunca se apunta a producción en tests, scripts ni ejemplos.
- La clave privada y el certificado viven fuera del repo y no se suben nunca.

## Licencia

MIT. Ver [LICENSE](LICENSE).
