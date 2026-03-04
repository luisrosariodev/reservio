from datetime import datetime, timedelta
from decimal import Decimal
import logging
import csv
import os
import time
import threading
import base64
from email.mime.image import MIMEImage

import requests
import stripe

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.utils import IntegrityError
from django.db.models import Count, F, Q, Min, Max
from django.core.exceptions import MultipleObjectsReturned
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.http import HttpResponseBadRequest
from django.http import HttpResponse
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST

from .models import Checkout, Client, ClientDependent, ClientProfile, Reservation, StripeRefundEvent, StripeWebhookEvent, TimeSlot, Trainer, TrainerAvailability, UserTwoFactorAuth

from .forms import ClientRoleActivationForm, TrainerRoleActivationForm, TrainerSettingsForm

# Edición de disponibilidad (formset inline)
# Nota: el formset debe estar definido en forms.py.
from .forms import TrainerAvailabilityFormSet

#
# Importa services como módulo para evitar ImportError durante recargas si services.py cambia.

from . import services as services

# ---- Helpers de llamadas seguras a services (evitan fallos duros en refactors) ----

def _services_has(name: str) -> bool:
    return hasattr(services, name) and callable(getattr(services, name))


def _maybe_sync_timeslots_for_week(*, trainer, week_start):
    if _services_has("sync_timeslots_for_week"):
        services.sync_timeslots_for_week(trainer=trainer, week_start=week_start)


def _available_timeslots_queryset(*, trainer, week_start):
    # Prefiere la implementación de services; fallback a consulta directa en DB.
    if _services_has("available_timeslots_for_week"):
        return services.available_timeslots_for_week(trainer=trainer, week_start=week_start)
    # Fallback: todos los timeslots activos del entrenador para esa semana.
    end_date = week_start + timedelta(days=6)
    return TimeSlot.objects.filter(trainer=trainer, active=True, date__gte=week_start, date__lte=end_date)

from django.core.exceptions import ValidationError


from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.views import LoginView
from django.contrib.auth.views import PasswordResetView, PasswordResetDoneView, PasswordResetConfirmView, PasswordResetCompleteView


PORTAL_TABS = {"agenda", "availability", "profile", "payments", "clients"}
EMAIL_VERIFY_SALT = "trainer-email-verify-v1"
CLIENT_EMAIL_VERIFY_SALT = "client-email-verify-v1"
logger = logging.getLogger(__name__)
ROLE_TRAINER = "trainer"
ROLE_CLIENT = "client"
ROLE_SESSION_KEY = "account_active_role"
TWO_FA_PENDING_USER_KEY = "two_fa_pending_user_id"
TWO_FA_PENDING_NEXT_KEY = "two_fa_pending_next"
TWO_FA_CODE_HASH_KEY = "two_fa_email_code_hash"
TWO_FA_CODE_EXPIRES_KEY = "two_fa_email_code_expires_at"
TWO_FA_CODE_RESEND_AT_KEY = "two_fa_email_code_resend_at"


def _client_ip(request):
    xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip() or "unknown"


def _trainer_clients_queryset(*, trainer, q: str = "", segment: str = "all", sort: str = "name_asc", today=None):
    if today is None:
        today = timezone.localdate()

    qs = (
        Client.objects.filter(trainer=trainer)
        .annotate(
            total_reservations=Count("reservations", distinct=True),
            upcoming_reservations=Count(
                "reservations",
                filter=Q(reservations__timeslot__date__gte=today),
                distinct=True,
            ),
            pending_manual_reservations=Count(
                "reservations",
                filter=Q(
                    reservations__payment_method=Reservation.PAYMENT_ATH,
                    reservations__paid=False,
                ),
                distinct=True,
            ),
            next_session_date=Min(
                "reservations__timeslot__date",
                filter=Q(reservations__timeslot__date__gte=today),
            ),
            next_session_time=Min(
                "reservations__timeslot__time",
                filter=Q(reservations__timeslot__date__gte=today),
            ),
            last_session_date=Max("reservations__timeslot__date"),
            first_session_date=Min("reservations__timeslot__date"),
        )
    )

    q = (q or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(email__icontains=q)
            | Q(phone__icontains=q)
        )

    segment = (segment or "all").strip().lower()
    if segment not in {"all", "active", "new", "pending"}:
        segment = "all"
    if segment == "active":
        qs = qs.filter(upcoming_reservations__gt=0)
    elif segment == "new":
        cutoff = today - timedelta(days=30)
        qs = qs.filter(first_session_date__gte=cutoff)
    elif segment == "pending":
        qs = qs.filter(pending_manual_reservations__gt=0)

    sort = (sort or "name_asc").strip().lower()
    if sort == "name_desc":
        qs = qs.order_by("-name", "email")
    elif sort == "next_session":
        qs = qs.order_by(F("next_session_date").asc(nulls_last=True), F("next_session_time").asc(nulls_last=True), "name")
    elif sort == "last_session":
        qs = qs.order_by(F("last_session_date").desc(nulls_last=True), "name")
    elif sort == "reservations_desc":
        qs = qs.order_by("-total_reservations", "name")
    else:
        sort = "name_asc"
        qs = qs.order_by("name", "email")

    return qs, segment, sort


def _rate_limiter_keys(prefix: str, *, request, identifier: str = ""):
    keys = [f"rl:{prefix}:ip:{_client_ip(request)}"]
    token = (identifier or "").strip().lower()
    if token:
        keys.append(f"rl:{prefix}:id:{token}")
    return keys


def _rate_limiter_is_blocked(keys, *, max_attempts: int):
    for key in keys:
        if int(cache.get(key, 0) or 0) >= int(max_attempts):
            return True
    return False


def _rate_limiter_hit(keys, *, window_seconds: int):
    timeout = max(int(window_seconds), 60)
    for key in keys:
        current = int(cache.get(key, 0) or 0) + 1
        cache.set(key, current, timeout=timeout)


def _rate_limiter_clear(keys):
    for key in keys:
        cache.delete(key)


def _resolve_account_roles(user):
    trainer = _get_trainer_for_user(user)
    has_trainer = trainer not in (None, "__MULTIPLE__")
    has_client = ClientProfile.objects.filter(user=user, active=True).exists()
    return has_trainer, has_client


def _get_client_profile_for_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return ClientProfile.objects.filter(user=user, active=True).first()


def healthz_view(request):
    return JsonResponse({"ok": True, "service": "reservio"}, status=200)


def _redirect_for_role(role):
    if role == ROLE_TRAINER:
        return redirect("booking:trainer_portal")
    if role == ROLE_CLIENT:
        return redirect("booking:client_portal_dashboard")
    return redirect("booking:account_role_management")


def _set_active_role_session(request, role):
    if role in {ROLE_TRAINER, ROLE_CLIENT}:
        request.session[ROLE_SESSION_KEY] = role


def _post_login_redirect_url(request, user, next_url: str = ""):
    trainer = _get_trainer_for_user(user)
    client_profile = _get_client_profile_for_user(user)
    redirect_url = (next_url or "").strip()
    if redirect_url:
        trainer_portal_path = reverse("booking:trainer_portal")
        client_dashboard_path = reverse("booking:client_portal_dashboard")
        if redirect_url.startswith(client_dashboard_path):
            _set_active_role_session(request, ROLE_CLIENT)
        elif redirect_url.startswith(trainer_portal_path):
            _set_active_role_session(request, ROLE_TRAINER)
        if trainer not in (None, "__MULTIPLE__") and _email_verification_is_required() and not trainer.email_verified:
            if redirect_url.startswith(client_dashboard_path):
                if client_profile and _client_email_verification_is_required() and not client_profile.email_verified:
                    return f"{reverse('booking:client_verify_pending')}?email={user.email}"
                return redirect_url
            return f"{reverse('booking:trainer_verify_pending')}?email={user.email}"
        if client_profile and _client_email_verification_is_required() and not client_profile.email_verified:
            if redirect_url.startswith(client_dashboard_path):
                return f"{reverse('booking:client_verify_pending')}?email={user.email}"
        return redirect_url

    has_trainer, has_client = _resolve_account_roles(user)
    # Email verification gate applies to trainer portal access, not to client-only navigation.
    if (
        trainer not in (None, "__MULTIPLE__")
        and _email_verification_is_required()
        and not trainer.email_verified
        and has_trainer
        and not has_client
    ):
        return f"{reverse('booking:trainer_verify_pending')}?email={user.email}"

    if has_trainer and has_client:
        return reverse("booking:account_mode_select")
    if has_trainer:
        _set_active_role_session(request, ROLE_TRAINER)
        return reverse("booking:trainer_portal")
    if has_client:
        if client_profile and _client_email_verification_is_required() and not client_profile.email_verified:
            return f"{reverse('booking:client_verify_pending')}?email={user.email}"
        _set_active_role_session(request, ROLE_CLIENT)
        return reverse("booking:client_portal_dashboard")
    return reverse("booking:account_role_management")


def _get_or_create_user_2fa(user):
    obj, _ = UserTwoFactorAuth.objects.get_or_create(user=user)
    return obj


def _two_fa_method() -> str:
    method = (getattr(settings, "TWO_FA_METHOD", "email") or "email").strip().lower()
    if method not in {"off", "email"}:
        return "email"
    return method


def _is_two_fa_globally_enabled() -> bool:
    return _two_fa_method() != "off"


def _generate_two_fa_email_code() -> str:
    return "".join(get_random_string(1, allowed_chars="0123456789") for _ in range(6))


