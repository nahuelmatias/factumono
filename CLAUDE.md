# Facturitas — facturación electrónica ARCA fácil, local y abierta

> Este archivo es la especificación y guía de trabajo del proyecto. Leelo entero antes de escribir código.
> Idioma del proyecto: código, nombres y commits en inglés; UI, mensajes al usuario, README y docs en español rioplatense.

## 1. Objetivo

Una app **open source, local y multiplataforma** (Windows primero, Linux, macOS) para emitir comprobantes electrónicos en ARCA (ex AFIP) **sin entrar al portal**. Debe poder usarla alguien no técnico ("Miguel") y debe poder invocarse desde una IA (MCP) o desde otro sistema (API REST local).

Un solo motor, cuatro caras:
1. **Web UI local** (`http://127.0.0.1:<puerto>`): asistente de certificado, perfiles, formulario de factura, historial y PDFs. Es la cara principal para no técnicos. La IA es opcional.
2. **MCP server (stdio)**: para Claude Desktop / Claude Code / Gemini CLI / Cursor / VS Code.
3. **API REST local**: para integrar sistemas (primer consumidor: el POS "Postaberry" de una panadería).
4. **CLI**: para power users y automatización (Programador de tareas, cron).

## 2. Usuarios y casos reales (usarlos como casos de prueba)

- **Monotributista con un cliente fijo (Matías)**: una Factura C por mes a una empresa Responsable Inscripta (ej. CUIT 30-71058146-7). El monto cambia todos los meses. Un ítem tipo `1 × "otras unidades" × "Servicios de innovación"`. Concepto = Servicios, con período facturado (desde/hasta) y vencimiento de pago. También alguna Factura C chica a Consumidor Final.
- **Operador / "contador" (la novia de Matías)**: factura en nombre de varias personas (Matías, su padre Miguel, etc.) usando **un solo certificado propio**, gracias a la **delegación** de ARCA (cada representado le delega el web service de factura electrónica al CUIT del operador en "Administrador de Relaciones de Clave Fiscal"). En la app esto es: 1 credencial (certificado) → N perfiles (CUIT representados).
- **Miguel (gestión de micros escolares)**: muchas facturas por mes a padres (Consumidor Final). Necesita **emisión por lote desde Excel/CSV** y una UI muy simple.
- **Postaberry (POS de panadería)**: emite en cada venta, vía API REST local, con ítems de producto: `1 docena de facturas`, `0.5 kg de pan`. Concepto = Productos. Puede ser Factura C (si el comercio es monotributista) o B/A (si es Responsable Inscripto, con IVA por alícuota). Necesita **idempotencia** y tolerancia a cortes.

## 3. Decisiones de arquitectura (no cambiar sin consultar)

