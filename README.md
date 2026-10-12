# Facturitas

Facturación electrónica de ARCA (ex AFIP), **fácil, local y abierta**. Emití comprobantes sin
entrar al portal: tus claves quedan en tu máquina y el código es MIT.

> ⚠️ **Estado: M3 (MCP) + soporte de producción.** Emite Factura C y Nota de Crédito C, guarda
> historial y clientes, genera el PDF, reconcilia con ARCA si se corta la red y se puede usar
> desde una IA (MCP). **Arranca siempre en homologación** (pruebas, sin validez fiscal):
> producción hay que activarla a mano (ver [Pasar a producción](#pasar-a-producción)).
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
período (y no está anulada), el preview lo menciona. Es **solo informativo**: no pide ninguna
confirmación extra (un comercio puede facturarle dos veces al mismo cliente el mismo día). No
avisa para Consumidor Final sin identificar, donde todos los tickets se parecen. Se puede
apagar por perfil con `facturitas init --sin-aviso-duplicados` (y volver a prender con
`--aviso-duplicados`).

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
`confirmo=true`. Solo al **anular una factura que ya estaba anulada** hace falta además
`confirmo_duplicado=true`. El ambiente no se puede cambiar desde la IA: solo desde la terminal.

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
  en ARCA, de tipo web services (ver [Pasar a producción](#pasar-a-producción)).

## Pasar a producción

Facturitas arranca siempre en **homologación**. Producción (comprobantes con validez fiscal) es
otro ambiente, con su propio certificado, su propio punto de venta, su propio historial, su propio
caché de tickets de acceso y su propia carpeta de PDF. Nada se mezcla con lo de pruebas.

### 1. En ARCA

1. **Certificado de producción.** Es distinto del de homologación. Generá la clave y el pedido
   (CSR) con `uv run python tools/gen_csr.py` (o con la herramienta que prefieras), entrá con
   clave fiscal a **«Administración de Certificados Digitales»** (la de producción, no la de
   homologación), agregá un alias (por ejemplo `facturitas-prod`), pegá el CSR y
   descargá el `.crt`. Guardá el `.key` y el `.crt` fuera del repo, por ejemplo en
   `~/.facturitas/prod/facturitas-prod.key` y `.crt`.
2. **Relación con el web service.** En **«Administrador de Relaciones de Clave Fiscal»** →
   *Nueva relación* → servicio **ARCA → WebServices → «Facturación Electrónica»** (`wsfe`), y
   asociale el alias del certificado. Si facturás en nombre de otra persona, esa persona tiene que
   delegarle `wsfe` a tu CUIT desde el mismo lugar.
3. **Punto de venta.** En **«Administración de Puntos de Venta y Domicilios»** → *A/B/M de puntos
   de venta* → *Agregar*, de tipo **«Factura Electrónica - Monotributo - Web Services»** (o
   «Factura Electrónica - Responsable Inscripto - Web Services» si no sos monotributista).
   Los puntos de venta de «Comprobantes en línea» **no sirven** para web services. Anotá el
   número que te asignó (por ejemplo el 2); la numeración arranca en 1.

### 2. En Facturitas

```
uv run facturitas init --ambiente prod --punto-venta 2      # registra el certificado y el pto. de venta reales
uv run facturitas estado --ambiente prod                    # chequeo: no activa nada
uv run facturitas ambiente usar prod                        # recién acá se activa
```

- `init --ambiente prod` usa por defecto `~/.facturitas/prod/facturitas-prod.crt` y `.key` si
  existen (o pasá `--cert` y `--key`). **No activa** producción.
- `estado --ambiente prod` consulta ARCA en producción (solo lectura) y tiene que dar todo OK:
  FEDummy, autenticación y que el punto de venta configurado figure en `FEParamGetPtosVenta`
  (que solo lista los de web services), sin bloqueo ni baja. Lista los puntos de venta reales con
  su tipo y marca cuál es el configurado.
- `ambiente usar prod` repite ese chequeo, exige además al menos una emisión de prueba autorizada
  en homologación para ese CUIT, y pregunta antes de activar. Si algo falla, no cambia nada.
  `facturitas ambiente` muestra el ambiente activo y `facturitas ambiente usar homo` vuelve a
  pruebas. `facturitas config --ambiente prod|homo` hace lo mismo.
- Con producción activa, **toda** salida de la CLI, todo preview y toda respuesta del MCP empieza
  con `⚠️ PRODUCCIÓN — comprobante con validez fiscal` (en homologación, `🧪 HOMOLOGACIÓN`).
- Los PDF de producción van **sin marca de agua** en `Documentos/Facturitas/<perfil>/`; los de
  homologación siguen en la subcarpeta `homologacion/`.
- El historial de producción es otro archivo (`historial-prod.sqlite`), así que la numeración y
  los avisos de homologación no se cruzan con los reales. Los clientes guardados se comparten.
- La IA (MCP) **no puede** cambiar de ambiente: solo usa el que dejaste activo.

La primera factura real conviene emitirla vos, mirando el preview completo.

## Usarlo en una segunda PC

> Hoy el historial, los clientes y los PDF viven en cada PC. La sincronización está planificada
> (hito M3.5 en [CLAUDE.md](CLAUDE.md)); mientras tanto, esto es lo que hay que saber.

- **La numeración no se pisa:** antes de emitir siempre se consulta a ARCA el último número
  autorizado, así que dos PCs con el mismo punto de venta no repiten número.
- **El historial local queda incompleto:** lo emitido desde la otra PC no aparece acá. Con
  `facturitas recuperar <desde> <hasta>` se trae de ARCA (solo los totales: ARCA no guarda ítems
  ni datos del receptor).
- **Certificado propio por PC:** no copies la clave privada de una PC a otra. En la segunda PC
  generá otra clave y otro pedido (CSR), subilo en «Administración de Certificados Digitales» con
  **otro alias** (por ejemplo `facturitas-prod-pc2`) y asocialo a la misma relación de `wsfe`
  en «Administrador de Relaciones de Clave Fiscal». Después, `facturitas init --ambiente prod`.
- Los clientes guardados hay que cargarlos de nuevo en cada PC (por ahora).

## Nombre de fantasía

El perfil tiene un campo opcional `nombre_fantasia` que se imprime en el PDF (útil para un
comercio, por ejemplo una panadería). Para ser consistente con ARCA, el nombre de fantasía
tiene que estar **declarado y asociado al punto de venta** en "Administración de Puntos de
Venta y Domicilios".

## Seguridad

- Nunca se apunta a producción en tests, scripts ni ejemplos. Un guard en `tests/conftest.py`
  hace fallar cualquier test que intente conectarse a los hosts de producción de ARCA.
- La clave privada y el certificado viven fuera del repo y no se suben nunca.

## Licencia

MIT. Ver [LICENSE](LICENSE).
