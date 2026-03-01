# Reserv.io UI Style Guide

## Tokens Base
- `--bg`, `--bg2`: fondos de página y superficies suaves.
- `--card`, `--border`: tarjetas y contornos.
- `--text`, `--muted`: jerarquía tipográfica.
- `--accent`, `--accentHover`, `--accentSoft`: color principal de marca.
- `--fieldBg`, `--fieldBorder`, `--fieldShadow`: estilo uniforme de campos.

## Campos
Todos los inputs/selects/textarea deben usar el patrón global en `booking/static/css/components.css`.

Regla:
- No definir colores de fields inline en templates.
- Si una página necesita ajuste, hacerlo en CSS de página usando tokens.

## Botones
- Primario: `.btn.btn-primary`
- Secundario: `.btn.btn-secondary`
- Estados de envío: clase `.is-busy` + `data-submit-lock` en forms.

## Auth Pages
Todas las pantallas auth usan `booking/static/css/pages/auth.css` y el layout:
- `.rio-auth-card`
- `.rio-auth-title`
- `.rio-auth-subtitle`
- `.rio-form`
- `.rio-field`
- `.rio-actions`

## Caching de estáticos
- En producción (`DEBUG=False`) se usa `ManifestStaticFilesStorage` para versionado automático de archivos estáticos.
- En desarrollo (`DEBUG=True`) se usa `StaticFilesStorage` para iteración rápida.
