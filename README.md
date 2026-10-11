# Facturitas

Facturación electrónica de ARCA (ex AFIP), **fácil, local y abierta**. Emití comprobantes sin
entrar al portal: tus claves quedan en tu máquina y el código es MIT.

> ⚠️ **Estado: M2 (comprobante completo, en homologación).** Emite Factura C y Nota de Crédito C
> **solo en homologación** (pruebas, sin validez fiscal), guarda historial, genera el PDF y
> reconcilia con ARCA si se corta la red. Producción todavía no está habilitada en el código.
> El plan completo está en [CLAUDE.md](CLAUDE.md).

## Instalación para desarrollo

Requiere Python 3.12 y [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run facturitas --version
uv run pytest
```

## Uso (M2, homologación)

```
uv run facturitas init            # registra certificado y perfil (pide lo que falta)
uv run facturitas estado          # FEDummy, autenticación y puntos de venta
uv run facturitas ultimo          # último comprobante autorizado (Factura C)
uv run facturitas condiciones-iva # condiciones IVA del receptor válidas para Factura C
uv run facturitas emitir --descripcion "Servicios" --precio 1500 ^
    --receptor-cuit 30000000007 --condicion-iva "IVA Responsable Inscripto"
uv run facturitas historial       # todo lo emitido (--buscar texto, --limite N)
uv run facturitas pdf 4           # genera / reimprime el PDF del comprobante Nro. 4
uv run facturitas nota-credito 4 --motivo "Error de facturación"   # anula la factura 4
uv run facturitas reconciliar     # resuelve comprobantes 'inciertos' tras un corte de red
uv run facturitas recuperar 1 3   # trae de ARCA comprobantes emitidos antes del historial
uv run facturitas config --carpeta-pdf "D:\Facturas"              # dónde se guardan los PDF
```

`emitir` y `nota-credito` **siempre** muestran el preview y preguntan `[s/N]` antes de pedir el
CAE. `--yes` salta la pregunta (para scripts y tests), pero igual imprime el preview. Sin
`--desde/--hasta` el período es el mes en curso, y sin `--vto-pago` vence a los 4 días
(`--vto-dias`). Las fechas siempre se muestran como DD/MM/AAAA.

### Historial

Todo lo emitido queda en una base SQLite del directorio de datos (`historial.sqlite`), incluida
la homologación, que se marca como `HOMO` en `facturitas historial`.

**Aviso de posible duplicado:** si ya emitiste una factura con el mismo perfil, receptor, importe y
período (y no está anulada), el preview lo avisa y pide una confirmación extra. No bloquea. No
avisa para Consumidor Final sin identificar, donde todos los tickets se parecen.

### PDF

Se genera solo al emitir, con el formato del comprobante de ARCA (ORIGINAL, DUPLICADO y
TRIPLICADO, QR con la URL de `arca.gob.ar`). El nombre es el mismo que usa ARCA:
`<CUIT>_<tipo>_<PV>_<NRO>.pdf`, por ejemplo `20000000001_011_00001_00000103.pdf`.

Se guardan en `Documentos/Facturitas/<perfil>/`; cambiá la carpeta con `facturitas config
--carpeta-pdf`. Los de **homologación van a la subcarpeta `homologacion/`** y llevan una marca
de agua: su numeración arranca de cero y los nombres chocarían con los de los comprobantes reales.
Todavía no lleva el logo de ARCA (la especificación del QR no lo menciona).

### Si se corta la red

Antes de mandar el pedido de CAE, el número a usar se guarda en el historial. Si la conexión se
corta después de enviarlo, Facturitas **consulta a ARCA (`FECompConsultar`) antes de reintentar**:
si ya estaba autorizado, lo registra con su CAE y no emite otro; si no, te lo dice y se puede
emitir de nuevo sin riesgo. Si ni siquiera se puede consultar, queda como `incierto` y
`facturitas reconciliar` lo resuelve cuando vuelva la conexión. La `--clave-idempotencia` de
`emitir` permite repetir el mismo pedido sin duplicar.

### Nota de crédito

`facturitas nota-credito <nro> --motivo "..."` anula **por completo** la factura (misma
receptora, ítems e importe) con una Nota de Crédito C asociada. Si la factura ya tiene una nota de
crédito, pide una confirmación extra. Las notas de crédito parciales llegan más adelante.

La configuración (rutas al certificado, datos del perfil) se guarda en el directorio de datos
del usuario (en Windows, `%LOCALAPPDATA%/facturitas/config.toml`), nunca en el repo.

## Tests

```
uv run pytest             # unitarios (fixtures SOAP sintéticas y reales sanitizadas)
uv run pytest -m homo     # integración de solo lectura contra homologación (requiere perfil)
uv run python tools/grabar_fixtures.py [--emitir]   # re-graba tests/fixtures/real (homologación)
```

`tests/fixtures/real/` tiene respuestas reales de homologación con los CUIT cambiados por ficticios
y sin token ni sign.

## Puntos de venta: homologación vs. producción

- Para usar web services hace falta un punto de venta de tipo **"RECE para aplicativo y web
  services"**. Los puntos de venta de **"Comprobantes en línea"** (por ejemplo el 00001 que se
  usa desde el portal) **no sirven** para web services.
- En **homologación** (pruebas) `FEParamGetPtosVenta` suele venir vacío, pero ARCA acepta
  consultar con cualquier número (probado con el 1, 2, 3 y 4000). `facturitas estado` lo
  muestra como informativo (no es un error) y, si falta definir el punto de venta, sugiere
  `facturitas init --punto-venta N`. La numeración
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
