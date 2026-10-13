---
name: facturacion-mensual
description: Rutina de facturación mensual con Factumono (ARCA/AFIP). Usala cuando el usuario pida facturar, hacer "la de siempre", anular una factura o saber cuánto facturó, usando las tools del MCP de Factumono.
---

# Facturación mensual con Factumono

Esta skill guía la rutina de facturar con las tools del MCP `factumono`. Todo comprobante pasa
por **preparar → mostrar el preview → confirmar**. Nada se emite sin que el usuario lo apruebe.

## Reglas que no se saltean

1. **Nunca llames a `emitir_comprobante` con `confirmo=true` sin haberle mostrado al usuario el
   preview completo de ESE borrador y sin que haya dicho explícitamente que sí.** Un pedido como
   "facturale a Acciona" no es una confirmación: es el pedido de preparar el borrador.
2. Mostrá el preview **tal cual lo devuelve la tool** (cliente, período, ítem, importe, ambiente) y
   preguntá: "¿Lo emito?". Esperá la respuesta.
3. Aclará siempre el ambiente: 🧪 **HOMOLOGACIÓN** = pruebas sin validez fiscal; ⚠️ **PRODUCCIÓN**
   = comprobantes reales. Si es producción, extremá el cuidado con el importe y el período.
4. Si el preview trae un aviso de **ya emitiste algo parecido**, mencionáselo al usuario: es solo
   informativo y no pide confirmación extra. Si avisa que la factura a anular **ya fue
   anulada**, leéselo y pasá `confirmo_duplicado=true` solo si insiste en seguir.
5. Los borradores vencen a los 15 minutos: si venció, preparalo de nuevo (no lo reutilices a ciegas).
6. No inventes datos. Si falta el importe, el cliente o el período, preguntá.

## Rutina mensual

1. **¿A quién?** Si no lo dijo, `listar_clientes` y preguntá. Los clientes guardados traen sus
   defaults (descripción, unidad, concepto, días de vencimiento).
2. **¿Cuánto?** El importe cambia cada mes: preguntalo siempre (a menos que pida "la de
   siempre", que repite el importe anterior). Se escribe como lo diga el usuario
   (`1.093.843,20`).
3. **¿Qué período?** Para servicios: el mes que corresponde ("por septiembre" → `periodo="septiembre"`).
   Si no lo dijo, preguntá; el valor por defecto es el mes en curso.
4. **Preparar:**
   - Cliente guardado + importe: `preparar_comprobante(cliente="acciona", importe="...", periodo="septiembre")`
   - "La de siempre": `preparar_comprobante(cliente="seba", repetir_ultima=true)` repite la
     última factura con el período corrido al mes siguiente; decile al usuario qué período quedó.
5. **Mostrar el preview y preguntar.** Recién con un "sí" explícito: `emitir_comprobante(borrador_id=..., confirmo=true)`.
6. **Devolver el resultado:** número, CAE y la ruta del PDF.

## Período y concepto

- `concepto`: **1** = productos, **2** = servicios, **3** = productos y servicios. El 2 y el 3
  llevan período y vencimiento.
- Cada cliente guarda su **modo de período**: `mes` (mes completo), `dia` (día de emisión),
  `rango` (desde-hasta, hay que indicarlo con `periodo="03/09/2026 al 17/09/2026"`) o `sin` (sin
  período: solo productos). «La de siempre» lo respeta. Si el cliente está en `rango`, preguntá las
  fechas. Podés forzar otro modo con `modo_periodo`.

## Importar facturas anteriores

- «Importá esta factura» / «cargá los clientes de estas facturas» → `importar_factura(ruta)`:
  lee el PDF de ARCA (o de Factumono, una carpeta, o el Excel/CSV de «Mis Comprobantes») de forma
  determinística, valida el QR y **prepara** qué se guardaría. Mostrale el preview completo y,
  solo con un «sí» explícito, `confirmar_importacion(importacion_id=..., confirmo=true)`.
- Si devuelve **«No reconocido»** con el texto crudo (no es de ARCA o es una imagen): interpretalo
  vos y cargalo con `guardar_plantilla(datos)` (nombre, CUIT o DNI, condición IVA, domicilio,
  concepto, ítems). **No inventes datos** que no estén en el texto. Es el mismo paso doble: preview
  y confirmación.
- Cuando un cliente tiene plantilla, `preparar_comprobante(cliente="acme")` sin importe usa sus ítems.

## Otras consultas

- "¿Cuánto facturé este año?" → `resumen_facturacion` (sin fechas = lo que va del año; para
  monotributistas suma también los últimos 12 meses). Aclará si son datos de homologación.
- "Mostrame las últimas 5 facturas" → `listar_comprobantes(limite=5)`.
- "Anulá la factura N" → `nota_de_credito(comprobante_original=N, motivo="...")`: prepara la nota
  de crédito (solo total). Pedí el motivo si no lo dio, mostrá el preview y confirmá igual que
  una factura.
- Reimprimir un PDF → `reimprimir_pdf(numero=N)`.
- Si algo falla o no hay conexión → `estado_arca`. Si se cortó la comunicación al emitir, **no
  reintentes a ciegas**: la herramienta consulta a ARCA antes y no duplica el comprobante.

## Ejemplos

- *"Facturale a Acciona 1.093.843,20 por septiembre"* → preparar con `cliente="acciona"`,
  `importe="1.093.843,20"`, `periodo="septiembre"` → mostrar preview → preguntar → emitir.
- *"Hacele la de siempre a Seba"* → preparar con `cliente="seba"`, `repetir_ultima=true` →
  mostrar preview (importe y período) → preguntar → emitir.
