# Facturitas

Facturación electrónica de ARCA (ex AFIP), **fácil, local y abierta**. Emití comprobantes sin
entrar al portal: tus claves quedan en tu máquina y el código es MIT.

> ⚠️ **Estado: M0 (esqueleto).** Todavía no emite nada. El plan completo está en
> [CLAUDE.md](CLAUDE.md).

## Instalación para desarrollo

Requiere Python 3.12 y [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run facturitas --version
uv run pytest
```

## Puntos de venta: homologación vs. producción

- Para usar web services hace falta un punto de venta de tipo **"RECE para aplicativo y web
  services"**. Los puntos de venta de **"Comprobantes en línea"** (por ejemplo el 00001 que se
  usa desde el portal) **no sirven** para web services.
- En **homologación** (pruebas) los puntos de venta disponibles se listan con
  `FEParamGetPtosVenta`; Facturitas los va a mostrar en `facturitas estado`.
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
