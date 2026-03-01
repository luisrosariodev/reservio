# Reservio Deployment Checklist (Staging -> Production)

This checklist is designed for the current stack:
- Django 5 + Gunicorn
- PostgreSQL on Supabase
- Stripe (payments + Connect)
- SMTP/Resend for transactional emails

Use `.env.production.example` as base.

## 1) Pre-flight (local)

Run before any deploy:

```bash
./venv/bin/pip install -r requirements.txt
./venv/bin/python manage.py check
./venv/bin/python manage.py test booking -v 1
```

If tests fail, stop and fix before deploying.

## 2) Secrets and keys (mandatory)

Rotate any leaked keys before production:
- SMTP/Resend API key
- Stripe secret keys
- Supabase DB password
- `DJANGO_SECRET_KEY`

Never commit real secrets to git.

## 3) Environment variables (staging/prod)

Set in hosting provider:

- Django:
  - `DJANGO_DEBUG=False`
  - `DJANGO_SECRET_KEY=<long-random-secret>`
  - `DJANGO_ALLOWED_HOSTS=<your-domain>`
  - `DJANGO_CSRF_TRUSTED_ORIGINS=https://<your-domain>`
- Database:
  - `DATABASE_URL=postgresql://...` (Supabase)
  - `DB_CONN_MAX_AGE=60`
  - `DB_FALLBACK_TO_SQLITE_IF_PG_DRIVER_MISSING=False`
- Stripe:
  - `STRIPE_SECRET_KEY=...`
  - `STRIPE_PUBLISHABLE_KEY=...`
  - `STRIPE_WEBHOOK_SECRET=...`
- Email:
  - `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`
  - `EMAIL_HOST=smtp.resend.com`
  - `EMAIL_PORT=587`
  - `EMAIL_HOST_USER=resend`
  - `EMAIL_HOST_PASSWORD=...`
  - `EMAIL_USE_TLS=True`
  - `DEFAULT_FROM_EMAIL=...`
- App/brand:
  - `APP_BASE_URL=https://<your-domain>`
  - `SITE_FAVICON_URL=/media/favicon-256.png`
  - `EMAIL_LOGO_URL=/media/favicon-256.png`

## 4) Supabase connection rule

Prefer `Transaction pooler` URI for web apps:

```text
postgresql://postgres.<project-ref>:<password>@<region>.pooler.supabase.com:6543/postgres?sslmode=require
```

If connection fails with DNS errors:
- test from a different network (hotspot),
- disable VPN/private relay,
- re-copy URI directly from Supabase `Connect`.

## 5) Build and release

Use this order:

```bash
python manage.py check --deploy
python manage.py migrate
python manage.py collectstatic --noinput
```

Start command:

```bash
gunicorn reservio.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --timeout 120
```

## 6) Stripe production setup

In Stripe Dashboard:
- set webhook endpoint:
  - `https://<your-domain>/stripe/webhook/`
- configure required events used by your app
- paste webhook secret into `STRIPE_WEBHOOK_SECRET`

Validate:
- successful payment,
- cancellation/refund behavior,
- platform fee behavior.

## 7) Email production validation

Verify end-to-end:
- trainer verification email
- trainer welcome email
- booking confirmation email with PDF invoice attachment

Check that email logo renders in common clients (Gmail/Apple Mail/Outlook).

## 8) Smoke test (go/no-go)

Run this exact flow in staging and production:

1. Create trainer account.
2. Verify email.
3. Enter trainer portal and set availability.
4. Create client account/login.
5. Book session and pay with Stripe.
6. Confirm booking appears in trainer and client dashboards.
7. Confirm confirmation email + invoice PDF.
8. Cancel one reservation and verify status + notification.

No-go if any payment, email, or auth step fails.

## 9) Rollback plan

Keep previous release available.

Rollback triggers:
- payment failures
- migration errors
- login/auth failures
- critical booking regressions

Rollback actions:
1. Revert app release.
2. Re-check DB schema compatibility.
3. Disable new traffic until smoke tests pass.

## 10) Post-launch observability

Minimum monitoring:
- app uptime and `/healthz/`
- 5xx error rate
- Stripe webhook failures
- email send failures

Keep daily DB backups active in Supabase.
