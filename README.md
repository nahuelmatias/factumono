# Facturitas

Facturación electrónica de ARCA (ex AFIP), **fácil, local y abierta**. Emití comprobantes sin
entrar al portal: tus claves quedan en tu máquina y el código es MIT.

> ⚠️ **Estado: M3 (MCP, en homologación).** Emite Factura C y Nota de Crédito C
> **solo en homologación** (pruebas, sin validez fiscal), guarda historial y clientes, genera el
> PDF, reconcilia con ARCA si se corta la red y se puede usar desde una IA (MCP). Producción todavía no está habilitada en el código.
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

### Clientes y "la de siempre"

```
uv run facturitas cliente agregar acciona --nombre "ACCIONA IT S.R.L" --cuit 30710581467 ^
    --condicion-iva "IVA Responsable Inscripto" --descripcion "Servicios de innovación" ^
    --unidad "otras unidades" --concepto servicios --vto-dias 0
uv run facturitas cliente listar              # (también: editar, borrar)
uv run facturitas emitir --cliente acciona --precio "1.093.843,20" --periodo septiembre
uv run facturitas emitir --cliente seba --repetir-ultima
```

Los clientes se guardan por perfil en la base local del usuario (nunca en el repo). Con
`--cliente` se usan sus datos y defaults; `--repetir-ultima` repite la última factura a ese
cliente con el período corrido al mes que sigue (un mes completo pasa al mes completo siguiente;
otro tipo de período conserva sus días corridos un mes). `--periodo` acepta `septiembre`,
`septiembre 2026`, `09/2026` o `DD/MM/AAAA al DD/MM/AAAA`.

## Usarlo desde una IA (MCP)

`facturitas mcp` es un servidor MCP por stdio. Las tools (todas indican el ambiente en cada
respuesta): `estado_arca`, `listar_perfiles`, `listar_clientes`, `guardar_cliente`,
`ultimo_comprobante`, `listar_comprobantes`, `preparar_comprobante`, `emitir_comprobante`,
`consultar_comprobante`, `reimprimir_pdf`, `nota_de_credito` y `resumen_facturacion`.

**Nada se emite sin confirmar:** `preparar_comprobante` (y `nota_de_credito`) devuelven un preview
y un `borrador_id` (vence a los 15 minutos); `emitir_comprobante` exige ese borrador y
`confirmo=true`, y si el preview avisó un posible duplicado o una factura ya anulada, además
`confirmo_duplicado=true`.

### Conectarlo

```
uv run facturitas mcp instalar      # agrega Facturitas a Claude Desktop (hace backup antes)
uv run facturitas mcp instalar --solo-mostrar   # solo muestra los snippets
```

Escribe en `claude_desktop_config.json` la entrada `uv run --directory <repo> facturitas mcp`
(con backup `*.bak-<fecha>` del archivo anterior; no toca nada más) y muestra los equivalentes:

```
# Claude Code
claude mcp add facturitas -- uv run --directory <repo> facturitas mcp
# Gemini CLI
gemini mcp add facturitas uv run --directory <repo> facturitas mcp
```

Reiniciá Claude Desktop (cerralo del todo) para que aparezca. Frases que funcionan:
*"Facturale a Acciona 1.093.843,20 por septiembre"*, *"Hacele la de siempre a Seba"*,
*"¿Cuánto facturé este año?"*, *"Mostrame las últimas 5 facturas"*, *"Anulá la factura 5"*.
Siempre te muestra el preview y espera tu confirmación.

La skill [`skills/facturacion-mensual/SKILL.md`](skills/facturacion-mensual/SKILL.md) le
explica a la IA la rutina mensual (qué preguntar, qué defaults usar y que nunca confirme sin
mostrar el preview). Copiala a tu carpeta de skills (por ejemplo `~/.claude/skills/`).

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