def _send_two_fa_email_code(user, code: str):
    to_email = (user.email or "").strip()
    if not to_email:
        raise ValueError("User has no email for 2FA delivery")
    _send_templated_email(
        subject="Tu código de acceso de Reserv.io",
        to=[to_email],
        text_template="emails/two_factor_code.txt",
        html_template="emails/two_factor_code.html",
        context={
            "code": code,
            "user_email": (user.email or "").strip(),
            "expires_minutes": max(1, int(getattr(settings, "TWO_FA_EMAIL_CODE_TTL_SECONDS", 600) // 60)),
        },
    )


def _issue_two_fa_code(request, user):
    now_ts = int(time.time())
    expires_in = int(getattr(settings, "TWO_FA_EMAIL_CODE_TTL_SECONDS", 600) or 600)
    resend_cooldown = int(getattr(settings, "TWO_FA_EMAIL_RESEND_COOLDOWN_SECONDS", 45) or 45)
    code = _generate_two_fa_email_code()
    request.session[TWO_FA_CODE_HASH_KEY] = make_password(code)
    request.session[TWO_FA_CODE_EXPIRES_KEY] = now_ts + expires_in
    request.session[TWO_FA_CODE_RESEND_AT_KEY] = now_ts + resend_cooldown
    _send_two_fa_email_code(user, code)


def _portal_url(*, tab="availability", edit=False):
    safe_tab = (tab or "availability").strip().lower()
    if safe_tab not in PORTAL_TABS:
        safe_tab = "availability"

    url = f"{reverse('booking:trainer_portal')}?tab={safe_tab}"
    if edit:
        url += "&edit=1"
    return url


def _email_verification_is_required() -> bool:
    """Activa gate de verificación solo si también está activo el envío de emails."""
    require_verification = bool(getattr(settings, "TRAINER_REQUIRE_EMAIL_VERIFICATION", True))
    send_emails = bool(getattr(settings, "TRAINER_SEND_TRANSACTIONAL_EMAILS", True))
    if require_verification and not send_emails:
        logger.warning(
            "TRAINER_REQUIRE_EMAIL_VERIFICATION=True pero TRAINER_SEND_TRANSACTIONAL_EMAILS=False; "
            "se desactiva temporalmente el gate de verificación para evitar bloqueo de acceso."
        )
        return False
    return require_verification


def _client_email_verification_is_required() -> bool:
    require_verification = bool(getattr(settings, "CLIENT_REQUIRE_EMAIL_VERIFICATION", True))
    send_emails = bool(getattr(settings, "TRAINER_SEND_TRANSACTIONAL_EMAILS", True))
    if require_verification and not send_emails:
        logger.warning(
            "CLIENT_REQUIRE_EMAIL_VERIFICATION=True pero TRAINER_SEND_TRANSACTIONAL_EMAILS=False; "
            "se desactiva temporalmente el gate de verificación para evitar bloqueo de acceso."
        )
        return False
    return require_verification


def _build_trainer_verify_token(user):
    payload = {
        "uid": user.pk,
        "email": (user.email or "").strip().lower(),
    }
    return signing.dumps(payload, salt=EMAIL_VERIFY_SALT)


def _build_trainer_verify_link(request, user):
    token = _build_trainer_verify_token(user)
    verify_url = reverse("booking:trainer_verify_email")
    return request.build_absolute_uri(f"{verify_url}?token={token}")


def _build_client_verify_token(user):
    payload = {
        "uid": user.pk,
        "email": (user.email or "").strip().lower(),
    }
    return signing.dumps(payload, salt=CLIENT_EMAIL_VERIFY_SALT)


def _build_client_verify_link(request, user):
    token = _build_client_verify_token(user)
    verify_url = reverse("booking:client_verify_email")
    return request.build_absolute_uri(f"{verify_url}?token={token}")


def _send_templated_email(*, subject, to, text_template, html_template, context, attachments=None):
    if not to:
        return

    brand_name = (getattr(settings, "EMAIL_BRAND_NAME", "") or "").strip() or "Reserv.io"
    app_base_url = (getattr(settings, "APP_BASE_URL", "") or "").strip().rstrip("/")
    brand_url = (getattr(settings, "EMAIL_BRAND_URL", "") or "").strip() or app_base_url
    logo_url_raw = (getattr(settings, "EMAIL_LOGO_URL", "") or "").strip()
    logo_file_raw = (getattr(settings, "EMAIL_LOGO_FILE", "") or "").strip()
    site_favicon_url = (getattr(settings, "SITE_FAVICON_URL", "") or "").strip() or "/static/img/favicon-256.png"
    if logo_url_raw.startswith("http://") or logo_url_raw.startswith("https://"):
        logo_url = logo_url_raw
    elif logo_url_raw.startswith("/") and app_base_url:
        logo_url = f"{app_base_url}{logo_url_raw}"
    elif logo_url_raw:
        logo_url = logo_url_raw
    elif site_favicon_url.startswith("/") and app_base_url:
        logo_url = f"{app_base_url}{site_favicon_url}"
    elif site_favicon_url.startswith("http://") or site_favicon_url.startswith("https://"):
        logo_url = site_favicon_url
    elif app_base_url:
        logo_url = f"{app_base_url}/static/img/favicon-256.png"
    else:
        logo_url = ""
    support_email = (getattr(settings, "EMAIL_SUPPORT_EMAIL", "") or "").strip()
    footer_note = (getattr(settings, "EMAIL_FOOTER_NOTE", "") or "").strip() or "Este correo fue generado automáticamente por Reserv.io."
    legal_name = (getattr(settings, "EMAIL_LEGAL_NAME", "") or "").strip() or brand_name
    legal_address = (getattr(settings, "EMAIL_LEGAL_ADDRESS", "") or "").strip()

    merged_context = {
        "email_brand_name": brand_name,
        "email_brand_url": brand_url,
        "email_logo_url": logo_url,
        "email_logo_cid": "",
        "email_support_email": support_email,
        "email_footer_note": footer_note,
        "email_legal_name": legal_name,
        "email_legal_address": legal_address,
        "email_year": timezone.localtime(timezone.now()).year,
        **(context or {}),
    }

    use_resend_api = bool((getattr(settings, "RESEND_API_KEY", "") or "").strip())
    text_body = render_to_string(text_template, merged_context)
    logo_cid = ""
    logo_part = None
    if not use_resend_api:
        # Inline logo for better compatibility with SMTP clients.
        logo_candidate_paths = []
        if logo_file_raw:
            if os.path.isabs(logo_file_raw):
                logo_candidate_paths.append(logo_file_raw)
            else:
                logo_candidate_paths.append(os.path.join(settings.BASE_DIR, logo_file_raw))
        if site_favicon_url.startswith("/media/"):
            logo_candidate_paths.append(os.path.join(settings.MEDIA_ROOT, site_favicon_url.removeprefix("/media/")))
        logo_candidate_paths.append(os.path.join(settings.MEDIA_ROOT, "favicon-256.png"))

        for candidate in logo_candidate_paths:
            try:
                if candidate and os.path.exists(candidate):
                    with open(candidate, "rb") as fh:
                        logo_part = MIMEImage(fh.read())
                    logo_cid = "reservio-logo"
                    logo_part.add_header("Content-ID", f"<{logo_cid}>")
                    logo_part.add_header("Content-Disposition", "inline", filename=os.path.basename(candidate))
                    break
            except Exception:
                continue

    merged_context["email_logo_cid"] = logo_cid
    html_body = render_to_string(html_template, merged_context)

    if use_resend_api:
        resend_key = (getattr(settings, "RESEND_API_KEY", "") or "").strip()
        resend_url = (getattr(settings, "RESEND_API_URL", "https://api.resend.com/emails") or "").strip()
        timeout = int(getattr(settings, "EMAIL_TIMEOUT", 10) or 10)

        resend_attachments = []
        for attachment in attachments or []:
            filename, content, _mimetype = attachment
            if isinstance(content, str):
                content_bytes = content.encode("utf-8")
            else:
                content_bytes = content
            resend_attachments.append(
                {
                    "filename": filename,
                    "content": base64.b64encode(content_bytes).decode("ascii"),
                }
            )

        payload = {
            "from": getattr(settings, "DEFAULT_FROM_EMAIL", None),
            "to": to,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        if resend_attachments:
            payload["attachments"] = resend_attachments

        response = requests.post(
            resend_url,
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Resend API error {response.status_code}: {response.text[:300]}")
        return

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=to,
    )
    email.attach_alternative(html_body, "text/html")
    if logo_part is not None:
        email.mixed_subtype = "related"
        email.attach(logo_part)
    for attachment in attachments or []:
        filename, content, mimetype = attachment
        email.attach(filename, content, mimetype)
    email.send(fail_silently=False)


def _send_trainer_verification_email(request, user, trainer):
    if not getattr(settings, "TRAINER_SEND_TRANSACTIONAL_EMAILS", True):
        return
    verify_link = _build_trainer_verify_link(request, user)
    _send_templated_email(
        subject="Confirma tu correo en Reserv.io",
        to=[user.email],
        text_template="emails/trainer_verification.txt",
        html_template="emails/trainer_verification.html",
        context={
            "trainer_name": trainer.business_name,
            "verify_link": verify_link,
        },
    )


def _send_client_verification_email(request, user, profile):
    if not getattr(settings, "TRAINER_SEND_TRANSACTIONAL_EMAILS", True):
        return
    verify_link = _build_client_verify_link(request, user)
    _send_templated_email(
        subject="Confirma tu correo en Reserv.io",
        to=[user.email],
        text_template="emails/client_verification.txt",
        html_template="emails/client_verification.html",
        context={
            "client_name": (profile.full_name or user.email or "cliente"),
            "verify_link": verify_link,
        },
    )


def _send_trainer_welcome_email(user, trainer):
    if not getattr(settings, "TRAINER_SEND_TRANSACTIONAL_EMAILS", True):
        return
    app_base_url = (getattr(settings, "APP_BASE_URL", "") or "").strip().rstrip("/")
    portal_path = reverse("booking:trainer_portal")
    portal_url = f"{app_base_url}{portal_path}" if app_base_url else portal_path
    _send_templated_email(
        subject="Tu cuenta de entrenador ya está activa",
        to=[user.email],
        text_template="emails/trainer_welcome.txt",
        html_template="emails/trainer_welcome.html",
        context={
            "trainer_name": trainer.business_name,
            "portal_url": portal_url,
        },
    )


def _send_client_welcome_email(user, profile):
    if not getattr(settings, "TRAINER_SEND_TRANSACTIONAL_EMAILS", True):
        return
    app_base_url = (getattr(settings, "APP_BASE_URL", "") or "").strip().rstrip("/")
    portal_path = reverse("booking:client_portal_dashboard")
    portal_url = f"{app_base_url}{portal_path}" if app_base_url else portal_path
    _send_templated_email(
        subject="Tu cuenta de cliente ya está activa",
        to=[user.email],
        text_template="emails/client_welcome.txt",
        html_template="emails/client_welcome.html",
        context={
            "client_name": (profile.full_name or user.email or "cliente"),
            "portal_url": portal_url,
        },
    )


def _money_fmt(value, currency="USD"):
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except Exception:
        amount = Decimal("0.00")
    return f"{amount} {currency}"


def _pdf_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_invoice_pdf_bytes(*, checkout, reservations):
    """Build a minimal PDF invoice without external dependencies."""
    currency = (checkout.currency or "USD").upper()
    trainer_name = getattr(checkout.trainer, "business_name", "Trainer")
    client_name = getattr(checkout.client, "name", "")
    client_email = getattr(checkout.client, "email", "")

    lines = [
        "Reserv.io - Invoice",
        f"Invoice: {str(checkout.id)[:8].upper()}",
        f"Date: {timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Trainer: {trainer_name}",
        f"Client: {client_name}",
        f"Email: {client_email}",
        f"Payment method: {checkout.payment_method}",
        f"Checkout status: {checkout.status}",
        "",
        "Sessions:",
    ]
    for idx, r in enumerate(reservations, start=1):
        slot = getattr(r, "timeslot", None)
        if slot:
            when = f"{slot.date} {slot.time.strftime('%I:%M %p')}"
        else:
            when = "-"
        attendee = getattr(r, "attendee_name", "") or getattr(checkout.client, "name", "")
        lines.append(f"{idx}. {when} - {attendee} - {_money_fmt(r.amount_due, currency)}")
    lines.extend(
        [
            "",
            f"Discount: {_money_fmt(getattr(checkout, 'discount_amount', 0), currency)}",
            f"Discount code: {getattr(checkout, 'applied_discount_code', '') or '-'}",
            f"Total: {_money_fmt(checkout.total_amount, currency)}",
            "",
            "Thank you for your booking.",
        ]
    )

    stream_lines = ["BT", "/F1 12 Tf", "50 760 Td"]
    for i, line in enumerate(lines):
        if i > 0:
            stream_lines.append("0 -16 Td")
        stream_lines.append(f"({_pdf_escape(line)}) Tj")
    stream_lines.append("ET")
    content = ("\n".join(stream_lines) + "\n").encode("latin-1", errors="replace")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    pdf = bytearray(header)
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_pos = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            "trailer\n"
            f"<< /Size {len(offsets)} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_pos}\n"
            "%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


def _send_checkout_confirmation_email(checkout):
    """Send booking confirmation email with invoice PDF once per checkout."""
    if not getattr(settings, "TRAINER_SEND_TRANSACTIONAL_EMAILS", True):
        return False
    if not checkout or checkout.status != Checkout.STATUS_CONFIRMED:
        return False
    if checkout.confirmation_email_sent_at:
        return False

    reservations = list(
        Reservation.objects.select_related("timeslot")
        .filter(checkout=checkout)
        .order_by("timeslot__date", "timeslot__time")
    )
    if not reservations:
        return False
    client_email = (getattr(checkout.client, "email", "") or "").strip()
    if not client_email:
        return False

    currency = (checkout.currency or "USD").upper()
    checkout_code = str(checkout.id)[:8].upper()
    subject = f"Confirmación de reserva #{checkout_code} · Reserv.io"
    session_items = []
    for r in reservations:
        slot = r.timeslot
        attendee = getattr(r, "attendee_name", "") or getattr(checkout.client, "name", "")
        session_items.append(
            {
                "when": f"{slot.date} {slot.time.strftime('%I:%M %p')}",
                "attendee": attendee,
                "amount": _money_fmt(r.amount_due, currency),
            }
        )

    pdf_bytes = _build_invoice_pdf_bytes(checkout=checkout, reservations=reservations)
    filename = f"invoice-{str(checkout.id)[:8].lower()}.pdf"
    _send_templated_email(
        subject=subject,
        to=[client_email],
        text_template="emails/booking_confirmation.txt",
        html_template="emails/booking_confirmation.html",
        context={
            "client_name": getattr(checkout.client, "name", "cliente"),
            "trainer_name": getattr(checkout.trainer, "business_name", "-"),
            "checkout_code": checkout_code,
            "discount_amount": _money_fmt(getattr(checkout, "discount_amount", 0), currency),
            "discount_code": getattr(checkout, "applied_discount_code", "") or "-",
            "total_paid": _money_fmt(checkout.total_amount, currency),
            "session_items": session_items,
        },
        attachments=[(filename, pdf_bytes, "application/pdf")],
    )

    updated = Checkout.objects.filter(
        id=checkout.id,
        confirmation_email_sent_at__isnull=True,
    ).update(confirmation_email_sent_at=timezone.now())
    if updated:
        checkout.refresh_from_db(fields=["confirmation_email_sent_at"])
    return bool(updated)


def _send_checkout_confirmation_email_async(checkout_id):
    """Best-effort async email send to keep webhooks fast and resilient."""
    def _worker():
        try:
            checkout = Checkout.objects.filter(id=checkout_id).first()
            if checkout:
                _send_checkout_confirmation_email(checkout)
        except Exception:
            logger.exception("No se pudo enviar email async checkout_id=%s", checkout_id)

    threading.Thread(target=_worker, daemon=True).start()


def _validate_trainer_coupon(*, trainer, coupon_code_input):
    """Validate trainer coupon and return tuple(valid, percent, error_message)."""
    if not coupon_code_input:
        return True, 0, ""

    trainer_discount_code = (getattr(trainer, "discount_code", "") or "").strip().upper()
    trainer_discount_percent = int(getattr(trainer, "discount_percent_off", 0) or 0)
    if not trainer_discount_code or coupon_code_input != trainer_discount_code or trainer_discount_percent <= 0:
        return False, 0, "El código de descuento no es válido para este entrenador."

    expires_on = getattr(trainer, "discount_expires_on", None)
    if expires_on and timezone.localdate() > expires_on:
        return False, 0, "Este código de descuento ya expiró."

    max_uses = int(getattr(trainer, "discount_max_uses", 0) or 0)
    if max_uses > 0:
        used = Checkout.objects.filter(
            trainer=trainer,
            applied_discount_code=trainer_discount_code,
            status__in=[Checkout.STATUS_PENDING, Checkout.STATUS_CONFIRMED],
        ).count()
        if used >= max_uses:
            return False, 0, "Este código de descuento alcanzó su límite de usos."

    return True, trainer_discount_percent, ""


def _trainer_booking_readiness(trainer):
    """Evalúa si un entrenador está listo para recibir reservas públicas."""
    has_profile_basics = bool((trainer.business_name or "").strip()) and Decimal(str(trainer.session_price or 0)) > Decimal("0")
    has_availability = TrainerAvailability.objects.filter(trainer=trainer, active=True).exists()
    has_manual_payment = bool((getattr(trainer, "ath_mobile_handle", "") or "").strip())
    has_stripe_payment = bool(services.is_trainer_approved(trainer) and services.is_trainer_stripe_ready(trainer))
    has_payment_setup = has_manual_payment or has_stripe_payment

    missing = []
    if not has_profile_basics:
        missing.append("perfil y precio")
    if not has_availability:
        missing.append("disponibilidad")
    if not has_payment_setup:
        missing.append("pagos")

    ready = len(missing) == 0
    message = ""
    if not ready:
        message = (
            "Este perfil aún no está listo para recibir reservas. "
            "Falta completar: " + ", ".join(missing) + "."
        )

    return {
        "ready": ready,
        "message": message,
        "has_profile_basics": has_profile_basics,
        "has_availability": has_availability,
        "has_payment_setup": has_payment_setup,
        "has_manual_payment": has_manual_payment,
        "has_stripe_payment": has_stripe_payment,
    }


def _get_trainer_for_user(user):
    """Devuelve el Trainer para el usuario autenticado.

    Lo mantenemos en un solo lugar para que todas las vistas de entrenador
    se comporten de forma consistente.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    try:
        return Trainer.objects.get(user=user)
    except Trainer.DoesNotExist:
        return None
    except MultipleObjectsReturned:
        # No debería pasar si Trainer.user es OneToOneField, pero si hay datos sucios
        # preferimos fallar de forma segura en vez de mostrar el entrenador equivocado.
        return "__MULTIPLE__"


# Helper: inicio de semana (domingo)
def _week_start_sunday(d):
    """Devuelve el domingo de la semana que contiene la fecha `d` (fecha local)."""
    weekday = d.weekday()  # Mon=0 ... Sun=6
    return d - timedelta(days=(weekday + 1) % 7)


def _build_booking_context(*, trainer, week_param, request=None, form_data=None, error_message=None):
    """Construye contexto compartido para la página de reservas.

    Mantiene la UI consistente cuando hay que re-renderizar el formulario
    con un error amigable.
    """
    form_data = form_data or {}
    booking_dependents = []
    is_client_session = False
    client_identity_locked = False
    if request and request.user.is_authenticated:
        profile = ClientProfile.objects.filter(user=request.user, active=True).first()
        if profile:
            is_client_session = True
            client_identity_locked = True
            booking_dependents = list(
                ClientDependent.objects.filter(profile=profile, active=True).order_by("full_name")
            )
            # Auto-fill identity fields from authenticated client account.
            if not form_data.get("name"):
                form_data["name"] = (
                    (profile.full_name or "").strip()
                    or (request.user.get_full_name() or "").strip()
                    or (request.user.email or "").strip()
                )
            if not form_data.get("email"):
                form_data["email"] = (request.user.email or "").strip().lower()
            if not form_data.get("phone"):
                form_data["phone"] = (profile.phone or "").strip()

    today = timezone.localdate()

    # week=current | next
    week_param = (week_param or "current").strip().lower()
    if week_param not in {"current", "next"}:
        week_param = "current"

    weekday = today.weekday()
    sunday = _week_start_sunday(today)
    if week_param == "next":
        sunday = sunday + timedelta(days=7)

    end_date = sunday + timedelta(days=6)
    week_dates = [sunday + timedelta(days=i) for i in range(7)]

    # Regla UX: la semana actual muestra solo desde hoy en adelante.
    start_date = today if week_param == "current" else sunday

    # Asegura que la DB tenga los slots más recientes para la semana solicitada.
    # (Idempotente: es seguro llamarlo repetidamente.)
    _maybe_sync_timeslots_for_week(trainer=trainer, week_start=sunday)

    readiness = _trainer_booking_readiness(trainer)

    # Obtiene slots disponibles de DB para esa semana.
    timeslots = (
        _available_timeslots_queryset(trainer=trainer, week_start=sunday)
        .filter(date__gte=start_date, date__lte=end_date)
        .order_by("date", "time")
    )
    if not readiness["ready"]:
        timeslots = timeslots.none()

    dates_with_slots = {slot.date.isoformat() for slot in timeslots}

    ctx = {
        "trainer": trainer,
        "timeslots": timeslots,
        "week_dates": week_dates,
        "dates_with_slots": list(dates_with_slots),
        "form_data": form_data,
        "week_param": week_param,
        "week_range_label": f"{sunday.strftime('%m/%d/%y')} – {end_date.strftime('%m/%d/%y')}",
        "booking_enabled": readiness["ready"],
        "booking_disabled_reason": readiness["message"],
        "is_client_session": is_client_session,
        "client_identity_locked": client_identity_locked,
        "booking_dependents": booking_dependents,
    }

    if error_message:
        ctx["error_message"] = error_message
    elif not readiness["ready"] and readiness["message"]:
        ctx["error_message"] = readiness["message"]

    return ctx


def booking_view(request, slug):
    """Página de reservas: muestra disponibilidad de la semana actual o próxima."""
    trainer = get_object_or_404(Trainer, slug=slug, active=True)
    week_param = (request.GET.get("week") or "current")

    ctx = _build_booking_context(trainer=trainer, week_param=week_param, request=request, form_data={})
    return render(request, "booking/booking_form.html", ctx)


@require_POST
def create_checkout_view(request, slug):
    """Solo POST: crea un Checkout + reservas.

    - Si payment_method == STRIPE, crea Stripe Checkout Session y redirige.
    - Si payment_method == ATH, muestra la página de éxito con QR/instrucciones
      (pendiente de confirmación manual).
    """
    trainer = get_object_or_404(Trainer, slug=slug, active=True)
    week_param = (request.GET.get("week") or "current")
    readiness = _trainer_booking_readiness(trainer)

    def _redirect_back_to_booking(message: str, *, level: str = "error"):
        """Redirige al formulario de reservas sin consultar DB dentro de una transacción atómica rota."""
        if level == "success":
            messages.success(request, message)
        elif level == "info":
            messages.info(request, message)
        else:
            messages.error(request, message)

        wp = (request.GET.get("week") or "current").strip().lower()
        if wp not in {"current", "next"}:
            wp = "current"
        url = reverse("booking:booking", kwargs={"slug": trainer.slug})
        return redirect(f"{url}?week={wp}")

    if not readiness["ready"]:
        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
            request=request,
            form_data={
                "name": request.POST.get("name", ""),
                "email": request.POST.get("email", ""),
                "phone": request.POST.get("phone", ""),
                "payment_method": request.POST.get("payment_method", "STRIPE"),
            },
            error_message=readiness["message"],
        )
        return render(request, "booking/booking_form.html", ctx)

    # 1) Leer slots seleccionados
    timeslot_ids = request.POST.getlist("timeslot_ids")
    # quitar duplicados manteniendo orden
    timeslot_ids = list(dict.fromkeys(timeslot_ids))
    if not timeslot_ids:
        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
            request=request,
            form_data={"name": request.POST.get("name", ""), "email": request.POST.get("email", ""), "phone": request.POST.get("phone", ""), "payment_method": request.POST.get("payment_method", "STRIPE")},
            error_message="Selecciona al menos un horario antes de reservar.",
        )
        return render(request, "booking/booking_form.html", ctx)

    # 2) Datos del cliente
    name = (request.POST.get("name") or "").strip()
    email = (request.POST.get("email") or "").strip().lower()
    phone = (request.POST.get("phone") or "").strip()
    coupon_code_input = (request.POST.get("coupon_code") or "").strip().upper()
    if not name or not email:
        pm = (request.POST.get("payment_method") or Reservation.PAYMENT_STRIPE).strip().upper()
        if pm not in {Reservation.PAYMENT_STRIPE, Reservation.PAYMENT_ATH}:
            pm = Reservation.PAYMENT_STRIPE

        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
            request=request,
            form_data={"name": name, "email": email, "phone": phone, "payment_method": pm, "timeslot_ids": timeslot_ids},
            error_message="Completa tu nombre y email para continuar.",
        )
        return render(request, "booking/booking_form.html", ctx)

    # Si hay sesión de cliente, forzar coherencia de identidad por email.
    session_client_profile = None
    allowed_dependents_by_id = {}
    allowed_dependent_ids = set()
    if request.user.is_authenticated:
        session_client_profile = ClientProfile.objects.filter(user=request.user, active=True).first()
    if session_client_profile:
        dependents = list(
            ClientDependent.objects.filter(profile=session_client_profile, active=True).order_by("full_name")
        )
        allowed_dependents_by_id = {d.id: d for d in dependents}
        allowed_dependent_ids = set(allowed_dependents_by_id.keys())
    if session_client_profile:
        account_email = (request.user.email or "").strip().lower()
        if account_email and email != account_email:
            ctx = _build_booking_context(
                trainer=trainer,
                week_param=week_param,
                request=request,
                form_data={"name": name, "email": account_email, "phone": phone, "payment_method": request.POST.get("payment_method", "STRIPE"), "timeslot_ids": timeslot_ids, "coupon_code": coupon_code_input},
                error_message="El email de la reserva debe coincidir con tu cuenta de cliente.",
            )
            return render(request, "booking/booking_form.html", ctx)

    # Participantes por horario (self y/o dependientes por slot).
    attendees_by_slot = {}
    for slot_id in timeslot_ids:
        slot_key = str(slot_id)
        if not session_client_profile:
            attendees_by_slot[slot_key] = ["self"]
            continue

        raw_values = request.POST.getlist(f"attendees_{slot_key}")
        normalized = []
        seen = set()
        for raw in raw_values:
            token = (raw or "").strip().lower()
            if token == "self":
                if token not in seen:
                    normalized.append(token)
                    seen.add(token)
                continue
            if token.startswith("dep:"):
                dep_id_raw = token.split(":", 1)[1]
                if dep_id_raw.isdigit():
                    dep_id = int(dep_id_raw)
                    if dep_id in allowed_dependent_ids:
                        dep_token = f"dep:{dep_id}"
                        if dep_token not in seen:
                            normalized.append(dep_token)
                            seen.add(dep_token)
        if not normalized:
            normalized = ["self"]
        attendees_by_slot[slot_key] = normalized

    # 3) Método de pago
    payment_method = (request.POST.get("payment_method") or Reservation.PAYMENT_STRIPE).strip().upper()
    if payment_method not in {Reservation.PAYMENT_STRIPE, Reservation.PAYMENT_ATH}:
        payment_method = Reservation.PAYMENT_STRIPE

    coupon_ok, trainer_discount_percent, coupon_error = _validate_trainer_coupon(
        trainer=trainer,
        coupon_code_input=coupon_code_input,
    )
    apply_discount = bool(coupon_code_input and coupon_ok and trainer_discount_percent > 0)
    if coupon_code_input and not coupon_ok:
        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
            request=request,
            form_data={
                "name": name,
                "email": email,
                "phone": phone,
                "payment_method": payment_method,
                "timeslot_ids": timeslot_ids,
                "coupon_code": coupon_code_input,
            },
            error_message=coupon_error or "El código de descuento no es válido.",
        )
        return render(request, "booking/booking_form.html", ctx)

    # Guardrail: no permitir Stripe checkout para entrenadores no aprobados/no onboarded.
    # (Evita que clientes paguen a un entrenador que aún no puede recibir payout.)
    if payment_method == Reservation.PAYMENT_STRIPE and (
        (not services.is_trainer_approved(trainer)) or (not services.is_trainer_stripe_ready(trainer))
    ):
        reason = []
        if not services.is_trainer_approved(trainer):
            reason.append("este entrenador aún no está aprobado")
        if not services.is_trainer_stripe_ready(trainer):
            reason.append("este entrenador aún no ha conectado Stripe")

        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
            request=request,
            form_data={
                "name": name,
                "email": email,
                "phone": phone,
                "payment_method": Reservation.PAYMENT_ATH,
                "timeslot_ids": timeslot_ids,
            },
            error_message=(
                "Pago con tarjeta no disponible: "
                + " y ".join(reason)
                + ". Selecciona ATH Móvil o intenta más tarde."
            ),
        )
        return render(request, "booking/booking_form.html", ctx)

    # 4) Cargar y bloquear slots + crear registros DB en transacción CORTA
    today = timezone.localdate()

    try:
        with transaction.atomic():
            slots_qs = (
                TimeSlot.objects
                .select_for_update()
                .filter(trainer=trainer, active=True, id__in=timeslot_ids)
                .filter(date__gte=today)
                .order_by("date", "time")
            )
            slots = list(slots_qs)

            # Asegura que todos los IDs existan y pertenezcan a este entrenador
            if len(slots) != len(timeslot_ids):
                ctx = _build_booking_context(
                    trainer=trainer,
                    week_param=week_param,
                    request=request,
                    form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                    error_message="Uno o más horarios ya no están disponibles. Selecciona otros y vuelve a intentar.",
                )
                return render(request, "booking/booking_form.html", ctx)

            # Validación de capacidad considerando asistentes por slot.
            # Nota Postgres: no combinar select_for_update() con annotate()/GROUP BY.
            # Por eso el lock de slots se hace arriba y el conteo en una consulta separada.
            reservations_by_slot = {
                row["timeslot_id"]: row["num_reservations"]
                for row in (
                    Reservation.objects
                    .filter(timeslot_id__in=[s.id for s in slots])
                    .values("timeslot_id")
                    .annotate(num_reservations=Count("id"))
                )
            }
            for s in slots:
                seats_requested = len(attendees_by_slot.get(str(s.id), ["self"]))
                current_reserved = int(reservations_by_slot.get(s.id, 0))
                if (current_reserved + seats_requested) > s.capacity:
                    ctx = _build_booking_context(
                        trainer=trainer,
                        week_param=week_param,
                        request=request,
                        form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                        error_message="Uno de los horarios seleccionados se llenó. Selecciona otro horario.",
                    )
                    return render(request, "booking/booking_form.html", ctx)

            # 5) Obtener/crear cliente para este entrenador
            client, _ = Client.objects.get_or_create(
                trainer=trainer,
                email=email,
                defaults={"name": name, "phone": phone, "user": request.user if session_client_profile else None},
            )
            # Mantener datos de cliente actualizados
            changed = False
            if client.name != name:
                client.name = name
                changed = True
            if phone and getattr(client, "phone", "") != phone:
                client.phone = phone
                changed = True
            if session_client_profile and client.user_id != request.user.id:
                client.user = request.user
                changed = True
            if changed:
                client.save()

            # Evitar reserva duplicada por asistente en un mismo horario.
            for s in slots:
                attendee_tokens = attendees_by_slot.get(str(s.id), ["self"])
                if Reservation.objects.filter(client=client, timeslot=s, attendee_key__in=attendee_tokens).exists():
                    ctx = _build_booking_context(
                        trainer=trainer,
                        week_param=week_param,
                        request=request,
                        form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                        error_message="Uno de los asistentes ya tiene reserva en uno de esos horarios.",
                    )
                    return render(request, "booking/booking_form.html", ctx)

            # 6) Calcular total
            raw_price = getattr(trainer, "session_price", None)
            if raw_price is None:
                raw_price = getattr(trainer, "price_per_session", None)
            if raw_price is None:
                raw_price = Decimal("0")

            price = Decimal(str(raw_price))
            currency = getattr(trainer, "currency", None) or "USD"
            total_units = sum(len(attendees_by_slot.get(str(slot.id), ["self"])) for slot in slots)
            base_total = (price * Decimal(total_units)).quantize(Decimal("0.01"))
            unit_price = price
            if apply_discount:
                multiplier = (Decimal("100") - Decimal(str(trainer_discount_percent))) / Decimal("100")
                unit_price = (price * multiplier).quantize(Decimal("0.01"))
            total_amount = (unit_price * Decimal(total_units)).quantize(Decimal("0.01"))
            discount_amount = (base_total - total_amount).quantize(Decimal("0.01"))

            if total_amount <= Decimal("0.00"):
                ctx = _build_booking_context(
                    trainer=trainer,
                    week_param=week_param,
                    request=request,
                    form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                    error_message="Este entrenador todavía no tiene precio configurado. Intenta más tarde.",
                )
                return render(request, "booking/booking_form.html", ctx)

            # 7) Crear checkout (pending por defecto)
            platform_fee_percent = services.get_platform_fee_percent(trainer=trainer) if hasattr(services, "get_platform_fee_percent") else Decimal("0")
            platform_fee_amount = (
                services.compute_platform_fee_amount(total_amount, trainer=trainer)
                if hasattr(services, "compute_platform_fee_amount")
                else Decimal("0.00")
            )
            trainer_net_amount = (total_amount - platform_fee_amount).quantize(Decimal("0.01"))
            checkout = Checkout.objects.create(
                trainer=trainer,
                client=client,
                payment_method=payment_method,
                status=Checkout.STATUS_PENDING,
                currency=currency,
                total_amount=total_amount,
                applied_discount_code=(coupon_code_input if apply_discount else ""),
                applied_discount_percent=(trainer_discount_percent if apply_discount else 0),
                discount_amount=(discount_amount if apply_discount else Decimal("0.00")),
                platform_fee_percent_applied=platform_fee_percent,
                platform_fee_amount=platform_fee_amount,
                trainer_net_amount=trainer_net_amount,
            )

            # 8) Crear reservas vinculadas al checkout
            reservations = []
            for slot in slots:
                for attendee_token in attendees_by_slot.get(str(slot.id), ["self"]):
                    attendee_type = Reservation.ATTENDEE_SELF
                    attendee_name = name
                    attendee_key = "self"
                    dependent = None
                    if attendee_token.startswith("dep:"):
                        dep_id = int(attendee_token.split(":", 1)[1])
                        dependent = allowed_dependents_by_id.get(dep_id)
                        if dependent:
                            attendee_type = Reservation.ATTENDEE_DEPENDENT
                            attendee_name = dependent.full_name
                            attendee_key = f"dep:{dependent.id}"

                    res = Reservation.objects.create(
                        trainer=trainer,
                        client=client,
                        timeslot=slot,
                        checkout=checkout,
                        amount_due=unit_price,
                        payment_method=payment_method,
                        paid=False,
                        attendee_type=attendee_type,
                        attendee_name=attendee_name,
                        attendee_key=attendee_key,
                        dependent=dependent,
                    )
                    reservations.append(res)

    except (IntegrityError, ValidationError):
        # Mantener fuera de la transacción para evitar TransactionManagementError
        return _redirect_back_to_booking(
            "Ya tienes una reserva para uno de esos horarios (o alguien lo tomó justo ahora). Selecciona un horario diferente.",
            level="error",
        )

    # Si Stripe está seleccionado, crear Stripe Checkout Session FUERA de la transacción DB.
    if payment_method == Reservation.PAYMENT_STRIPE:
        try:
            # La capa services debería lanzar errores amigables para casos comunes
            # (no aprobado, no onboarded, falta config de Stripe, etc.)
            session_obj = services.create_stripe_checkout_session(
                request=request,
                trainer=trainer,
                client=client,
                checkout=checkout,
                unit_amount=price,
                quantity=sum(len(attendees_by_slot.get(str(slot.id), ["self"])) for slot in slots),
                currency=currency,
                week_param=week_param,
            )

            # Acepta múltiples formatos: string URL | dict con 'url' | objeto con .url
            session_url = None
            session_id = None
            if isinstance(session_obj, str):
                session_url = session_obj
            elif isinstance(session_obj, dict):
                session_url = session_obj.get("url")
                session_id = session_obj.get("id")
            else:
                session_url = getattr(session_obj, "url", None)
                session_id = getattr(session_obj, "id", None)

            if not session_url:
                raise ValueError("No se pudo crear la sesión de pago de Stripe.")

            # Persistir session id si existe
            if session_id and getattr(checkout, "stripe_session_id", None) != session_id:
                Checkout.objects.filter(id=checkout.id).update(stripe_session_id=session_id)

            return redirect(session_url)

        except getattr(services, "ServiceUserError", Exception) as e:
            # Errores esperados y orientados al usuario desde services.py
            user_msg = getattr(e, "user_message", None) or str(e) or "Este entrenador todavía no está listo para recibir pagos con tarjeta."
            # Marcar checkout cancelado para evitar pendientes colgados en desarrollo.
            Checkout.objects.filter(id=checkout.id).update(status=Checkout.STATUS_CANCELLED)
            return _redirect_back_to_booking(
                f"Pago con tarjeta no disponible: {user_msg}. Selecciona ATH Móvil o intenta más tarde.",
                level="error",
            )

        except stripe.error.StripeError:
            # Errores del SDK de Stripe (red, parámetros inválidos, etc.)
            Checkout.objects.filter(id=checkout.id).update(status=Checkout.STATUS_CANCELLED)
            return _redirect_back_to_booking(
                "No pudimos iniciar el pago con Stripe ahora mismo. Intenta de nuevo en unos segundos.",
                level="error",
            )

        except ValueError as e:
            Checkout.objects.filter(id=checkout.id).update(status=Checkout.STATUS_CANCELLED)
            msg = str(e) or "Este entrenador todavía no está listo para recibir pagos con tarjeta."
            return _redirect_back_to_booking(
                f"Pago con tarjeta no disponible: {msg}. Selecciona ATH Móvil o intenta más tarde.",
                level="error",
            )

    # ATH (manual): mostrar recibo/instrucciones (pendiente de confirmación manual)
    return render(
        request,
        "booking/booking_success.html",
        {
            "trainer": trainer,
            "client": client,
            "reservations": reservations,
            "total_amount": total_amount,
            "payment_method": payment_method,
            "checkout": checkout,
            "return_url": (
                reverse("booking:client_portal_dashboard")
                if request.user.is_authenticated and ClientProfile.objects.filter(user=request.user, active=True).exists()
                else reverse("booking:home_io")
            ),
            "return_label": (
                "Volver a Inicio"
                if request.user.is_authenticated and ClientProfile.objects.filter(user=request.user, active=True).exists()
                else "Volver al inicio"
            ),
        },
    )


def booking_success_view(request):
    """Página de éxito.

    En Stripe, el usuario puede caer aquí justo tras pagar, pero el webhook
    sigue siendo la fuente de verdad. Aun así, mostramos una vista tipo recibo
    basada en el Checkout.
    """
    checkout_id = (request.GET.get("checkout_id") or "").strip()
    if not checkout_id:
        return HttpResponseBadRequest("Missing checkout_id")

    checkout = get_object_or_404(Checkout, id=checkout_id)
    trainer = checkout.trainer
    client = checkout.client

    # Fallback DEV: si webhooks de Stripe no están configurados/recibidos aún,
    # intentar reconciliar estado de pago desde Stripe al llegar a success.
    # NOTA: en producción, el webhook sigue siendo la fuente de verdad.
    if (
        getattr(settings, "DEBUG", False)
        and checkout.payment_method == Reservation.PAYMENT_STRIPE
        and checkout.status != Checkout.STATUS_CONFIRMED
        and getattr(checkout, "stripe_session_id", "")
        and getattr(settings, "STRIPE_SECRET_KEY", "")
    ):
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            session = stripe.checkout.Session.retrieve(checkout.stripe_session_id)
            payment_status = (session.get("payment_status") or "").lower()
            session_status = (session.get("status") or "").lower()
            payment_intent = session.get("payment_intent")

            # Marcar confirmado cuando Stripe indique pago completado.
            if payment_status == "paid" or session_status in {"complete", "completed"}:
                with transaction.atomic():
                    Checkout.objects.filter(id=checkout.id).update(
                        status=Checkout.STATUS_CONFIRMED,
                        confirmed_at=timezone.now(),
                        stripe_payment_intent_id=payment_intent or getattr(checkout, "stripe_payment_intent_id", None),
                    )
                    Reservation.objects.filter(checkout=checkout).update(
                        paid=True,
                        payment_method=Reservation.PAYMENT_STRIPE,
                        payment_date=timezone.now(),
                    )
                checkout.refresh_from_db()
                try:
                    _send_checkout_confirmation_email(checkout)
                except Exception:
                    logger.exception("No se pudo enviar email de confirmacion checkout_id=%s", checkout.id)
        except stripe.error.StripeError:
            # Ignorar y dejar la página en pending; el webhook puede llegar después.
            pass

    reservations = (
        Reservation.objects
        .select_related("timeslot")
        .filter(checkout=checkout)
        .order_by("timeslot__date", "timeslot__time")
    )

    return render(
        request,
        "booking/booking_success.html",
        {
            "trainer": trainer,
            "client": client,
            "reservations": reservations,
            "total_amount": checkout.total_amount,
            "payment_method": checkout.payment_method,
            "checkout": checkout,
            "return_url": (
                reverse("booking:client_portal_dashboard")
                if request.user.is_authenticated and ClientProfile.objects.filter(user=request.user, active=True).exists()
                else reverse("booking:home_io")
            ),
            "return_label": (
                "Volver a Inicio"
                if request.user.is_authenticated and ClientProfile.objects.filter(user=request.user, active=True).exists()
                else "Volver al inicio"
            ),
        },
    )


# Endpoint de webhook de Stripe
@csrf_exempt
def stripe_webhook_view(request):
    """Endpoint webhook de Stripe.

    Los webhooks son la fuente de verdad para confirmar pagos Stripe.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    # Debe estar configurado en .env y cargado en settings
    if not getattr(settings, "STRIPE_WEBHOOK_SECRET", ""):
        return HttpResponse(status=500)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    # El SDK de Stripe usa la secret key para operaciones API
    # (no estrictamente necesaria para verificación de firma)
    stripe.api_key = getattr(settings, "STRIPE_SECRET_KEY", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        StripeWebhookEvent.objects.create(
            processed_ok=False,
            error_message="Invalid payload",
            payload={},
        )
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        StripeWebhookEvent.objects.create(
            processed_ok=False,
            error_message="Invalid signature",
            payload={},
        )
        return HttpResponse(status=400)

    event_type = event.get("type")
    event_id = event.get("id") or ""
    webhook_log = StripeWebhookEvent.objects.filter(event_id=event_id).first() if event_id else None
    if webhook_log and webhook_log.processed_ok:
        # Idempotency: event already processed.
        return HttpResponse(status=200)
    if webhook_log is None:
        webhook_log = StripeWebhookEvent.objects.create(
            event_id=event_id,
            event_type=event_type or "",
            livemode=bool(event.get("livemode", False)),
            processed_ok=False,
            payload={
                "id": event_id,
                "type": event_type or "",
                "created": event.get("created"),
            },
        )

    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        session = event["data"]["object"]

        checkout_id = (session.get("metadata") or {}).get("checkout_id")
        session_id = session.get("id")
        payment_intent = session.get("payment_intent")

        checkout = None
        if checkout_id:
            checkout = Checkout.objects.filter(id=checkout_id).first()
        if checkout is None and session_id:
            checkout = Checkout.objects.filter(stripe_session_id=session_id).first()

        if checkout:
            # Idempotente: seguro llamar múltiples veces
            updated_fields = []

            if session_id and checkout.stripe_session_id != session_id:
                checkout.stripe_session_id = session_id
                updated_fields.append("stripe_session_id")

            if payment_intent and getattr(checkout, "stripe_payment_intent_id", None) != payment_intent:
                checkout.stripe_payment_intent_id = payment_intent
                updated_fields.append("stripe_payment_intent_id")

            if checkout.status != Checkout.STATUS_CONFIRMED:
                checkout.status = Checkout.STATUS_CONFIRMED
                checkout.confirmed_at = timezone.now()
                updated_fields.extend(["status", "confirmed_at"])

                Reservation.objects.filter(checkout=checkout).update(
                    paid=True,
                    payment_method=Reservation.PAYMENT_STRIPE,
                    payment_date=timezone.now(),
                )

            if updated_fields:
                checkout.save(update_fields=list(dict.fromkeys(updated_fields)))
            if checkout.status == Checkout.STATUS_CONFIRMED:
                _send_checkout_confirmation_email_async(checkout.id)

    webhook_log.processed_ok = True
    webhook_log.error_message = ""
    webhook_log.save(update_fields=["processed_ok", "error_message"])
    return HttpResponse(status=200)


# ====== FASE 1: Páginas públicas ======

def home_io_view(request):
    """Landing simple (/)"""
    return render(request, "home_io.html")


def home_full_view(request):
    """Landing completa (/home/)"""
    return render(request, "home.html")

# Alias retrocompatible por si algo aún referencia `home_view` (home completa)
home_view = home_full_view


# --- Página embudo para entrenadores: elegir iniciar sesión o crear cuenta
def trainer_access_view(request):
    """Página embudo para entrenadores.

    Es el punto de entrada cuando el usuario pulsa “Soy entrenador personal”
    en home_io. NO debe enviarlo directo a registro; ofrece:
    - Iniciar sesión
    - Crear cuenta
    """
    context = {}
    if request.user.is_authenticated:
        has_trainer, has_client = _resolve_account_roles(request.user)
        trainer = _get_trainer_for_user(request.user)
        context.update(
            {
                "is_logged_in": True,
                "has_trainer": has_trainer,
                "has_client": has_client,
                "has_trainer_verified": bool(
                    trainer not in (None, "__MULTIPLE__") and getattr(trainer, "email_verified", False)
                ),
            }
        )
    return render(request, "booking/trainer_access.html", context)


# Nombre alterno/retrocompatible por si alguna URL usa `trainer_access`
trainer_access = trainer_access_view


def client_access_view(request):
    """Página embudo para clientes: iniciar sesión o crear cuenta."""
    context = {}
    if request.user.is_authenticated:
        has_trainer, has_client = _resolve_account_roles(request.user)
        profile = _get_client_profile_for_user(request.user)
        context.update(
            {
                "is_logged_in": True,
                "has_trainer": has_trainer,
                "has_client": has_client,
                "has_client_verified": bool(profile and profile.email_verified),
            }
        )
    return render(request, "booking/client_access.html", context)


def trainer_list_view(request):
    """Página pública de lista de entrenadores (/trainers/)"""
    trainers = Trainer.objects.filter(active=True).order_by("business_name")
    return render(request, "booking/trainer_list.html", {"trainers": trainers})


def account_portal_home_view(request):
    """Entry-point único para redirigir al portal correcto según rol."""
    if not request.user.is_authenticated:
        return redirect("login")

    has_trainer, has_client = _resolve_account_roles(request.user)
    if has_trainer and has_client:
        forced_role = (request.GET.get("role") or "").strip().lower()
        if forced_role in {ROLE_TRAINER, ROLE_CLIENT}:
            _set_active_role_session(request, forced_role)
            if forced_role == ROLE_TRAINER:
                trainer = _get_trainer_for_user(request.user)
                if trainer not in (None, "__MULTIPLE__") and _email_verification_is_required() and not trainer.email_verified:
                    return redirect(f"{reverse('booking:trainer_verify_pending')}?email={request.user.email}")
            if forced_role == ROLE_CLIENT:
                client_profile = _get_client_profile_for_user(request.user)
                if client_profile and _client_email_verification_is_required() and not client_profile.email_verified:
                    return redirect(f"{reverse('booking:client_verify_pending')}?email={request.user.email}")
            return _redirect_for_role(forced_role)
        return redirect("booking:account_mode_select")
    if has_trainer:
        _set_active_role_session(request, ROLE_TRAINER)
        return redirect("booking:trainer_portal")
    if has_client:
        _set_active_role_session(request, ROLE_CLIENT)
        return redirect("booking:client_portal_dashboard")

    messages.info(request, "Tu cuenta aún no tiene un perfil activo. Completa tu configuración.")
    return redirect("booking:account_role_management")


@login_required
def account_mode_select_view(request):
    """Selector de modo cuando una cuenta tiene ambos roles."""
    has_trainer, has_client = _resolve_account_roles(request.user)
    if not (has_trainer and has_client):
        return redirect("booking:account_portal_home")

    if request.method == "POST":
        selected_role = (request.POST.get("mode") or "").strip().lower()
        if selected_role not in {ROLE_TRAINER, ROLE_CLIENT}:
            messages.error(request, "Selecciona un modo válido.")
            return redirect("booking:account_mode_select")
        _set_active_role_session(request, selected_role)
        if selected_role == ROLE_TRAINER:
            trainer = _get_trainer_for_user(request.user)
            if trainer not in (None, "__MULTIPLE__") and _email_verification_is_required() and not trainer.email_verified:
                return redirect(f"{reverse('booking:trainer_verify_pending')}?email={request.user.email}")
        if selected_role == ROLE_CLIENT:
            client_profile = _get_client_profile_for_user(request.user)
            if client_profile and _client_email_verification_is_required() and not client_profile.email_verified:
                return redirect(f"{reverse('booking:client_verify_pending')}?email={request.user.email}")
        return _redirect_for_role(selected_role)

    return render(request, "booking/account_mode_select.html")


@login_required
def account_role_management_view(request):
    """Pantalla central para activar roles sobre una cuenta existente."""
    user = request.user
    trainer = _get_trainer_for_user(user)
    has_trainer = trainer not in (None, "__MULTIPLE__")
    has_client = ClientProfile.objects.filter(user=user, active=True).exists()
    two_fa = UserTwoFactorAuth.objects.filter(user=user).first()
    two_fa_enabled = bool(two_fa and two_fa.is_enabled)
    two_fa_method = _two_fa_method()

    trainer_form = TrainerRoleActivationForm(prefix="trainer")
    client_form = ClientRoleActivationForm(prefix="client")

    if request.method == "POST":
        form_name = (request.POST.get("form_name") or "").strip().lower()
        if form_name == "activate_trainer":
            trainer_form = TrainerRoleActivationForm(request.POST, prefix="trainer")
            if has_trainer:
                messages.info(request, "Tu perfil de entrenador ya está activo.")
                return redirect("booking:trainer_portal")
            if trainer_form.is_valid():
                trainer = Trainer.objects.create(
                    user=user,
                    business_name=(trainer_form.cleaned_data.get("business_name") or "").strip(),
                    ath_mobile_handle=(trainer_form.cleaned_data.get("ath_mobile_handle") or "").strip(),
                    active=True,
                    email_verified=False,
                    is_approved=False,
                    stripe_onboarded=False,
                )
                if _email_verification_is_required():
                    try:
                        if user.email:
                            _send_trainer_verification_email(request, user, trainer)
                    except Exception:
                        pass
                    messages.success(request, "Perfil trainer activado. Verifica tu correo para continuar.")
                    return redirect(f"{reverse('booking:trainer_verify_pending')}?email={user.email}")

                trainer.email_verified = True
                trainer.email_verified_at = timezone.now()
                trainer.save(update_fields=["email_verified", "email_verified_at"])
                messages.success(request, "Perfil de entrenador activado.")
                return redirect("booking:trainer_portal")
            messages.error(request, "Corrige los errores para activar el perfil de entrenador.")

        elif form_name == "activate_client":
            client_form = ClientRoleActivationForm(request.POST, prefix="client")
            if has_client:
                messages.info(request, "Tu perfil de cliente ya está activo.")
                return redirect("booking:client_portal_dashboard")
            if client_form.is_valid():
                profile = ClientProfile.objects.create(
                    user=user,
                    full_name=(client_form.cleaned_data.get("full_name") or "").strip(),
                    phone=(client_form.cleaned_data.get("phone") or "").strip(),
                    email_verified=False,
                    active=True,
                )
                if _client_email_verification_is_required():
                    try:
                        if user.email:
                            _send_client_verification_email(request, user, profile)
                    except Exception:
                        logger.exception("No se pudo enviar verificacion de cliente user_id=%s", user.pk)
                        messages.warning(
                            request,
                            "Perfil de cliente activado, pero no pudimos enviar el correo de verificación. Puedes reenviarlo.",
                        )
                    messages.success(request, "Perfil de cliente activado. Verifica tu correo para continuar.")
                    return redirect(f"{reverse('booking:client_verify_pending')}?email={user.email}")

                profile.email_verified = True
                profile.email_verified_at = timezone.now()
                profile.save(update_fields=["email_verified", "email_verified_at"])
                messages.success(request, "Perfil de cliente activado.")
                return redirect("booking:client_portal_dashboard")
            messages.error(request, "Corrige los errores para activar el perfil de cliente.")

    return render(
        request,
        "booking/account_role_management.html",
        {
            "has_trainer": has_trainer,
            "has_client": has_client,
            "trainer_form": trainer_form,
            "client_form": client_form,
            "two_fa_enabled": two_fa_enabled,
            "two_fa_method": two_fa_method,
        },
    )


@login_required
@require_POST
def account_delete_view(request):
    """Elimina la cuenta del usuario autenticado tras confirmar contraseña."""
    password = (request.POST.get("confirm_password") or "").strip()
    confirmation = (request.POST.get("confirm_text") or "").strip().upper()

    if not password:
        messages.error(request, "Escribe tu contraseña para confirmar.")
        return redirect("booking:account_role_management")
    if confirmation != "ELIMINAR":
        messages.error(request, "Escribe ELIMINAR para confirmar la eliminación de cuenta.")
        return redirect("booking:account_role_management")
    if not request.user.check_password(password):
        messages.error(request, "La contraseña es incorrecta.")
        return redirect("booking:account_role_management")

    user = request.user
    logout(request)
    user.delete()
    request.session["account_deleted_notice"] = True
    return redirect("booking:account_deleted")


def account_deleted_view(request):
    """Pantalla de confirmación tras eliminar cuenta."""
    if not request.session.pop("account_deleted_notice", False):
        return redirect("booking:home_io")
    return render(request, "booking/account_deleted.html")


@login_required
def account_two_factor_setup_view(request):
    user = request.user
    if not _is_two_fa_globally_enabled():
        messages.info(request, "2FA está desactivado en esta instancia.")
        return redirect("booking:account_role_management")
    if not (user.email or "").strip():
        messages.error(request, "Tu cuenta no tiene email. Añádelo antes de activar 2FA.")
        return redirect("booking:account_role_management")

    two_fa = _get_or_create_user_2fa(user)
    if request.method == "POST":
        password = (request.POST.get("confirm_password") or "").strip()
        if not password or not request.user.check_password(password):
            messages.error(request, "Contraseña incorrecta. No se pudo activar 2FA.")
        else:
            two_fa.is_enabled = True
            two_fa.totp_secret = ""
            two_fa.backup_codes = []
            two_fa.last_verified_at = timezone.now()
            two_fa.save(update_fields=["is_enabled", "totp_secret", "backup_codes", "last_verified_at", "updated_at"])
            messages.success(request, "Autenticación en dos pasos activada. Recibirás códigos por email al iniciar sesión.")
            return redirect("booking:account_role_management")

    context = {
        "two_fa": two_fa,
        "two_fa_method": _two_fa_method(),
    }
    return render(request, "booking/account_two_factor_setup.html", context)


@login_required
@require_POST
def account_two_factor_disable_view(request):
    two_fa = UserTwoFactorAuth.objects.filter(user=request.user).first()
    if not two_fa or not two_fa.is_enabled:
        messages.info(request, "La autenticación en dos pasos ya estaba desactivada.")
        return redirect("booking:account_role_management")

    password = (request.POST.get("confirm_password") or "").strip()
    if not password or not request.user.check_password(password):
        messages.error(request, "Contraseña incorrecta. No se pudo desactivar 2FA.")
        return redirect("booking:account_role_management")

    two_fa.is_enabled = False
    two_fa.totp_secret = ""
    two_fa.backup_codes = []
    two_fa.save(update_fields=["is_enabled", "totp_secret", "backup_codes", "updated_at"])
    messages.success(request, "Autenticación en dos pasos desactivada.")
    return redirect("booking:account_role_management")


@login_required
@require_POST
def account_two_factor_regenerate_codes_view(request):
    messages.info(request, "Con 2FA por email no se usan códigos de respaldo.")
    return redirect("booking:account_role_management")


def two_factor_verify_view(request):
    pending_user_id = request.session.get(TWO_FA_PENDING_USER_KEY)
    if not pending_user_id:
        messages.info(request, "Tu sesión de verificación expiró. Inicia sesión nuevamente.")
        return redirect("login")

    user_model = get_user_model()
    user = user_model.objects.filter(pk=pending_user_id).first()
    if not user:
        request.session.pop(TWO_FA_PENDING_USER_KEY, None)
        request.session.pop(TWO_FA_PENDING_NEXT_KEY, None)
        messages.error(request, "No se pudo completar la verificación.")
        return redirect("login")

    two_fa = UserTwoFactorAuth.objects.filter(user=user, is_enabled=True).first()
    if not two_fa or not _is_two_fa_globally_enabled():
        next_url = request.session.pop(TWO_FA_PENDING_NEXT_KEY, "")
        request.session.pop(TWO_FA_PENDING_USER_KEY, None)
        request.session.pop(TWO_FA_CODE_HASH_KEY, None)
        request.session.pop(TWO_FA_CODE_EXPIRES_KEY, None)
        request.session.pop(TWO_FA_CODE_RESEND_AT_KEY, None)
        backend_path = (getattr(settings, "AUTHENTICATION_BACKENDS", []) or ["django.contrib.auth.backends.ModelBackend"])[0]
        auth_login(request, user, backend=backend_path)
        return redirect(_post_login_redirect_url(request, user, next_url=next_url))

    max_attempts = int(getattr(settings, "TWO_FA_RATE_LIMIT_MAX_ATTEMPTS", 8) or 8)
    window_seconds = int(getattr(settings, "TWO_FA_RATE_LIMIT_WINDOW_SECONDS", 300) or 300)
    limiter_keys = _rate_limiter_keys("2fa", request=request, identifier=str(user.pk))
    if _rate_limiter_is_blocked(limiter_keys, max_attempts=max_attempts):
        messages.error(request, "Demasiados intentos. Espera unos minutos e inténtalo de nuevo.")
        return redirect("login")

    code_hash = request.session.get(TWO_FA_CODE_HASH_KEY, "")
    expires_at = int(request.session.get(TWO_FA_CODE_EXPIRES_KEY, 0) or 0)
    resend_at = int(request.session.get(TWO_FA_CODE_RESEND_AT_KEY, 0) or 0)
    now_ts = int(time.time())

    if request.method == "POST":
        action = (request.POST.get("action") or "verify").strip().lower()
        if action == "resend":
            if now_ts < resend_at:
                wait_seconds = max(1, resend_at - now_ts)
                messages.info(request, f"Espera {wait_seconds}s para reenviar otro código.")
            else:
                try:
                    _issue_two_fa_code(request, user)
                    messages.success(request, "Te enviamos un nuevo código por email.")
                except Exception:
                    logger.exception("No se pudo enviar código 2FA por email para user_id=%s", user.pk)
                    messages.error(request, "No pudimos enviar el código. Inténtalo nuevamente.")
            return redirect("booking:two_factor_verify")

        submitted_code = "".join(ch for ch in (request.POST.get("code") or "") if ch.isdigit())
        if len(submitted_code) != 6 or not code_hash or now_ts > expires_at or not check_password(submitted_code, code_hash):
            _rate_limiter_hit(limiter_keys, window_seconds=window_seconds)
            if now_ts > expires_at:
                messages.error(request, "El código expiró. Solicita uno nuevo.")
            else:
                messages.error(request, "Código inválido. Intenta nuevamente.")
        else:
            _rate_limiter_clear(limiter_keys)
            two_fa.last_verified_at = timezone.now()
            two_fa.save(update_fields=["last_verified_at", "updated_at"])
            backend_path = (getattr(settings, "AUTHENTICATION_BACKENDS", []) or ["django.contrib.auth.backends.ModelBackend"])[0]
            auth_login(request, user, backend=backend_path)
            next_url = request.session.pop(TWO_FA_PENDING_NEXT_KEY, "")
            request.session.pop(TWO_FA_PENDING_USER_KEY, None)
            request.session.pop(TWO_FA_CODE_HASH_KEY, None)
            request.session.pop(TWO_FA_CODE_EXPIRES_KEY, None)
            request.session.pop(TWO_FA_CODE_RESEND_AT_KEY, None)
            messages.success(request, "Verificación completada.")
            return redirect(_post_login_redirect_url(request, user, next_url=next_url))

    return render(
        request,
        "registration/two_factor_verify.html",
        {
            "masked_email": (user.email or "").strip(),
            "resend_wait_seconds": max(0, resend_at - now_ts),
        },
    )


@login_required
def trainer_portal_view(request):
    """Portal del entrenador (/trainer/).

    Esta página está protegida:
    - Requiere autenticación
    - Requiere perfil de Trainer

    Si el usuario está autenticado pero no tiene perfil, redirige a registro.

    UX:
    - Settings precargado en GET (solo lectura salvo ?edit=1)
    - Availability formset se guarda por separado para evitar errores de ManagementForm.
    """
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(
            request,
            "Hay múltiples perfiles de entrenador vinculados a tu usuario. Contacta a soporte/administración para corregirlo.",
        )
        return redirect("booking:home_io")
    if not trainer:
        messages.info(request, "Primero activa tu perfil de entrenador en tu cuenta.")
        return redirect("booking:account_role_management")
    if _email_verification_is_required() and not trainer.email_verified:
        messages.warning(request, "Debes verificar tu correo para entrar al portal.")
        return redirect(f"{reverse('booking:trainer_verify_pending')}?email={request.user.email}")
    request.session[ROLE_SESSION_KEY] = ROLE_TRAINER

    requested_tab = (request.GET.get("tab") or "availability").strip().lower()
    if requested_tab not in PORTAL_TABS:
        requested_tab = "availability"

    # Solo lectura por defecto; edición con ?edit=1
    edit_mode = request.GET.get("edit") == "1"

    # Qué formulario se envió (usamos input oculto `form_name`)
    posted_form_name = (request.POST.get("form_name") or "").strip().lower() if request.method == "POST" else ""
    # Fallback: si template no envió form_name, inferimos por payload.
    # Availability formsets siempre incluyen keys de ManagementForm.
    if request.method == "POST" and not posted_form_name:
        if any(k.startswith("availability-") for k in request.POST.keys()):
            posted_form_name = "availability"
        else:
            posted_form_name = "settings"

    # Formularios por defecto (sin bind) para GET y para el no-enviado en POST
    form = TrainerSettingsForm(instance=trainer)
    availability_formset = TrainerAvailabilityFormSet(instance=trainer, prefix="availability")

    if request.method == "POST":
        # En cualquier POST, mantener modo edición para mostrar validaciones.
        edit_mode = True

        if posted_form_name == "visibility":
            publish_requested = (request.POST.get("is_published") or "0").strip() == "1"
            readiness = _trainer_booking_readiness(trainer)
            if publish_requested and not readiness["ready"]:
                messages.error(
                    request,
                    "No puedes publicar tu perfil todavía. Completa perfil, disponibilidad y pagos primero.",
                )
                return redirect(_portal_url(tab="profile"))

            if trainer.active != publish_requested:
                trainer.active = publish_requested
                trainer.save(update_fields=["active"])

            if trainer.active:
                messages.success(request, "Tu perfil público está activo.")
            else:
                messages.info(request, "Tu perfil público quedó pausado.")
            return redirect(_portal_url(tab="profile"))

        if posted_form_name == "settings":
            form = TrainerSettingsForm(request.POST, instance=trainer)
            # IMPORTANTE: NO bindear availability formset al guardar settings,
            # o puede aparecer "ManagementForm data is missing".
            availability_formset = TrainerAvailabilityFormSet(instance=trainer, prefix="availability")

            if form.is_valid():
                form.save()
                messages.success(request, "Ajustes guardados.")
                return redirect(_portal_url(tab="profile"))
            messages.error(request, "Corrige los errores indicados.")

        elif posted_form_name == "availability":
            availability_formset = TrainerAvailabilityFormSet(
                request.POST,
                instance=trainer,
                prefix="availability",
            )
            # IMPORTANTE: NO bindear settings form al guardar availability.
            form = TrainerSettingsForm(instance=trainer)

            if availability_formset.is_valid():
                with transaction.atomic():
                    availability_formset.save()
                    # Sincronizar timeslots de semana actual y próxima para efecto inmediato.
                    today = timezone.localdate()
                    this_sunday = _week_start_sunday(today)
                    next_sunday = this_sunday + timedelta(days=7)
                    _maybe_sync_timeslots_for_week(trainer=trainer, week_start=this_sunday)
                    _maybe_sync_timeslots_for_week(trainer=trainer, week_start=next_sunday)
                messages.success(request, "Disponibilidad guardada.")
                return redirect(_portal_url(tab="availability"))
            messages.error(request, "Corrige los errores indicados.")

        elif posted_form_name == "client_note":
            client_id_raw = (request.POST.get("client_id") or "").strip()
            notes = (request.POST.get("trainer_notes") or "").strip()
            return_query = (request.POST.get("return_query") or "").strip()
            if client_id_raw.isdigit():
                target_client = Client.objects.filter(trainer=trainer, id=int(client_id_raw)).first()
                if not target_client:
                    messages.error(request, "Cliente no encontrado.")
                else:
                    target_client.trainer_notes = notes
                    target_client.save(update_fields=["trainer_notes"])
                    messages.success(request, f"Notas guardadas para {target_client.name}.")
            else:
                messages.error(request, "Cliente inválido.")

            base = reverse("booking:trainer_portal")
            qs = f"?{return_query}" if return_query else "?tab=clients"
            return redirect(f"{base}{qs}")

        else:
            # POST desconocido: evitar errores confusos.
            # Hacemos fallback a settings para reducir confusión de "refrescó pero no guardó".
            messages.error(request, "No pudimos identificar el formulario enviado. Inténtalo de nuevo.")
            return redirect(_portal_url(tab=requested_tab, edit=True))

    # --- Estado de Stripe Connect (UI) ---
    stripe_status = services.get_stripe_connect_status(trainer)

    # --- Agenda (próximas sesiones) ---
    today = timezone.localdate()
    upcoming_reservations = (
        Reservation.objects
        .select_related("client", "timeslot", "checkout")
        .filter(trainer=trainer, timeslot__date__gte=today)
        .order_by("timeslot__date", "timeslot__time")
    )
    agenda_client_email = (request.GET.get("agenda_client_email") or "").strip().lower()
    if agenda_client_email:
        upcoming_reservations = upcoming_reservations.filter(client__email__iexact=agenda_client_email)

    # Mantener el portal rápido: mostrar solo los próximos 25 ítems por defecto.
    upcoming_reservations = upcoming_reservations[:25]

    # --- Progreso de onboarding (UX SaaS) ---
    readiness = _trainer_booking_readiness(trainer)
    has_availability = readiness["has_availability"]
    has_profile_basics = readiness["has_profile_basics"]
    stripe_state = stripe_status.get("state") if isinstance(stripe_status, dict) else getattr(stripe_status, "state", None)
    stripe_connected = stripe_state == "connected"
    has_manual_payment = readiness["has_manual_payment"]

    onboarding_steps = [
        {
            "label": "Completar perfil y precio",
            "done": has_profile_basics,
            "hint": "Define tu nombre público y precio por sesión.",
            "url": _portal_url(tab="profile"),
            "cta": "Ir a perfil",
        },
        {
            "label": "Configurar disponibilidad",
            "done": has_availability,
            "hint": "Añade bloques semanales para que los clientes puedan reservar.",
            "url": _portal_url(tab="availability"),
            "cta": "Configurar disponibilidad",
        },
        {
            "label": "Configurar pagos",
            "done": stripe_connected or has_manual_payment,
            "hint": "Conecta Stripe o usa ATH Móvil para cobrar.",
            "url": _portal_url(tab="payments"),
            "cta": "Configurar pagos",
        },
        {
            "label": "Publicar perfil",
            "done": trainer.active,
            "hint": "Activa tu perfil para aparecer en el listado público.",
            "url": _portal_url(tab="profile"),
            "cta": "Gestionar publicación",
        },
    ]
    onboarding_completed = sum(1 for step in onboarding_steps if step["done"])
    onboarding_total = len(onboarding_steps)
    onboarding_percent = int((onboarding_completed / onboarding_total) * 100) if onboarding_total else 0
    onboarding_next_step = next((step for step in onboarding_steps if not step["done"]), None)

    # --- Clientes tab ---
    client_q = (request.GET.get("client_q") or "").strip()
    client_segment = (request.GET.get("client_segment") or "all").strip().lower()
    client_sort = (request.GET.get("client_sort") or "name_asc").strip().lower()
    client_page = (request.GET.get("client_page") or "1").strip()

    trainer_clients_qs, client_segment, client_sort = _trainer_clients_queryset(
        trainer=trainer,
        q=client_q,
        segment=client_segment,
        sort=client_sort,
        today=today,
    )
    clients_paginator = Paginator(trainer_clients_qs, 20)
    clients_page_obj = clients_paginator.get_page(client_page)
    clients_items = list(clients_page_obj.object_list)

    trainer_clients_base = _trainer_clients_queryset(
        trainer=trainer,
        q="",
        segment="all",
        sort="name_asc",
        today=today,
    )[0]
    clients_summary = {
        "total": trainer_clients_base.count(),
        "active": trainer_clients_base.filter(upcoming_reservations__gt=0).count(),
        "pending_manual": trainer_clients_base.filter(pending_manual_reservations__gt=0).count(),
        "with_reservations": trainer_clients_base.filter(total_reservations__gt=0).count(),
    }

    client_params = request.GET.copy()
    if "tab" in client_params:
        client_params.pop("tab")
    if "client_page" in client_params:
        client_params.pop("client_page")
    clients_querystring = client_params.urlencode()

    return render(
        request,
        "booking/trainer_portal.html",
        {
            "trainer": trainer,
            "form": form,
            "availability_formset": availability_formset,
            "stripe_status": stripe_status,
            "edit_mode": edit_mode,
            "active_tab": requested_tab,
            "upcoming_reservations": upcoming_reservations,
            "onboarding_steps": onboarding_steps,
            "onboarding_completed": onboarding_completed,
            "onboarding_total": onboarding_total,
            "onboarding_percent": onboarding_percent,
            "onboarding_next_step": onboarding_next_step,
            "onboarding_ready": readiness["ready"] and trainer.active,
            "agenda_client_email": agenda_client_email,
            "trainer_clients": clients_items,
            "client_q": client_q,
            "client_segment": client_segment,
            "client_sort": client_sort,
            "clients_summary": clients_summary,
            "clients_page_obj": clients_page_obj,
            "clients_querystring": clients_querystring,
        },
    )


@login_required
def trainer_clients_export_view(request):
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(request, "Hay múltiples perfiles de entrenador para este usuario.")
        return redirect(_portal_url(tab="clients"))
    if not trainer:
        messages.error(request, "No encontramos tu perfil de entrenador.")
        return redirect("booking:account_role_management")

    today = timezone.localdate()
    client_q = (request.GET.get("client_q") or "").strip()
    client_segment = (request.GET.get("client_segment") or "all").strip().lower()
    client_sort = (request.GET.get("client_sort") or "name_asc").strip().lower()

    clients_qs, _segment, _sort = _trainer_clients_queryset(
        trainer=trainer,
        q=client_q,
        segment=client_segment,
        sort=client_sort,
        today=today,
    )

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="clientes_{trainer.slug}_{today.isoformat()}.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "Nombre",
            "Email",
            "Telefono",
            "Total reservas",
            "Proximas reservas",
            "Pendientes manuales",
            "Proxima fecha",
            "Ultima fecha",
            "Notas",
        ]
    )
    for c in clients_qs:
        writer.writerow(
            [
                c.name,
                c.email,
                c.phone or "",
                c.total_reservations,
                c.upcoming_reservations,
                c.pending_manual_reservations,
                c.next_session_date.isoformat() if c.next_session_date else "",
                c.last_session_date.isoformat() if c.last_session_date else "",
                (c.trainer_notes or "").replace("\n", " ").strip(),
            ]
        )
    return response


# ====== Vistas de Stripe Connect ======

@login_required
def trainer_stripe_connect_start(request):
    """Inicia onboarding de Stripe Connect (Express) para el entrenador autenticado."""
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(
            request,
            "Hay múltiples perfiles de entrenador vinculados a tu usuario. Contacta a soporte/administración para corregirlo.",
        )
        return redirect(_portal_url(tab="payments"))

    if not trainer:
        messages.error(request, "Primero debes crear tu perfil de entrenador.")
        return redirect("booking:trainer_register")

    # Gate de aprobación (centralizado)
    if not services.is_trainer_approved(trainer):
        messages.info(
            request,
            "Tu perfil está pendiente de aprobación. Stripe estará disponible después de ser aprobado.",
        )
        return redirect(_portal_url(tab="payments"))

    # Gate de configuración de Stripe
    if not getattr(settings, "STRIPE_SECRET_KEY", ""):
        messages.error(request, "Stripe aún no está configurado (falta STRIPE_SECRET_KEY).")
        return redirect(_portal_url(tab="payments"))

    try:
        # 1) Crear/reusar cuenta conectada en Stripe y persistir ID en Trainer.
        services.create_or_get_connected_account(trainer)

        # 2) Crear enlace de onboarding y redirigir a Stripe.
        onboarding = services.create_account_onboarding_link(request=request, trainer=trainer)

        # Acepta múltiples formatos desde services: str URL | objeto Stripe .url | dict 'url'
        url = None
        if isinstance(onboarding, str):
            url = onboarding
        elif hasattr(onboarding, "url"):
            url = getattr(onboarding, "url")
        elif isinstance(onboarding, dict):
            url = onboarding.get("url")

        if not url:
            raise ValueError("No se pudo crear el enlace de onboarding de Stripe.")

        return redirect(url)

    except getattr(services, "ServiceUserError", Exception) as e:
        messages.error(request, getattr(e, "user_message", None) or str(e) or "No se pudo iniciar la conexión con Stripe.")
        return redirect(_portal_url(tab="payments"))

    except ValueError as e:
        messages.error(request, str(e) or "No se pudo crear el enlace de onboarding de Stripe.")
        return redirect(_portal_url(tab="payments"))

    except stripe.error.InvalidRequestError:
        messages.error(
            request,
            "Stripe Connect aún no está habilitado en esta cuenta de Stripe. "
            "Ve al panel de Stripe → Connect y actívalo (modo de prueba está bien), luego vuelve a intentarlo.",
        )
        return redirect(_portal_url(tab="payments"))

    except stripe.error.AuthenticationError:
        messages.error(
            request,
            "Falló la autenticación con Stripe. Verifica tu STRIPE_SECRET_KEY en .env (clave de prueba o producción).",
        )
        return redirect(_portal_url(tab="payments"))

    except stripe.error.StripeError:
        messages.error(request, "Stripe no está disponible temporalmente. Inténtalo de nuevo en unos minutos.")
        return redirect(_portal_url(tab="payments"))


@login_required
def trainer_stripe_connect_return(request):
    """URL de retorno tras onboarding. Reconsulta cuenta y marca onboarded si aplica."""
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(request, "Hay múltiples perfiles de entrenador vinculados a tu usuario. Contacta a soporte/administración para corregirlo.")
        return redirect(_portal_url(tab="payments"))

    if not trainer or not getattr(trainer, "stripe_account_id", ""):
        return HttpResponseBadRequest("No existe una cuenta de Stripe para este entrenador.")

    if not getattr(settings, "STRIPE_SECRET_KEY", ""):
        return HttpResponseBadRequest("Stripe no está configurado.")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        acct = stripe.Account.retrieve(trainer.stripe_account_id)
    except stripe.error.InvalidRequestError:
        messages.error(
            request,
            "No pudimos obtener tu cuenta de Stripe Connect. "
            "Asegúrate de que Stripe Connect esté habilitado en tu panel de Stripe e inténtalo de nuevo.",
        )
        return redirect(_portal_url(tab="payments"))
    except stripe.error.AuthenticationError:
        messages.error(request, "Falló la autenticación con Stripe. Verifica tu STRIPE_SECRET_KEY en .env (clave de prueba o producción).")
        return redirect(_portal_url(tab="payments"))
    except stripe.error.StripeError:
        messages.error(request, "Stripe no está disponible temporalmente. Inténtalo de nuevo en unos minutos.")
        return redirect(_portal_url(tab="payments"))

    details_submitted = bool(getattr(acct, "details_submitted", False))
    payouts_enabled = bool(getattr(acct, "payouts_enabled", False))

    if details_submitted and payouts_enabled:
        trainer.stripe_onboarded = True
        trainer.save(update_fields=["stripe_onboarded"])
        messages.success(request, "Stripe se conectó correctamente.")
    else:
        messages.info(request, "El onboarding de Stripe aún no está completo. Finaliza los pasos pendientes.")

    return redirect(_portal_url(tab="payments"))


@login_required
def trainer_stripe_connect_refresh(request):
    """URL de refresco si el entrenador necesita reiniciar onboarding."""
    messages.info(request, "Intentemos conectar Stripe nuevamente.")
    return redirect("booking:trainer_stripe_connect_start")


def trainer_register_view(request):
    """Página pública de registro de entrenador (/trainer/register/)."""
    from .forms import TrainerRegisterForm

    if request.user.is_authenticated:
        has_trainer, has_client = _resolve_account_roles(request.user)
        if has_trainer:
            _set_active_role_session(request, ROLE_TRAINER)
            return redirect("booking:trainer_portal")
        if has_client:
            messages.info(request, "Tu cuenta ya existe como cliente. Activa el perfil de entrenador desde Mi cuenta.")
            return redirect("booking:account_role_management")
        return redirect("booking:account_role_management")

    if request.method == "POST":
        form = TrainerRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            trainer = getattr(user, "trainer_profile", None)
            require_email_verification = _email_verification_is_required()

            if require_email_verification:
                try:
                    if user.email and trainer:
                        _send_trainer_verification_email(request, user, trainer)
                except Exception:
                    logger.exception("No se pudo enviar email de verificacion al registrar trainer user_id=%s", user.pk)
                    messages.warning(
                        request,
                        "Tu cuenta fue creada, pero no pudimos enviar el correo de verificación. Puedes reenviarlo desde la pantalla de acceso.",
                    )

                messages.success(
                    request,
                    "Cuenta creada. Te enviamos un correo para confirmar tu cuenta.",
                )
                return redirect(f"{reverse('booking:trainer_verify_pending')}?email={user.email}")

            if trainer and not trainer.email_verified:
                trainer.email_verified = True
                trainer.email_verified_at = timezone.now()
                trainer.save(update_fields=["email_verified", "email_verified_at"])

            messages.success(
                request,
                "Cuenta creada. Inicia sesión para acceder a tu panel de entrenador.",
            )
            login_url = reverse("login")
            next_url = reverse("booking:trainer_portal")
            return redirect(f"{login_url}?next={next_url}")

        # Mensaje superior amigable; los errores de campo siguen mostrando en formulario.
        if form.errors.get("email"):
            messages.error(request, "Ese correo ya está registrado. Inicia sesión y activa el rol desde Mi cuenta.")
        else:
            messages.error(request, "Corrige los errores indicados.")
    else:
        form = TrainerRegisterForm()

    return render(request, "booking/trainer_register.html", {"form": form})


def client_register_view(request):
    """Registro público de cliente."""
    from .forms import ClientRegisterForm

    if request.user.is_authenticated:
        has_trainer, has_client = _resolve_account_roles(request.user)
        if has_client:
            _set_active_role_session(request, ROLE_CLIENT)
            return redirect("booking:client_portal_dashboard")
        if has_trainer:
            messages.info(request, "Tu cuenta ya existe como entrenador. Activa el perfil de cliente desde Mi cuenta.")
            return redirect("booking:account_role_management")
        return redirect("booking:account_role_management")

    if request.method == "POST":
        form = ClientRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            profile = ClientProfile.objects.filter(user=user, active=True).first()
            require_email_verification = _client_email_verification_is_required()

            if require_email_verification:
                try:
                    if user.email and profile:
                        _send_client_verification_email(request, user, profile)
                except Exception:
                    logger.exception("No se pudo enviar email de verificacion cliente user_id=%s", user.pk)
                    messages.warning(
                        request,
                        "Tu cuenta fue creada, pero no pudimos enviar el correo de verificación. Puedes reenviarlo desde la pantalla de acceso.",
                    )
                messages.success(request, "Cuenta de cliente creada. Te enviamos un correo para confirmar tu cuenta.")
                return redirect(f"{reverse('booking:client_verify_pending')}?email={user.email}")

            if profile and not profile.email_verified:
                profile.email_verified = True
                profile.email_verified_at = timezone.now()
                profile.save(update_fields=["email_verified", "email_verified_at"])
            messages.success(request, "Cuenta de cliente creada. Inicia sesión para ver tu dashboard.")
            login_url = reverse("login")
            next_url = reverse("booking:client_portal_dashboard")
            return redirect(f"{login_url}?next={next_url}")
        if form.errors.get("email"):
            messages.error(request, "Ese correo ya está registrado. Inicia sesión y activa el rol de cliente desde Mi cuenta.")
        else:
            messages.error(request, "Corrige los errores del formulario.")
    else:
        form = ClientRegisterForm()

    return render(request, "booking/client_register.html", {"form": form})


@login_required
def client_dashboard_view(request):
    """Dashboard de cliente con historial de reservas."""
    profile = ClientProfile.objects.filter(user=request.user, active=True).first()
    if not profile:
        messages.info(request, "Primero activa tu perfil de cliente en tu cuenta.")
        return redirect("booking:account_role_management")
    if _client_email_verification_is_required() and not profile.email_verified:
        messages.warning(request, "Debes verificar tu correo para entrar al portal de cliente.")
        return redirect(f"{reverse('booking:client_verify_pending')}?email={request.user.email}")
    request.session[ROLE_SESSION_KEY] = ROLE_CLIENT

    if request.method == "POST":
        form_name = (request.POST.get("form_name") or "").strip().lower()
        if form_name == "add_dependent":
            full_name = (request.POST.get("dependent_full_name") or "").strip()
            relationship = (request.POST.get("dependent_relationship") or "").strip()
            if not full_name:
                messages.error(request, "Escribe el nombre del dependiente.")
            else:
                dep, created = ClientDependent.objects.get_or_create(
                    profile=profile,
                    full_name=full_name,
                    defaults={"relationship": relationship, "active": True},
                )
                if not created:
                    dep.relationship = relationship
                    dep.active = True
                    dep.save(update_fields=["relationship", "active"])
                    messages.success(request, "Dependiente actualizado.")
                else:
                    messages.success(request, "Dependiente añadido.")
            return redirect(f"{reverse('booking:client_portal_dashboard')}?tab=dependents")
        if form_name == "remove_dependent":
            dep_id = (request.POST.get("dependent_id") or "").strip()
            if dep_id.isdigit():
                deleted, _ = ClientDependent.objects.filter(profile=profile, id=int(dep_id)).delete()
                if deleted:
                    messages.success(request, "Dependiente eliminado.")
                else:
                    messages.error(request, "No encontramos ese dependiente.")
            else:
                messages.error(request, "Dependiente inválido.")
            return redirect(f"{reverse('booking:client_portal_dashboard')}?tab=dependents")

    # Vincular registros Client históricos por email al usuario autenticado.
    user_email = (request.user.email or "").strip().lower()
    if user_email:
        Client.objects.filter(user__isnull=True, email__iexact=user_email).update(user=request.user)

    active_tab = (request.GET.get("tab") or "upcoming").strip().lower()
    if active_tab not in {"upcoming", "history", "dependents"}:
        active_tab = "upcoming"

    reservations = (
        Reservation.objects
        .select_related("trainer", "timeslot", "checkout", "client")
        .filter(client__user=request.user)
    )
    upcoming_reservations = list(
        reservations
        .filter(timeslot__date__gte=timezone.localdate())
        .order_by("timeslot__date", "timeslot__time")[:50]
    )
    past_qs = reservations.filter(timeslot__date__lt=timezone.localdate())

    history_q = (request.GET.get("history_q") or "").strip()
    history_payment = (request.GET.get("history_payment") or "all").strip().lower()
    history_sort = (request.GET.get("history_sort") or "newest").strip().lower()
    history_from = parse_date((request.GET.get("history_from") or "").strip())
    history_to = parse_date((request.GET.get("history_to") or "").strip())

    if history_q:
        past_qs = past_qs.filter(
            Q(trainer__business_name__icontains=history_q)
            | Q(attendee_name__icontains=history_q)
        )
    if history_payment == "paid":
        past_qs = past_qs.filter(paid=True)
    elif history_payment == "unpaid":
        past_qs = past_qs.filter(paid=False)
    if history_from:
        past_qs = past_qs.filter(timeslot__date__gte=history_from)
    if history_to:
        past_qs = past_qs.filter(timeslot__date__lte=history_to)

    if history_sort == "oldest":
        past_qs = past_qs.order_by("timeslot__date", "timeslot__time")
    else:
        history_sort = "newest"
        past_qs = past_qs.order_by("-timeslot__date", "-timeslot__time")

    history_page = (request.GET.get("history_page") or "1").strip()
    paginator = Paginator(past_qs, 20)
    history_page_obj = paginator.get_page(history_page)
    past_reservations = list(history_page_obj.object_list)
    history_params = request.GET.copy()
    history_params["tab"] = "history"
    if "history_page" in history_params:
        history_params.pop("history_page")
    history_querystring = history_params.urlencode()
    for r in upcoming_reservations:
        r.cancel_deadline_at = _client_cancel_deadline(r)
        r.can_manage_now = _client_can_manage_reservation(r)
    dependents = ClientDependent.objects.filter(profile=profile, active=True).order_by("full_name")

    return render(
        request,
        "booking/client_dashboard.html",
        {
            "profile": profile,
            "dependents": dependents,
            "active_tab": active_tab,
            "upcoming_reservations": upcoming_reservations,
            "past_reservations": past_reservations,
            "history_q": history_q,
            "history_payment": history_payment,
            "history_sort": history_sort,
            "history_from": (history_from.isoformat() if history_from else ""),
            "history_to": (history_to.isoformat() if history_to else ""),
            "history_page_obj": history_page_obj,
            "history_total_count": paginator.count,
            "history_querystring": history_querystring,
        },
    )


def _client_can_manage_reservation(reservation) -> bool:
    """True when reservation is still within trainer cancellation window."""
    if not reservation or not reservation.timeslot_id:
        return False
    deadline = _client_cancel_deadline(reservation)
    return bool(deadline and timezone.now() <= deadline)


def _client_cancel_deadline(reservation):
    """Cancel/reschedule deadline based on trainer cancellation policy."""
    if not reservation or not reservation.timeslot_id:
        return None
    trainer = reservation.trainer
    start_dt = datetime.combine(reservation.timeslot.date, reservation.timeslot.time)
    if timezone.is_naive(start_dt):
        start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())
    cutoff_hours = int(getattr(trainer, "cancellation_hours_before", 12) or 12)
    return start_dt - timedelta(hours=cutoff_hours)


def _client_dashboard_tab_url(tab: str = "upcoming") -> str:
    return f"{reverse('booking:client_portal_dashboard')}?tab={tab}"


@login_required
@require_POST
def client_cancel_reservation_view(request, reservation_id):
    """Permite al cliente cancelar su propia reserva con reglas de negocio."""
    profile = ClientProfile.objects.filter(user=request.user, active=True).first()
    if not profile:
        messages.error(request, "No encontramos tu perfil de cliente.")
        return redirect("booking:client_portal_access")

    reservation = (
        Reservation.objects
        .select_related("trainer", "checkout", "timeslot", "client")
        .filter(id=reservation_id, client__user=request.user)
        .first()
    )
    if not reservation:
        messages.error(request, "Esa reserva no existe o no te pertenece.")
        return redirect(_client_dashboard_tab_url("upcoming"))

    if not _client_can_manage_reservation(reservation):
        messages.error(
            request,
            "Esta reserva ya no se puede cancelar desde portal (fuera de ventana de cancelación).",
        )
        return redirect(_client_dashboard_tab_url("upcoming"))

    trainer = reservation.trainer
    checkout = reservation.checkout
    requires_refund = bool(
        checkout
        and checkout.payment_method == Reservation.PAYMENT_STRIPE
        and checkout.status == Checkout.STATUS_CONFIRMED
    )
    refunded = False

    if requires_refund:
        if not trainer.allow_stripe_refunds:
            messages.error(
                request,
                "Este entrenador no permite reembolsos automáticos en su política actual.",
            )
            return redirect(_client_dashboard_tab_url("upcoming"))
        if not getattr(settings, "STRIPE_SECRET_KEY", ""):
            messages.error(request, "Stripe no está configurado en este momento.")
            return redirect(_client_dashboard_tab_url("upcoming"))
        if not (checkout.stripe_payment_intent_id or "").strip():
            messages.error(request, "No encontramos la referencia de pago para reembolso.")
            return redirect(_client_dashboard_tab_url("upcoming"))

        try:
            refund_amount_stripe = int(
                services.to_stripe_amount(
                    reservation.amount_due,
                    (checkout.currency or trainer.currency or "USD"),
                )
            )
        except Exception:
            refund_amount_stripe = 0
        if refund_amount_stripe <= 0:
            messages.error(request, "No pudimos calcular el monto del reembolso.")
            return redirect(_client_dashboard_tab_url("upcoming"))

        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            refund_obj = stripe.Refund.create(
                payment_intent=checkout.stripe_payment_intent_id,
                amount=refund_amount_stripe,
                metadata={
                    "checkout_id": str(checkout.id),
                    "trainer_id": str(trainer.id),
                    "triggered_by": "client_portal",
                    "reservation_id": str(reservation.id),
                    "client_user_id": str(request.user.id),
                },
            )
            refund_id = refund_obj.get("id") if isinstance(refund_obj, dict) else getattr(refund_obj, "id", "")
            refund_amount_minor = refund_obj.get("amount") if isinstance(refund_obj, dict) else getattr(refund_obj, "amount", 0)
            refund_currency = (
                refund_obj.get("currency") if isinstance(refund_obj, dict) else getattr(refund_obj, "currency", "")
            ) or checkout.currency or trainer.currency or "USD"
            refund_status = (refund_obj.get("status") if isinstance(refund_obj, dict) else getattr(refund_obj, "status", "")) or ""
            decimal_amount = Decimal(str(refund_amount_minor or 0)) / Decimal("100")
            StripeRefundEvent.objects.create(
                trainer=trainer,
                checkout=checkout,
                reservation=reservation,
                refund_id=refund_id or "",
                payment_intent_id=checkout.stripe_payment_intent_id or "",
                amount=decimal_amount,
                currency=str(refund_currency).upper(),
                source=StripeRefundEvent.SOURCE_CLIENT_PORTAL,
                status=refund_status,
                metadata={
                    "reservation_id": reservation.id,
                    "checkout_id": str(checkout.id),
                    "triggered_by_user_id": request.user.id,
                },
            )
            refunded = True
        except stripe.error.StripeError:
            messages.error(request, "No pudimos procesar el reembolso ahora mismo.")
            return redirect(_client_dashboard_tab_url("upcoming"))

    with transaction.atomic():
        reservation.delete()
        if checkout and not checkout.reservations.exists():
            checkout.status = Checkout.STATUS_CANCELLED
            checkout.confirmed_at = None
            checkout.save(update_fields=["status", "confirmed_at"])

    if refunded:
        messages.success(request, "Reserva cancelada con reembolso procesado.")
    else:
        messages.success(request, "Reserva cancelada correctamente.")
    return redirect(_client_dashboard_tab_url("upcoming"))


@login_required
def client_reschedule_reservation_view(request, reservation_id):
    """Reprograma una reserva del cliente a otro horario del mismo trainer."""
    profile = ClientProfile.objects.filter(user=request.user, active=True).first()
    if not profile:
        messages.error(request, "No encontramos tu perfil de cliente.")
        return redirect("booking:client_portal_access")

    reservation = (
        Reservation.objects
        .select_related("trainer", "timeslot", "client")
        .filter(id=reservation_id, client__user=request.user)
        .first()
    )
    if not reservation:
        messages.error(request, "Esa reserva no existe o no te pertenece.")
        return redirect(_client_dashboard_tab_url("upcoming"))

    if not _client_can_manage_reservation(reservation):
        messages.error(
            request,
            "Esta reserva ya no se puede reprogramar desde portal (fuera de ventana de cancelación).",
        )
        return redirect(_client_dashboard_tab_url("upcoming"))

    trainer = reservation.trainer
    today = timezone.localdate()
    slots_qs = (
        TimeSlot.objects
        .filter(trainer=trainer, active=True, date__gte=today)
        .exclude(id=reservation.timeslot_id)
        .annotate(num_reservations=Count("reservations"))
        .order_by("date", "time")
    )
    available_slots = [s for s in slots_qs if s.num_reservations < s.capacity]

    if request.method == "POST":
        target_id = (request.POST.get("target_timeslot_id") or "").strip()
        target = next((s for s in available_slots if str(s.id) == target_id), None)
        if not target:
            messages.error(request, "Selecciona un horario válido para reprogramar.")
            return redirect("booking:client_reschedule_reservation", reservation_id=reservation.id)

        try:
            with transaction.atomic():
                reservation.timeslot = target
                reservation.save(update_fields=["timeslot"])
        except ValidationError:
            messages.error(request, "No se pudo mover la reserva. Intenta con otro horario.")
            return redirect("booking:client_reschedule_reservation", reservation_id=reservation.id)

        messages.success(request, "Reserva reprogramada correctamente.")
        return redirect(_client_dashboard_tab_url("upcoming"))

    return render(
        request,
        "booking/client_reschedule.html",
        {"reservation": reservation, "available_slots": available_slots},
    )


def trainer_verify_pending_view(request):
    if not _email_verification_is_required():
        return redirect(reverse("login") + f"?next={reverse('booking:trainer_portal')}")
    email = (request.GET.get("email") or "").strip()
    return render(request, "booking/trainer_verify_pending.html", {"email": email})


def trainer_verify_email_view(request):
    if not _email_verification_is_required():
        return redirect(reverse("login") + f"?next={reverse('booking:trainer_portal')}")
    token = (request.GET.get("token") or "").strip()
    if not token:
        messages.error(request, "El enlace de verificación no es válido.")
        return redirect("booking:trainer_verify_pending")

    max_age = int(getattr(settings, "TRAINER_VERIFY_EMAIL_MAX_AGE_SECONDS", 60 * 60 * 24 * 2))
    try:
        payload = signing.loads(token, salt=EMAIL_VERIFY_SALT, max_age=max_age)
    except signing.SignatureExpired:
        messages.error(request, "El enlace expiró. Solicita uno nuevo.")
        return redirect("booking:trainer_verify_pending")
    except signing.BadSignature:
        messages.error(request, "El enlace de verificación no es válido.")
        return redirect("booking:trainer_verify_pending")

    user_id = payload.get("uid")
    token_email = (payload.get("email") or "").strip().lower()
    if not user_id or not token_email:
        messages.error(request, "El enlace de verificación no es válido.")
        return redirect("booking:trainer_verify_pending")

    trainer = Trainer.objects.select_related("user").filter(user_id=user_id).first()
    if not trainer:
        messages.error(request, "No encontramos una cuenta asociada a este enlace.")
        return redirect("booking:trainer_register")

    user = trainer.user
    current_email = (user.email or "").strip().lower()
    if current_email != token_email:
        messages.error(request, "Este enlace no coincide con tu correo actual.")
        return redirect("booking:trainer_verify_pending")

    if trainer.email_verified:
        messages.info(request, "Tu correo ya estaba confirmado. Puedes iniciar sesión.")
    else:
        trainer.email_verified = True
        trainer.email_verified_at = timezone.now()
        trainer.save(update_fields=["email_verified", "email_verified_at"])
        try:
            _send_trainer_welcome_email(user, trainer)
        except Exception:
            pass
        messages.success(request, "Correo confirmado. Tu cuenta de entrenador ya está activa.")

    login_url = reverse("login")
    next_url = reverse("booking:trainer_portal")
    return redirect(f"{login_url}?next={next_url}")


@require_POST
def trainer_verify_resend_view(request):
    if not _email_verification_is_required():
        return redirect(reverse("login") + f"?next={reverse('booking:trainer_portal')}")
    email = (request.POST.get("email") or "").strip().lower()
    if not email:
        messages.error(request, "Comparte tu correo para reenviar la verificación.")
        return redirect("booking:trainer_verify_pending")

    trainer = Trainer.objects.select_related("user").filter(user__email__iexact=email).first()
    if not trainer:
        messages.info(request, "Si el correo existe, te enviaremos un nuevo enlace.")
        return redirect(f"{reverse('booking:trainer_verify_pending')}?email={email}")

    if trainer.email_verified:
        messages.info(request, "Ese correo ya está confirmado. Puedes iniciar sesión.")
        login_url = reverse("login")
        next_url = reverse("booking:trainer_portal")
        return redirect(f"{login_url}?next={next_url}")

    try:
        _send_trainer_verification_email(request, trainer.user, trainer)
        messages.success(request, "Te enviamos un nuevo enlace de verificación.")
    except Exception:
        logger.exception("No se pudo reenviar email de verificacion trainer_id=%s", trainer.pk)
        messages.error(
            request,
            "No pudimos enviar el correo ahora. Inténtalo de nuevo en unos minutos.",
        )

    return redirect(f"{reverse('booking:trainer_verify_pending')}?email={email}")


def client_verify_pending_view(request):
    if not _client_email_verification_is_required():
        return redirect(reverse("login") + f"?next={reverse('booking:client_portal_dashboard')}")
    email = (request.GET.get("email") or "").strip()
    return render(request, "booking/client_verify_pending.html", {"email": email})


def client_verify_email_view(request):
    if not _client_email_verification_is_required():
        return redirect(reverse("login") + f"?next={reverse('booking:client_portal_dashboard')}")
    token = (request.GET.get("token") or "").strip()
    if not token:
        messages.error(request, "El enlace de verificación no es válido.")
        return redirect("booking:client_verify_pending")

    max_age = int(getattr(settings, "CLIENT_VERIFY_EMAIL_MAX_AGE_SECONDS", 60 * 60 * 24 * 2))
    try:
        payload = signing.loads(token, salt=CLIENT_EMAIL_VERIFY_SALT, max_age=max_age)
    except signing.SignatureExpired:
        messages.error(request, "El enlace expiró. Solicita uno nuevo.")
        return redirect("booking:client_verify_pending")
    except signing.BadSignature:
        messages.error(request, "El enlace de verificación no es válido.")
        return redirect("booking:client_verify_pending")

    user_id = payload.get("uid")
    token_email = (payload.get("email") or "").strip().lower()
    if not user_id or not token_email:
        messages.error(request, "El enlace de verificación no es válido.")
        return redirect("booking:client_verify_pending")

    profile = ClientProfile.objects.select_related("user").filter(user_id=user_id, active=True).first()
    if not profile:
        messages.error(request, "No encontramos una cuenta de cliente asociada a este enlace.")
        return redirect("booking:client_portal_register")

    user = profile.user
    current_email = (user.email or "").strip().lower()
    if current_email != token_email:
        messages.error(request, "Este enlace no coincide con tu correo actual.")
        return redirect("booking:client_verify_pending")

    if profile.email_verified:
        messages.info(request, "Tu correo ya estaba confirmado. Puedes iniciar sesión.")
    else:
        profile.email_verified = True
        profile.email_verified_at = timezone.now()
        profile.save(update_fields=["email_verified", "email_verified_at"])
        try:
            _send_client_welcome_email(user, profile)
        except Exception:
            logger.exception("No se pudo enviar email de bienvenida cliente user_id=%s", user.pk)
        messages.success(request, "Correo confirmado. Tu cuenta de cliente ya está activa.")

    login_url = reverse("login")
    next_url = reverse("booking:client_portal_dashboard")
    return redirect(f"{login_url}?next={next_url}")


@require_POST
def client_verify_resend_view(request):
    if not _client_email_verification_is_required():
        return redirect(reverse("login") + f"?next={reverse('booking:client_portal_dashboard')}")
    email = (request.POST.get("email") or "").strip().lower()
    if not email:
        messages.error(request, "Comparte tu correo para reenviar la verificación.")
        return redirect("booking:client_verify_pending")

    profile = ClientProfile.objects.select_related("user").filter(user__email__iexact=email, active=True).first()
    if not profile:
        messages.info(request, "Si el correo existe, te enviaremos un nuevo enlace.")
        return redirect(f"{reverse('booking:client_verify_pending')}?email={email}")

    if profile.email_verified:
        messages.info(request, "Ese correo ya está confirmado. Puedes iniciar sesión.")
        login_url = reverse("login")
        next_url = reverse("booking:client_portal_dashboard")
        return redirect(f"{login_url}?next={next_url}")

    try:
        _send_client_verification_email(request, profile.user, profile)
        messages.success(request, "Te enviamos un nuevo enlace de verificación.")
    except Exception:
        logger.exception("No se pudo reenviar email de verificacion cliente profile_id=%s", profile.pk)
        messages.error(
            request,
            "No pudimos enviar el correo ahora. Inténtalo de nuevo en unos minutos.",
        )

    return redirect(f"{reverse('booking:client_verify_pending')}?email={email}")


@login_required
def trainer_dashboard_view(request):
    """Dashboard del entrenador (/trainer/dashboard/).

    En Fase 1, el portal funciona como dashboard.
    """
    return redirect("booking:trainer_portal")


@login_required
@require_POST
def trainer_confirm_manual_payment_view(request, reservation_id):
    """Confirma pago manual (ATH) desde agenda del trainer."""
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(request, "Hay múltiples perfiles de entrenador para este usuario.")
        return redirect(_portal_url(tab="agenda"))
    if not trainer:
        messages.error(request, "No encontramos tu perfil de entrenador.")
        return redirect("booking:account_role_management")

    reservation = (
        Reservation.objects
        .select_related("trainer", "checkout", "timeslot", "client")
        .filter(id=reservation_id, trainer=trainer)
        .first()
    )
    if not reservation:
        messages.error(request, "La reserva no existe o no te pertenece.")
        return redirect(_portal_url(tab="agenda"))

    checkout = reservation.checkout
    payment_method = (reservation.payment_method or "").upper()
    if checkout and (checkout.payment_method or "").upper():
        payment_method = (checkout.payment_method or "").upper()

    if payment_method != Reservation.PAYMENT_ATH:
        messages.info(
            request,
            "Esta reserva no está en modo de pago manual (ATH). Para tarjeta, la confirmación llega por Stripe.",
        )
        return redirect(_portal_url(tab="agenda"))

    now = timezone.now()
    with transaction.atomic():
        if checkout:
            Checkout.objects.filter(id=checkout.id).update(
                status=Checkout.STATUS_CONFIRMED,
                confirmed_at=now,
            )
            Reservation.objects.filter(checkout=checkout).update(
                paid=True,
                payment_method=Reservation.PAYMENT_ATH,
                payment_date=now,
            )
            checkout.refresh_from_db()
        else:
            Reservation.objects.filter(id=reservation.id).update(
                paid=True,
                payment_method=Reservation.PAYMENT_ATH,
                payment_date=now,
            )

    if checkout:
        try:
            _send_checkout_confirmation_email(checkout)
        except Exception:
            logger.exception("No se pudo enviar email de confirmacion manual checkout_id=%s", checkout.id)

    messages.success(request, "Pago manual confirmado y reserva actualizada.")
    return redirect(_portal_url(tab="agenda"))


@login_required
@require_POST
def trainer_cancel_reservation_view(request, reservation_id):
    """Cancela una reserva desde el portal del trainer.

    Regla actual:
    - Si es pago Stripe confirmado, solo permite cancelar con reembolso cuando
      `trainer.allow_stripe_refunds` está activo.
    - Si el checkout tiene múltiples sesiones, el reembolso de Stripe es parcial
      por el `amount_due` de la reserva cancelada.
    """
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(request, "Hay múltiples perfiles de entrenador para este usuario.")
        return redirect(_portal_url(tab="agenda"))
    if not trainer:
        messages.error(request, "No encontramos tu perfil de entrenador.")
        return redirect("booking:trainer_register")

    reservation = (
        Reservation.objects.select_related("trainer", "checkout", "timeslot", "client")
        .filter(id=reservation_id, trainer=trainer)
        .first()
    )
    if not reservation:
        messages.error(request, "La reserva no existe o no te pertenece.")
        return redirect(_portal_url(tab="agenda"))

    checkout = reservation.checkout

    requires_refund = bool(
        checkout
        and checkout.payment_method == Reservation.PAYMENT_STRIPE
        and checkout.status == Checkout.STATUS_CONFIRMED
    )

    if requires_refund and not trainer.allow_stripe_refunds:
        messages.error(
            request,
            "Tienes desactivados los reembolsos Stripe. Actívalos en Precios y reservas para cancelar esta sesión.",
        )
        return redirect(_portal_url(tab="profile"))

    if requires_refund and not getattr(settings, "STRIPE_SECRET_KEY", ""):
        messages.error(request, "Stripe no está configurado (falta STRIPE_SECRET_KEY).")
        return redirect(_portal_url(tab="agenda"))

    if requires_refund and not (checkout.stripe_payment_intent_id or "").strip():
        messages.error(
            request,
            "No encontramos el identificador de pago Stripe para esta reserva. "
            "Cancélala desde admin.",
        )
        return redirect(_portal_url(tab="agenda"))

    refunded = False
    if requires_refund:
        refund_amount_stripe = 0
        try:
            refund_amount_stripe = int(
                services.to_stripe_amount(
                    reservation.amount_due,
                    (checkout.currency or trainer.currency or "USD"),
                )
            )
        except Exception:
            refund_amount_stripe = 0

        if refund_amount_stripe <= 0:
            messages.error(
                request,
                "No pudimos calcular el monto de reembolso para esta reserva. Cancélala desde admin.",
            )
            return redirect(_portal_url(tab="agenda"))

        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            refund_obj = stripe.Refund.create(
                payment_intent=checkout.stripe_payment_intent_id,
                amount=refund_amount_stripe,
                metadata={
                    "checkout_id": str(checkout.id),
                    "trainer_id": str(trainer.id),
                    "triggered_by": "trainer_portal",
                    "reservation_id": str(reservation.id),
                },
            )
            refund_id = refund_obj.get("id") if isinstance(refund_obj, dict) else getattr(refund_obj, "id", "")
            refund_amount_minor = refund_obj.get("amount") if isinstance(refund_obj, dict) else getattr(refund_obj, "amount", 0)
            refund_currency = (
                refund_obj.get("currency") if isinstance(refund_obj, dict) else getattr(refund_obj, "currency", "")
            ) or checkout.currency or trainer.currency or "USD"
            refund_status = (refund_obj.get("status") if isinstance(refund_obj, dict) else getattr(refund_obj, "status", "")) or ""
            decimal_amount = Decimal(str(refund_amount_minor or 0)) / Decimal("100")
            StripeRefundEvent.objects.create(
                trainer=trainer,
                checkout=checkout,
                reservation=reservation,
                refund_id=refund_id or "",
                payment_intent_id=checkout.stripe_payment_intent_id or "",
                amount=decimal_amount,
                currency=str(refund_currency).upper(),
                source=StripeRefundEvent.SOURCE_TRAINER_PORTAL,
                status=refund_status,
                metadata={
                    "reservation_id": reservation.id,
                    "checkout_id": str(checkout.id),
                    "triggered_by_user_id": request.user.id,
                    "stripe_refund_raw": {
                        "id": refund_id or "",
                        "amount": int(refund_amount_minor or 0),
                        "currency": str(refund_currency),
                        "status": refund_status,
                    },
                },
            )
            refunded = True
        except stripe.error.StripeError:
            messages.error(
                request,
                "No se pudo procesar el reembolso en Stripe para esta sesión. Intenta de nuevo o cancela desde admin.",
            )
            return redirect(_portal_url(tab="agenda"))

    slot_label = f"{reservation.timeslot.date} {reservation.timeslot.time.strftime('%I:%M %p')}"
    client_name = reservation.client.name

    with transaction.atomic():
        reservation.delete()
        if checkout and not checkout.reservations.exists():
            checkout.status = Checkout.STATUS_CANCELLED
            checkout.confirmed_at = None
            checkout.save(update_fields=["status", "confirmed_at"])

    if refunded:
        if checkout and checkout.reservations.exists():
            messages.success(
                request,
                f"Reserva de {client_name} cancelada y reembolsada parcialmente ({slot_label}).",
            )
        else:
            messages.success(request, f"Reserva de {client_name} cancelada y reembolsada ({slot_label}).")
    else:
        messages.success(request, f"Reserva de {client_name} cancelada ({slot_label}).")

    return redirect(_portal_url(tab="agenda"))


def trainer_portal_exit_view(request):
    """Salir del portal y volver al inicio con sesión cerrada."""
    if request.user.is_authenticated:
        logout(request)
    request.session.pop("trainer_portal_mode", None)
    return redirect("booking:home_io")


class TrainerAwareLoginView(LoginView):
    """Login con redirección inteligente por rol."""

    template_name = "registration/login.html"
    redirect_authenticated_user = True

    @staticmethod
    def _auth_back_target(*, next_url: str, authenticated: bool):
        next_url = (next_url or "").strip()
        trainer_portal_path = reverse("booking:trainer_portal")
        client_dashboard_path = reverse("booking:client_portal_dashboard")

        if authenticated:
            if next_url.startswith(client_dashboard_path):
                return reverse("booking:client_portal_dashboard"), "Volver a mi portal de cliente"
            if next_url.startswith(trainer_portal_path):
                return reverse("booking:trainer_portal"), "Volver a mi portal de entrenador"
            return reverse("booking:account_portal_home"), "Volver a mi portal"

        if next_url.startswith(client_dashboard_path):
            return reverse("booking:client_portal_access"), "Volver a acceso de clientes"
        if next_url.startswith(trainer_portal_path):
            return reverse("booking:trainer_access"), "Volver a acceso de entrenadores"
        return reverse("booking:home_io"), "Volver al inicio"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        next_url = (self.request.GET.get("next") or self.request.POST.get("next") or "").strip()
        back_url, back_label = self._auth_back_target(
            next_url=next_url,
            authenticated=self.request.user.is_authenticated,
        )

        context["back_url"] = back_url
        context["back_label"] = back_label
        submitted_login = (self.request.POST.get("username") or "").strip().lower()
        if submitted_login and context.get("form") and context["form"].errors:
            user_exists = get_user_model().objects.filter(
                Q(username__iexact=submitted_login) | Q(email__iexact=submitted_login)
            ).exists()
            context["login_error_message"] = (
                "No encontramos una cuenta con ese correo/usuario."
                if not user_exists
                else "Contraseña incorrecta. Inténtalo nuevamente."
            )
        return context

    def post(self, request, *args, **kwargs):
        username = (request.POST.get("username") or "").strip().lower()
        max_attempts = int(getattr(settings, "AUTH_RATE_LIMIT_MAX_ATTEMPTS", 8) or 8)
        keys = _rate_limiter_keys("login", request=request, identifier=username)
        if _rate_limiter_is_blocked(keys, max_attempts=max_attempts):
            messages.error(request, "Demasiados intentos de inicio de sesión. Inténtalo en unos minutos.")
            return redirect("login")
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        username = (self.request.POST.get("username") or "").strip().lower()
        _rate_limiter_clear(_rate_limiter_keys("login", request=self.request, identifier=username))
        two_fa = UserTwoFactorAuth.objects.filter(user=user, is_enabled=True).first()
        if two_fa and _is_two_fa_globally_enabled():
            self.request.session[TWO_FA_PENDING_USER_KEY] = user.pk
            self.request.session[TWO_FA_PENDING_NEXT_KEY] = self.get_redirect_url() or ""
            try:
                _issue_two_fa_code(self.request, user)
            except Exception:
                logger.exception("No se pudo emitir código 2FA por email para user_id=%s", user.pk)
                messages.error(self.request, "No pudimos enviar tu código de seguridad. Inténtalo nuevamente.")
                return redirect("login")
            messages.info(self.request, "Te enviamos un código de seguridad por email para completar el acceso.")
            return redirect("booking:two_factor_verify")
        return super().form_valid(form)

    def form_invalid(self, form):
        username = (self.request.POST.get("username") or "").strip().lower()
        window_seconds = int(getattr(settings, "AUTH_RATE_LIMIT_WINDOW_SECONDS", 300) or 300)
        _rate_limiter_hit(
            _rate_limiter_keys("login", request=self.request, identifier=username),
            window_seconds=window_seconds,
        )
        return super().form_invalid(form)

    def get_success_url(self):
        return _post_login_redirect_url(self.request, self.request.user, next_url=self.get_redirect_url())


class PortalAwarePasswordResetView(PasswordResetView):
    template_name = "registration/password_reset_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        next_url = (self.request.GET.get("next") or self.request.POST.get("next") or "").strip()
        back_url, back_label = TrainerAwareLoginView._auth_back_target(
            next_url=next_url,
            authenticated=self.request.user.is_authenticated,
        )
        context["back_url"] = back_url
        context["back_label"] = back_label
        context["next"] = next_url
        return context

    def get_success_url(self):
        url = reverse("password_reset_done")
        next_url = (self.request.POST.get("next") or self.request.GET.get("next") or "").strip()
        if next_url:
            return f"{url}?next={next_url}"
        return url


class PortalAwarePasswordResetDoneView(PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        next_url = (self.request.GET.get("next") or "").strip()
        login_url = reverse("login")
        if next_url:
            login_url = f"{login_url}?next={next_url}"
        context["login_url"] = login_url
        context["next"] = next_url
        return context


class PortalAwarePasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"


class PortalAwarePasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"
