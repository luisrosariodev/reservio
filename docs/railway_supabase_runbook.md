# Railway + Supabase Deploy Runbook (Reservio)

## 1) Create Railway service

1. Create project in Railway.
2. Add service from GitHub repo (this project).
3. Keep default build system (Nixpacks).

## 2) Configure start command

Railway should detect `Procfile` automatically:

```text
web: gunicorn reservio.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --timeout 120
```

## 3) Set environment variables

Copy from `.env.production.example` and set real values in Railway:

- `DJANGO_DEBUG=False`
- `DJANGO_SECRET_KEY=<long-random-secret>`
- `DJANGO_ALLOWED_HOSTS=<railway-domain>,<custom-domain>`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://<railway-domain>,https://<custom-domain>`
- `DATABASE_URL=<supabase-postgres-uri-with-sslmode=require>`
- `DB_CONN_MAX_AGE=60`
- `DB_FALLBACK_TO_SQLITE_IF_PG_DRIVER_MISSING=False`
- `STRIPE_SECRET_KEY=...`
- `STRIPE_PUBLISHABLE_KEY=...`
- `STRIPE_WEBHOOK_SECRET=...`
- `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`
- `EMAIL_HOST=smtp.resend.com`
- `EMAIL_PORT=587`
- `EMAIL_HOST_USER=resend`
- `EMAIL_HOST_PASSWORD=<resend-api-key>`
- `EMAIL_USE_TLS=True`
- `DEFAULT_FROM_EMAIL=Reservio <no-reply@yourdomain.com>`
- `APP_BASE_URL=https://<your-domain>`
- `TWO_FA_METHOD=email`

## 4) First release commands

After first deploy is healthy, run in Railway shell:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

## 5) Stripe webhook

In Stripe Dashboard:

- Endpoint: `https://<your-domain>/stripe/webhook/`
- Subscribe to required events for your app
- Copy signing secret to `STRIPE_WEBHOOK_SECRET`

## 6) Smoke test

1. Login admin (`/admin/`)
2. Trainer signup + email verify
3. Client signup/login
4. Booking + payment
5. Booking confirmation email received
6. Cancel/refund flow works

## 7) If deploy fails

- Check Railway logs.
- Run:
  - `python manage.py check --deploy`
  - `python manage.py showmigrations`
- Validate `DATABASE_URL` and `DJANGO_ALLOWED_HOSTS`.