- **Lenguaje**: Python 3.12, gestionado con `uv`.
- **Cliente ARCA propio y liviano** (WSAA + WSFEv1), no dependemos de pyafipws/PyARCA en runtime: son GPL/LGPL y queremos licencia **MIT** para que cualquiera (incluido un POS comercial) lo use. Usar pyafipws (https://github.com/reingart/pyafipws) y PyARCA (https://github.com/GeraCollante/PyARCA) **solo como referencia de comportamiento**, sin copiar código.
- **SOAP**: `zeep` o `httpx` + plantillas XML (elegir lo que sea más robusto para empaquetar con PyInstaller; los WSDL deben ir **embebidos en el paquete**, no descargarse en runtime).
- **Firma WSAA**: `cryptography` (PKCS7/CMS **con el contenido embebido**, no detached, en DER y luego base64).
- **Web/API**: FastAPI + uvicorn. UI con plantillas Jinja2 + htmx + CSS simple. **Sin build de Node**, para que el empaquetado sea trivial.
- **MCP**: SDK oficial `mcp` de Python (FastMCP), transporte stdio.
- **PDF**: `reportlab` + `qrcode`.
- **Persistencia**: SQLite (historial de comprobantes, borradores, clientes, perfiles) en el directorio de datos del usuario (`platformdirs`).
- **Secretos**: la clave privada nunca sale de la máquina. Se guarda en el directorio de datos con permisos restrictivos y, opcionalmente, cifrada con passphrase (la passphrase va en el keyring del SO vía `keyring`). Nada de secretos en el repo, en logs ni en respuestas del MCP.
- **Un solo ejecutable** `facturitas` con subcomandos: `facturitas app` (UI + API, abre el navegador), `facturitas mcp` (stdio), y los comandos de CLI.

## 4. Reglas de negocio ARCA (verificar contra la documentación oficial vigente antes de implementar)

Documentación: manuales de WSAA y WSFEv1 en el sitio de ARCA (sección Web Services / Factura Electrónica). **Verificá endpoints y campos en la doc actual**; los de abajo son la referencia de partida. Última verificación: 10/10/2026 contra el manual WSFEv1 **v4.7** (revisión del 1/9/2026), la especificación del QR y los WSDL oficiales de homologación.

**Endpoints**
- WSAA homologación: `https://wsaahomo.afip.gov.ar/ws/services/LoginCms`
- WSAA producción: `https://wsaa.afip.gov.ar/ws/services/LoginCms`
- WSFEv1 homologación: `https://wswhomo.afip.gov.ar/wsfev1/service.asmx`
- WSFEv1 producción: `https://servicios1.afip.gov.ar/wsfev1/service.asmx`

**WSAA**
- TRA con `uniqueId`, `generationTime` = ahora − 10 min, `expirationTime` = ahora + ~10 h, `service` = `wsfe`.
- El Ticket de Acceso (token + sign) dura ~12 h. **Cachearlo en disco** por (certificado, servicio, ambiente): ARCA rechaza pedir otro mientras haya uno válido.
- El certificado es del **operador**; en `FEAuthRequest` se manda `Cuit` = CUIT **representado** (perfil). Así funciona la delegación.

**WSFEv1**
- Métodos mínimos: `FEDummy`, `FECompUltimoAutorizado`, `FECAESolicitar`, `FECompConsultar`, `FEParamGetPtosVenta`, `FEParamGetTiposCbte`, `FEParamGetTiposDoc`, `FEParamGetTiposIva`, `FEParamGetCondicionIvaReceptor`.
- Tipos de comprobante: 1 = Factura A, 6 = Factura B, 11 = Factura C; 3/8/13 = Nota de Crédito A/B/C (incluir también las NC desde el inicio, es la única forma de "anular").
- Concepto: 1 = Productos, 2 = Servicios, 3 = Productos y Servicios. Si es 2 o 3 son **obligatorios** `FchServDesde`, `FchServHasta` y `FchVtoPago`.
- Documento del receptor: 80 = CUIT, 96 = DNI, 99 = Consumidor Final sin identificar (`DocNro` = 0). Validar el dígito verificador del CUIT localmente.
- **`CondicionIVAReceptorId` se manda siempre** (RG 5616). En la tabla del manual v4.7 figura como no obligatorio, pero la doc dice que "resultará obligatorio"; si falta, ARCA responde 10245/10246, y un valor inválido (10242/10243) rechaza el CAE. La fecha de corte 01/12/2026 no figura en el manual: no depender de ella. Aplica también a Factura C. Tomar los códigos de `FEParamGetCondicionIvaReceptor` (cacheados), no hardcodearlos. Valores típicos: Responsable Inscripto, Monotributo, Consumidor Final, Exento.
- **Factura C**: `ImpNeto` = total, `ImpIVA` = 0, sin array `Iva`. **Factura A/B**: desglose de IVA por alícuota (array `Iva` con `Id`, `BaseImp`, `Importe`). Calcular con `Decimal` y redondeo a 2 decimales; validar que los totales cierren.
- Moneda `PES`, cotización 1 (dejar preparado para moneda extranjera, sin implementarlo aún).
- Numeración: siempre `FECompUltimoAutorizado + 1`, pedido justo antes de emitir.
- **WSFE no recibe ítems.** Los ítems (cantidad, unidad, descripción, precio unitario, alícuota) se guardan localmente y aparecen **solo en el PDF**. El total enviado a ARCA tiene que ser igual a la suma de los ítems.
- **CAEA (contingencia)**: desde el 01/08/2026 el CAEA solo se permite como contingencia, y solo en domicilios con al menos un punto de venta CAE (o Controlador Fiscal) activo como modalidad principal. El plan de contingencia del POS (M7) tiene que respetar esto.
- El punto de venta tiene que ser de tipo Web Services (se crea en "ABM de puntos de venta"). Tiene **numeración propia** que arranca en 1. Listarlos con `FEParamGetPtosVenta` y avisar claramente si no hay ninguno.

**QR (RG 4892)**
- URL `https://www.arca.gob.ar/fe/qr/?p=<base64(JSON)>` (la especificación vigente usa `arca.gob.ar`; el ejemplo del PDF todavía muestra `afip.gob.ar`). `fecha` en formato `YYYY-MM-DD`, `importe` con hasta 2 decimales, `ctz` hasta 6. Sin logo de ARCA: la especificación no lo menciona. Campos: con `ver`, `fecha`, `cuit`, `ptoVta`, `tipoCmp`, `nroCmp`, `importe`, `moneda`, `ctz`, `tipoDocRec`, `nroDocRec`, `tipoCodAut` = "E", `codAut` = CAE.

**PDF**
- Formato estándar de comprobante argentino: letra (A/B/C) en recuadro central, código de comprobante, datos del emisor (razón social, CUIT, condición IVA, domicilio, IIBB, inicio de actividades — todo configurable por perfil), datos del receptor, período facturado si corresponde, tabla de ítems (cantidad, unidad, descripción, precio unitario, subtotal), totales, CAE y vencimiento del CAE, QR.
- Unidades de medida configurables (unidades, otras unidades, kg, docena, horas, etc.).

## 5. Seguridad y robustez (no negociables)

- **Nunca emitir sin confirmación.** El flujo es siempre *preparar* (borrador + preview, sin tocar ARCA salvo para consultar el próximo número) → *confirmar* (emisión real). Los borradores vencen (ej. 15 min).
- **Idempotencia**: toda emisión lleva una `idempotency_key` (la API la exige; MCP y UI la generan internamente). Si se corta la red después de mandar `FECAESolicitar`, **antes de reintentar** hay que consultar con `FECompUltimoAutorizado` / `FECompConsultar` si el comprobante ya quedó autorizado, para no emitir dos veces.
- **Preview antes de cada `FECAESolicitar`, también en homologación.** La CLI siempre muestra el preview y pide confirmación; los tests automatizados usan `--yes`.
- **Ambiente explícito**: homologación por defecto. Producción solo después de que el perfil haya hecho al menos una emisión de prueba OK y con un toggle explícito. Mostrar siempre el ambiente en UI, CLI y en las respuestas del MCP ("⚠️ PRODUCCIÓN").
- La API y la UI escuchan **solo en 127.0.0.1**. La API usa un token local (generado en la instalación) para que ninguna web del navegador pueda pegarle (cuidar CORS/CSRF).
- Logs sin token, sign ni clave privada. Errores de ARCA traducidos a mensajes claros en español (mantener un mapa de códigos de error frecuentes → explicación + acción sugerida).
- Tests: unitarios con respuestas SOAP grabadas (fixtures), más tests de integración contra homologación marcados aparte (`pytest -m homo`), que se corren solo si hay certificado de prueba configurado.

## 6. Interfaces

**Modelo de datos (mínimo)**
- `Credential`: certificado + clave (operador), CUIT del operador, ambiente.
- `Profile`: CUIT representado, razón social, `nombre_fantasia` (opcional, se imprime en el PDF), condición IVA del emisor, domicilio, punto de venta por defecto, datos para el PDF, credencial asociada.
- `Customer`: por perfil. Nombre, tipo/nro de documento, condición IVA del receptor, domicilio, email, defaults (concepto, ítem, unidad).
- `Draft` y `Voucher`: cabecera, ítems, totales, CAE, vencimiento del CAE, número, PDF, `idempotency_key`, estado.

**MCP tools** (nombres en español, descripciones claras para el modelo)
- `estado_arca` · `listar_perfiles` · `listar_clientes` · `guardar_cliente`
- `ultimo_comprobante(perfil, punto_venta, tipo)`
- `preparar_comprobante(perfil, cliente | receptor, tipo?, concepto, items[], periodo?, vto_pago?)` → devuelve el preview en texto + `borrador_id`. Infiere el tipo (C si el emisor es monotributista) y la condición IVA del cliente guardado.
- `emitir_comprobante(borrador_id, confirmo: true)` → CAE, número y ruta del PDF.
- `consultar_comprobante` · `reimprimir_pdf` · `nota_de_credito(comprobante_original, motivo)`
- `resumen_facturacion(perfil, desde, hasta)` (y, para monotributistas, total de los últimos 12 meses).
- Ejemplo que tiene que funcionar de punta a punta: *"Facturá a Acciona, CUIT 30-71058146-7, $123.456.789,87, 1 otras unidades, servicios de innovación, período septiembre 2026"*.

**API REST** (`/api/v1`, OpenAPI automático)
- `POST /vouchers` (con `Idempotency-Key`; modo `draft=true` para preview), `POST /vouchers/{draft_id}/confirm`, `GET /vouchers/{id}`, `GET /vouchers/{id}/pdf`, `GET /profiles`, `GET/POST /customers`, `GET /health`.
- Caso Postaberry: `POST /vouchers` con `confirm=true` directo (el POS ya confirmó en caja), Consumidor Final, ítems `[{qty: 1, unit: "docena", desc: "Facturas", unit_price: ...}, {qty: 0.5, unit: "kg", desc: "Pan", unit_price: ...}]`.

**CLI**
- `facturitas init` (asistente: genera clave + CSR y explica paso a paso qué hacer en ARCA). **Nota:** en M4 `facturitas init` reemplaza a `tools/gen_csr.py`; hasta entonces el script sigue siendo el generador de CSR. En M1 `init` solo registra credencial y perfil, `facturitas cert import`, `facturitas perfil ...`, `facturitas cliente ...`, `facturitas emitir ...` (con preview y confirmación interactiva, `--yes` para scripts), `facturitas lote archivo.xlsx`, `facturitas ultimo`, `facturitas estado`.

**Web UI** (prioridad: que la entienda Miguel)
- Primer arranque: asistente en pasos con capturas/explicaciones de cada pantalla de ARCA: (1) generar clave y CSR con un botón, (2) subir el CSR en "Administración de Certificados Digitales" y pegar o subir el `.crt`, (3) asociar el servicio de facturación electrónica en "Administrador de Relaciones", (4) crear el punto de venta Web Services, (5) emisión de prueba en homologación. Validar cada paso contra ARCA cuando se pueda (ej. `FEParamGetPtosVenta`).
- Pantalla "Nueva factura" con clientes guardados y "repetir la del mes pasado".
- Historial con búsqueda, descarga de PDF y nota de crédito.
- Botón **"Conectar con mi IA"**: escribe o actualiza la entrada del MCP en el config de Claude Desktop (y muestra el snippet para otros clientes), con backup previo del archivo.

## 7. Empaquetado y distribución (GitHub Releases)

No hace falta tener cada SO: se compila en **GitHub Actions** con una matriz `windows-latest`, `ubuntu-latest` y `macos-14` (arm64). PyInstaller no cross-compila, por eso se usa un runner por SO.
- **Windows**: PyInstaller (`--onedir`) + **Inno Setup** → `Facturitas-Setup-x.y.z.exe`. Instala por usuario (sin admin), crea accesos directos e ícono en la bandeja (`pystray`) que abre la UI. Sin firma de código, SmartScreen va a advertir: documentarlo en el README y evaluar más adelante firma gratuita para OSS (ej. SignPath).
- **Linux**: AppImage (y opcionalmente `.deb`).
- **macOS**: `.app` dentro de un `.dmg` para arm64, sin firmar. Documentar cómo abrirlo pese a Gatekeeper (clic derecho → Abrir). Firma y notarización quedan para después (requiere cuenta Apple Developer paga).
- **Para devs**: `uv tool install facturitas` / `pipx install facturitas` (publicar en PyPI).
- **Claude Desktop**: evaluar empaquetar también como bundle de extensión MCP de un clic (verificar el formato vigente en la documentación de Anthropic).
- Smoke test en CI por SO: ejecutar el binario empaquetado con `--version` y `estado --offline`.
- Releases automáticos al pushear un tag `v*`, con checksums SHA256.

## 8. Estructura del repo sugerida

```
facturitas/
  src/facturitas/
    arca/        # wsaa.py, wsfe.py, wsdl/ (embebidos), errors.py, qr.py
    core/        # models.py, service.py (preparar/emitir/idempotencia), money.py, cuit.py
    storage/     # sqlite, migraciones, secretos
    pdf/         # render.py, templates
    api/         # FastAPI app + routers
    web/         # templates jinja + static
    mcp/         # server.py
    cli/         # typer app
  tests/         # unit + fixtures SOAP; integración marcada "homo"
  packaging/     # pyinstaller spec, inno setup .iss, appimage, dmg
  .github/workflows/  # ci.yml, release.yml
  docs/          # guía de certificado paso a paso con capturas, delegación, FAQ
  LICENSE (MIT), README.md (español), CONTRIBUTING.md, SECURITY.md
```

## 9. Hitos (hacerlos en orden, cada uno con su PR y criterio de aceptación)

- **M0 · Esqueleto**: repo, `uv`, lint (ruff), tipos (mypy o pyright), pytest, CI básico en los 3 SO, licencia MIT y README inicial.
  *Acepta:* CI verde en Windows, Linux y macOS.
- **M1 · Motor ARCA en homologación**: WSAA con caché de TA, `FEDummy`, `FECompUltimoAutorizado`, `FEParamGet*`, `FECAESolicitar` para Factura C (servicios y productos) con `CondicionIVAReceptorId`, más CLI mínima (`init`, `estado`, `ultimo`, `emitir`).
  *Acepta:* emitir una Factura C real en homologación desde la CLI y obtener CAE.
- **M2 · Comprobante completo**: ítems con unidades, PDF con QR, SQLite con historial, perfiles y clientes, delegación (credencial ≠ CUIT del perfil), idempotencia con reconciliación, Nota de Crédito C, errores en español.
  *Acepta:* casos de Matías (servicios con período) y panadería (productos con kg y docena) generan PDF correcto; un corte simulado de red no duplica el comprobante.
- **M3 · MCP**: tools de la sección 6 con el flujo preparar/confirmar. Instrucciones de conexión para Claude Desktop, Claude Code y Gemini CLI.
  *Acepta:* el prompt de ejemplo de Acciona funciona de punta a punta desde Claude Desktop en homologación.
- **M4 · Web UI + asistente de certificado + botón "Conectar con mi IA"**.
  *Acepta:* una persona no técnica completa el setup en homologación siguiendo solo la UI.
- **M5 · API REST + Factura A/B con IVA** (para Postaberry), token local, OpenAPI y ejemplos de `curl`. Incluye **Nota de Crédito parcial** (en M2 la NC es solo total).
  *Acepta:* un script que simula el POS emite 50 tickets seguidos sin duplicados ni errores.
- **M6 · Instaladores**: workflow de release con `.exe` (Inno Setup), AppImage y `.dmg`.
  *Acepta:* instalar desde el Release en una Windows limpia y emitir en homologación sin tener Python instalado.
- **M7 · Extras**: lote desde Excel/CSV (caso Miguel), "repetir factura del mes pasado", resumen contra topes de monotributo (topes configurables, no hardcodeados), recordatorio mensual (tarea programada del SO opcional) y, más adelante, contingencia con CAEA para el POS.

## 10. Cómo trabajar en este repo

- **Nunca** apuntar a producción de ARCA desde tests, scripts ni ejemplos. Producción solo la habilita el usuario a mano.
- Antes de implementar algo de ARCA, citar en el PR la sección del manual oficial en la que te basaste. Si la doc y este archivo se contradicen, gana la doc: actualizá este archivo y avisá.
- Commits chicos y descriptivos. Cada hito termina con README actualizado.
- Si algo requiere una decisión del usuario (licencia, nombre, firma de código, gastos), preguntar en vez de asumir.
