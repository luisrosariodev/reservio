from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import stripe

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone
from django.urls import reverse

from .models import Checkout, Client, Reservation, TimeSlot, Trainer, TrainerAvailability


# Currencies that do NOT use cents in Stripe (amounts are integers in major units)

ZERO_DECIMAL_CURRENCIES = {
    "bif",
    "clp",
    "djf",
    "gnf",
    "jpy",
    "kmf",
    "krw",
    "mga",
    "pyg",
    "rwf",
    "ugx",
    "vnd",
    "vuv",
    "xaf",
    "xof",
    "xpf",
}


class ServiceUserError(Exception):
    """An exception with a safe, user-facing message.

    Views can surface `user_message` directly to end users.
    """

    def __init__(self, user_message: str, *, debug_message: str | None = None):
        super().__init__(debug_message or user_message)
        self.user_message = user_message
        self.debug_message = debug_message or user_message


def _stripe_secret_key() -> str:
    """Return Stripe secret key (or empty string if missing)."""
    return str(getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()


def _is_placeholder_key(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return False
    return (
        v.endswith("_xxx")
        or "replace-with" in v
        or v in {"sk_live_xxx", "pk_live_xxx", "sk_test_xxx", "pk_test_xxx", "whsec_xxx"}
    )


def _require_stripe() -> None:
    """Raise a clear error if Stripe is not configured."""
    key = _stripe_secret_key()
    if (not key) or _is_placeholder_key(key):
        raise ServiceUserError(
            "Stripe no está configurado todavía. Intenta más tarde.",
            debug_message="Missing STRIPE_SECRET_KEY",
        )


def stripe_is_configured() -> bool:
    """Backwards/compat helper used by views."""
    return bool(_stripe_secret_key())


def stripe_set_api_key() -> None:
    """Backwards/compat helper used by views."""
    stripe.api_key = _stripe_secret_key()


def get_or_create_express_account_for_trainer(trainer: Trainer) -> str:
    """Alias for create_or_get_connected_account for older/newer view code."""
    return create_or_get_connected_account(trainer)


def create_account_onboarding_url(*, account_id: str, refresh_url: str, return_url: str) -> str:
    """Low-level helper: create an onboarding AccountLink URL for a connected account."""
    _require_stripe()
    stripe_set_api_key()

    link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return link.url


def create_account_login_link(*, account_id: str, redirect_url: str) -> str:
    """Create an Express dashboard login link for a connected account."""
    _require_stripe()
    stripe_set_api_key()

    link = stripe.Account.create_login_link(
        account_id,
        redirect_url=redirect_url,
    )
    return link.url


def get_platform_fee_percent(trainer: Trainer | None = None) -> Decimal:
    """Global platform fee percentage.

    Per-trainer override has priority.
    If trainer override is empty, defaults to 0.
    (No global fallback for trainer checkouts.)

    Values are clamped to [0, 100].
    """
    raw = 0
    if trainer is not None:
        raw = getattr(trainer, "platform_fee_percent_override", 0)
        if raw in (None, ""):
            raw = 0
    try:
        pct = Decimal(str(raw))
    except Exception:
        pct = Decimal("0")

    if pct < 0:
        pct = Decimal("0")
    if pct > 100:
        pct = Decimal("100")
    return pct


def to_stripe_amount(amount: Decimal, currency: str) -> int:
    """Convert a Decimal major-unit amount to Stripe integer amount."""
    cur = (currency or "USD").lower().strip()
    if cur in ZERO_DECIMAL_CURRENCIES:
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_platform_fee_amount(total_amount: Decimal, trainer: Trainer | None = None) -> Decimal:
    """Compute platform fee in major currency units (e.g., dollars)."""
    pct = get_platform_fee_percent(trainer=trainer)
    if pct <= 0:
        return Decimal("0")
    return (total_amount * (pct / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_trainer_approved(trainer: Trainer) -> bool:
    """Return True if trainer is allowed to sell/book."""
    if hasattr(trainer, "is_approved"):
        return bool(getattr(trainer, "is_approved"))
    return True


def is_trainer_stripe_ready(trainer: Trainer) -> bool:
    """Stripe Connect is considered ready when onboarding is finished and we have an account id."""
    return bool(getattr(trainer, "stripe_onboarded", False)) and bool(getattr(trainer, "stripe_account_id", ""))


@dataclass
class StripeConnectStatus:
    state: str  # not_connected | incomplete | connected | error
    message: str
    action_label: Optional[str] = None
    action_url_name: Optional[str] = None
    details: list[tuple[str, str]] | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "action_label": self.action_label,
            "action_url_name": self.action_url_name,
            "details": self.details or [],
        }


def get_stripe_connect_status(trainer: Trainer) -> dict:
    """Return a simple dict for templates: state/message/action + optional details."""
    status = StripeConnectStatus(
        state="not_connected",
        message="Conecta Stripe para recibir pagos en tu cuenta bancaria.",
        action_label="Conectar Stripe",
        action_url_name="booking:trainer_stripe_connect_start",
        details=[],
    )

    if not _stripe_secret_key():
        status.state = "error"
        status.message = "Stripe aún no está configurado en la plataforma."
        status.action_label = None
        status.action_url_name = None
        return status.as_dict()

    # If the trainer is not approved, do not allow Connect actions.
    # This prevents confusion where the button exists but the backend blocks the flow.
    if not is_trainer_approved(trainer):
        status.state = "error"
        status.message = "Tu perfil está pendiente de aprobación. Stripe se habilita cuando admin apruebe tu cuenta."
        status.action_label = None
        status.action_url_name = None
        return status.as_dict()

    if bool(getattr(trainer, "stripe_onboarded", False)):
        status.state = "connected"
        status.message = "Stripe está conectado."
        status.action_label = None
        status.action_url_name = None
        return status.as_dict()

    if bool(getattr(trainer, "stripe_account_id", "")):
        status.state = "incomplete"
        status.message = "La cuenta de Stripe fue creada. Falta completar el onboarding."
        status.action_label = "Terminar configuración"
        status.action_url_name = "booking:trainer_stripe_connect_start"

        stripe.api_key = _stripe_secret_key()
        try:
            acct = stripe.Account.retrieve(trainer.stripe_account_id)

            details_submitted = bool(getattr(acct, "details_submitted", False))
            charges_enabled = bool(getattr(acct, "charges_enabled", False))
            payouts_enabled = bool(getattr(acct, "payouts_enabled", False))

            status.details = [
                ("Details submitted", "Yes" if details_submitted else "No"),
                ("Charges enabled", "Yes" if charges_enabled else "No"),
                ("Payouts enabled", "Yes" if payouts_enabled else "No"),
            ]

            if details_submitted and payouts_enabled:
                status.state = "connected"
                status.message = "Stripe está conectado y listo para pagos."
                status.action_label = None
                status.action_url_name = None

                if not getattr(trainer, "stripe_onboarded", False):
                    trainer.stripe_onboarded = True
                    trainer.save(update_fields=["stripe_onboarded"])

        except stripe.error.InvalidRequestError:
            status.state = "error"
            status.message = "Stripe Connect no está habilitado en esta cuenta de Stripe o la cuenta conectada es inválida."
            status.action_label = None
            status.action_url_name = None
        except stripe.error.StripeError:
            status.state = "error"
            status.message = "No pudimos conectar con Stripe ahora mismo. Inténtalo nuevamente en unos minutos."
            status.action_label = None
            status.action_url_name = None

    return status.as_dict()


def create_or_get_connected_account(trainer: Trainer) -> str:
    """Create the connected (Express) account if missing, otherwise reuse it.

    Notes:
    - Stripe commonly requires a `country` when creating accounts.
    - We also pass an email when available to make onboarding smoother.
    """
    _require_stripe()
    stripe.api_key = _stripe_secret_key()

    existing = (getattr(trainer, "stripe_account_id", "") or "").strip()
    if existing:
        return existing

    # Best-effort defaults; keep them configurable via settings.
    country = (getattr(settings, "STRIPE_DEFAULT_COUNTRY", "US") or "US").strip().upper()

    # Try to attach an email if we can infer it.
    email = None
    user = getattr(trainer, "user", None)
    if user is not None:
        email = (getattr(user, "email", None) or "").strip() or None

    acct = stripe.Account.create(
        type="express",
        country=country,
        email=email,
        capabilities={
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        business_profile={
            # Helps Stripe show a recognizable name; safe if missing.
            "name": (getattr(trainer, "business_name", "") or "").strip() or None,
        },
    )

    trainer.stripe_account_id = acct.id
    trainer.save(update_fields=["stripe_account_id"])
    return acct.id


def create_account_onboarding_link(*, request, trainer: Trainer) -> str:
    """Create an onboarding link for Stripe Connect Express."""
    _require_stripe()

    # Hard gate: only approved trainers can start Stripe onboarding in the app.
    if not is_trainer_approved(trainer):
        raise ServiceUserError("Tu perfil de trainer aún no está aprobado. Activa tu perfil primero.")

    stripe.api_key = _stripe_secret_key()

    # Ensure the connected account exists first
    if not getattr(trainer, "stripe_account_id", ""):
        create_or_get_connected_account(trainer)

    refresh_url = request.build_absolute_uri(reverse("booking:trainer_stripe_connect_refresh"))
    return_url = request.build_absolute_uri(reverse("booking:trainer_stripe_connect_return"))

    return create_account_onboarding_url(
        account_id=trainer.stripe_account_id,
        refresh_url=refresh_url,
        return_url=return_url,
    )


def create_stripe_checkout_session(
    *,
    request,
    trainer: Trainer,
    client: Client,
    checkout: Checkout,
    unit_amount: Decimal,
    quantity: int,
    currency: str,
    week_param: str,
) -> str:
    """Create a Stripe Checkout session.

    Uses Stripe Connect destination charges so payouts go to the trainer.
    Platform fee is configured globally via settings.PLATFORM_FEE_PERCENT.
    """
    _require_stripe()
    stripe.api_key = _stripe_secret_key()

    # Hard gates: never allow card checkout unless trainer can be paid.
    if not is_trainer_approved(trainer):
        raise ServiceUserError(
            "Este trainer todavía no está aprobado para recibir reservas. Intenta con otro trainer o vuelve más tarde."
        )
    if not is_trainer_stripe_ready(trainer):
        raise ServiceUserError(
            "Este trainer aún no tiene Stripe conectado para cobrar. Intenta más tarde o elige otro trainer."
        )

    success_url = request.build_absolute_uri(reverse("booking:booking_success")) + f"?checkout_id={checkout.id}"

    wk = (week_param or "current").strip().lower()
    if wk not in {"current", "next"}:
        wk = "current"

    cancel_url = request.build_absolute_uri(reverse("booking:booking", kwargs={"slug": trainer.slug})) + f"?week={wk}"

    stripe_currency = (currency or "USD").lower().strip()
    unit_amount_stripe = to_stripe_amount(unit_amount, stripe_currency)

    # Compute platform fee on the total (unit * qty)
    qty_int = int(quantity)
    total_amount = (unit_amount * Decimal(qty_int)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    platform_fee_percent = get_platform_fee_percent(trainer=trainer)
    platform_fee_amount = compute_platform_fee_amount(total_amount, trainer=trainer)
    platform_fee_stripe = to_stripe_amount(platform_fee_amount, stripe_currency)

    if platform_fee_stripe < 0:
        platform_fee_stripe = 0

    # Safety: Stripe requires application_fee_amount <= total amount.
    max_fee = unit_amount_stripe * qty_int
    if platform_fee_stripe > max_fee:
        platform_fee_stripe = max_fee

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=client.email,
            metadata={"checkout_id": str(checkout.id), "trainer_slug": trainer.slug},
            line_items=[
                {
                    "price_data": {
                        "currency": stripe_currency,
                        "product_data": {"name": f"Training session — {trainer.business_name}"},
                        "unit_amount": unit_amount_stripe,
                    },
                    "quantity": qty_int,
                }
            ],
            payment_intent_data={
                "application_fee_amount": int(platform_fee_stripe),
                "transfer_data": {"destination": str(trainer.stripe_account_id)},
            },
        )
    except stripe.error.AuthenticationError as e:
        raise ServiceUserError(
            "Pago con tarjeta no disponible por configuración de Stripe. Intenta con ATH Móvil o más tarde.",
            debug_message=str(e),
        ) from e
    except stripe.error.StripeError as e:
        raise ServiceUserError(
            "No pudimos iniciar el pago con Stripe ahora mismo.",
            debug_message=str(e),
        ) from e

    trainer_net_amount = (total_amount - platform_fee_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    checkout.stripe_session_id = session.id
    checkout.platform_fee_percent_applied = platform_fee_percent
    checkout.platform_fee_amount = platform_fee_amount
    checkout.trainer_net_amount = trainer_net_amount
    checkout.save(
        update_fields=[
            "stripe_session_id",
            "platform_fee_percent_applied",
            "platform_fee_amount",
            "trainer_net_amount",
        ]
    )

    return session.url


# ==============================
# Availability → TimeSlot engine
# ==============================


def week_start_sunday(d: date) -> date:
    """Return the Sunday that starts the week containing `d`.

    Matches the logic used earlier in booking views (week starts Sunday).
    """
    # Python weekday(): Monday=0 ... Sunday=6
    # We want Sunday as start.
    offset = (d.weekday() + 1) % 7
    return d - timedelta(days=offset)


def week_dates(week_start: date) -> list[date]:
    """7 dates starting at week_start."""
    return [week_start + timedelta(days=i) for i in range(7)]


def _availability_weekday(a: TrainerAvailability) -> int:
    """Best-effort get weekday int (0=Mon..6=Sun) from a TrainerAvailability row."""
    # We support both common conventions:
    # - Python: Monday=0 .. Sunday=6
    # - Some UIs/store: Sunday=0 .. Saturday=6
    raw = None
    for field in ("weekday", "day_of_week", "dow"):
        if hasattr(a, field):
            try:
                raw = int(getattr(a, field))
                break
            except Exception:
                raw = None

    if raw is None:
        return 0

    raw = raw % 7

    # Heuristic: if the stored value uses Sunday=0, convert to Python weekday.
    # We assume Sunday=0 convention when the row explicitly indicates Sunday with 0.
    # Conversion: Sunday(0)->6, Monday(1)->0, ..., Saturday(6)->5
    if raw == 0 and getattr(a, "_uses_sunday0", False):
        return 6

    # If a setting exists, it overrides heuristic.
    base = (getattr(settings, "AVAILABILITY_WEEKDAY_BASE", "MON") or "MON").upper().strip()
    if base in {"SUN", "SUNDAY"}:
        return 6 if raw == 0 else (raw - 1)

    # Default: treat stored values as Python weekday (Mon=0..Sun=6)
    return raw


def _availability_active(a: TrainerAvailability) -> bool:
    for field in ("active", "is_active", "enabled"):
        if hasattr(a, field):
            return bool(getattr(a, field))
    return True


def _availability_start(a: TrainerAvailability) -> time:
    for field in ("start_time", "start"):
        if hasattr(a, field):
            return getattr(a, field)
    # Fallback
    return time(9, 0)


def _availability_end(a: TrainerAvailability) -> time:
    for field in ("end_time", "end"):
        if hasattr(a, field):
            return getattr(a, field)
    # Fallback
    return time(17, 0)



@dataclass(frozen=True)
class DesiredSlot:
    slot_date: date
    slot_time: time


# === Backwards-compatible trainer settings helpers ===

def _trainer_session_length_min(trainer: Trainer) -> int:
    """Read the trainer's session length in minutes (backwards compatible)."""
    for field in ("session_length_min", "slot_duration_minutes", "session_duration_min"):
        if hasattr(trainer, field):
            try:
                val = int(getattr(trainer, field) or 0)
                if val > 0:
                    return val
            except Exception:
                pass
    return 60


def _trainer_buffer_min(trainer: Trainer) -> int:
    """Read optional buffer between sessions in minutes (backwards compatible)."""
    for field in ("buffer_min", "buffer_minutes", "buffer_between_sessions_min"):
        if hasattr(trainer, field):
            try:
                val = int(getattr(trainer, field) or 0)
                if val >= 0:
                    return val
            except Exception:
                pass
    return 0


def _trainer_capacity_per_slot(trainer: Trainer) -> int:
    """Read capacity per slot (backwards compatible)."""
    for field in ("capacity_per_slot", "slot_capacity"):
        if hasattr(trainer, field):
            try:
                val = int(getattr(trainer, field) or 0)
                if val > 0:
                    return val
            except Exception:
                pass
    return 1


def compute_desired_slots_for_week(*, trainer: Trainer, week_start: date) -> list[DesiredSlot]:
    """Compute the desired (date,time) slots for a week from TrainerAvailability.

    - Uses trainer.session_length_min (fallback 60)
    - Uses trainer.buffer_min (fallback 0)
    - Allows multiple availability blocks per day (multiple rows)

    Returns a list (ordered) of DesiredSlot.
    """
    duration_min = _trainer_session_length_min(trainer)
    buffer_min = _trainer_buffer_min(trainer)

    days = week_dates(week_start)
    # Pull availability rows once
    av_rows = (
        TrainerAvailability.objects
        .filter(trainer=trainer)
        .order_by("id")
    )

    # Map weekday -> list[availability]
    by_wd: dict[int, list[TrainerAvailability]] = {i: [] for i in range(7)}
    for a in av_rows:
        if not _availability_active(a):
            continue
        wd = _availability_weekday(a)
        if wd in by_wd:
            by_wd[wd].append(a)

    desired: list[DesiredSlot] = []

    for d in days:
        wd = d.weekday()  # Monday=0..Sunday=6
        for a in by_wd.get(wd, []):
            start_t = _availability_start(a)
            end_t = _availability_end(a)

            start_dt = datetime.combine(d, start_t)
            end_dt = datetime.combine(d, end_t)

            # Skip invalid blocks
            if end_dt <= start_dt:
                continue

            cursor = start_dt
            step = timedelta(minutes=(duration_min + buffer_min))
            dur = timedelta(minutes=duration_min)

            while cursor + dur <= end_dt:
                desired.append(DesiredSlot(slot_date=d, slot_time=cursor.time()))
                cursor = cursor + step

    return desired


@transaction.atomic
def sync_timeslots_for_week(*, trainer: Trainer, week_start: date) -> dict:
    """Ensure TimeSlot rows for the computed weekly availability exist.

    Creates missing slots and deactivates obsolete ones (only if they have no reservations).

    Returns a summary dict useful for debugging/logging.
    """
    desired = compute_desired_slots_for_week(trainer=trainer, week_start=week_start)
    desired_set = {(s.slot_date, s.slot_time) for s in desired}

    week_end = week_start + timedelta(days=6)

    # NOTE: SQLite doesn't support row-level locks like Postgres; `select_for_update()`
    # can increase contention and lead to "database is locked" in dev. Keep it simple.
    existing_qs = (
        TimeSlot.objects
        .filter(trainer=trainer, date__gte=week_start, date__lte=week_end)
    )

    existing = list(existing_qs)
    existing_set = {(ts.date, ts.time) for ts in existing}

    to_create = [s for s in desired if (s.slot_date, s.slot_time) not in existing_set]
    to_deactivate_ids = [ts.id for ts in existing if (ts.date, ts.time) not in desired_set]

    # Reactivate slots that are desired but currently inactive.
    desired_but_inactive_ids: list[int] = []
    for ts in existing:
        if (ts.date, ts.time) in desired_set and not getattr(ts, "active", True):
            desired_but_inactive_ids.append(ts.id)

    # Capacity default comes from the trainer settings (fallback 1)
    capacity = _trainer_capacity_per_slot(trainer)
    duration_min = _trainer_session_length_min(trainer)

    # Create missing slots using bulk_create to reduce transaction time/locking.
    created_count = 0

    has_duration_field = False
    try:
        TimeSlot._meta.get_field("duration_minutes")
        has_duration_field = True
    except Exception:
        has_duration_field = False

    to_bulk = []
    for s in to_create:
        kwargs = {
            "trainer": trainer,
            "date": s.slot_date,
            "time": s.slot_time,
            "capacity": capacity,
            "active": True,
        }
        if has_duration_field:
            kwargs["duration_minutes"] = duration_min
        to_bulk.append(TimeSlot(**kwargs))

    if to_bulk:
        # If you have a unique constraint on (trainer, date, time), ignore_conflicts
        # prevents noisy IntegrityErrors when concurrent requests race.
        TimeSlot.objects.bulk_create(to_bulk, ignore_conflicts=True)
        created_count = len(to_bulk)

    # Reactivate desired slots that were previously deactivated.
    reactivated_count = 0
    if desired_but_inactive_ids:
        reactivated_count = TimeSlot.objects.filter(id__in=desired_but_inactive_ids, active=False).update(active=True)

    # Keep capacity aligned with trainer settings for all active slots in this week.
    # (Optional but very useful when trainer changes capacity_per_slot later.)
    TimeSlot.objects.filter(trainer=trainer, date__gte=week_start, date__lte=week_end, active=True).update(capacity=capacity)

    # Deactivate obsolete slots ONLY when they have no reservations.
    # Do this in bulk to avoid N queries and reduce SQLite lock issues.
    deactivated_count = 0
    skipped_deactivate_with_reservations = 0
    if to_deactivate_ids:
        slots_with_counts = (
            TimeSlot.objects
            .filter(id__in=to_deactivate_ids)
            .annotate(res_count=Count("reservations"))
            .values("id", "active", "res_count")
        )

        safe_ids: list[int] = []
        for row in slots_with_counts:
            if int(row.get("res_count") or 0) > 0:
                skipped_deactivate_with_reservations += 1
                continue
            # Only update if it is currently active
            if bool(row.get("active", True)):
                safe_ids.append(int(row["id"]))

        if safe_ids:
            updated = TimeSlot.objects.filter(id__in=safe_ids, active=True).update(active=False)
            deactivated_count = int(updated or 0)

    return {
        "week_start": str(week_start),
        "created": created_count,
        "reactivated": int(reactivated_count or 0),
        "deactivated": deactivated_count,
        "skipped_deactivate_with_reservations": skipped_deactivate_with_reservations,
        "desired_total": len(desired),
        "existing_total": len(existing),
    }


def available_timeslots_for_week(*, trainer: Trainer, week_start: date):
    """Queryset of available TimeSlot rows for the week.

    This assumes you have already called `sync_timeslots_for_week`.
    Returns slots that:
    - are active
    - are not full (reservations < capacity)
    - are not in the past

    Annotates `num_reservations`.
    """
    today = timezone.localdate()
    week_end = week_start + timedelta(days=6)

    qs = (
        TimeSlot.objects
        .filter(trainer=trainer, active=True)
        .filter(date__gte=max(today, week_start), date__lte=week_end)
        .annotate(num_reservations=Count("reservations"))
        .filter(num_reservations__lt=F("capacity"))
        .order_by("date", "time")
    )
    return qs

__all__ = [
    # Stripe/connect helpers
    "ServiceUserError",
    "stripe_is_configured",
    "stripe_set_api_key",
    "get_or_create_express_account_for_trainer",
    "create_account_onboarding_url",
    "create_account_login_link",
    "get_stripe_connect_status",
    "create_or_get_connected_account",
    "create_account_onboarding_link",
    "create_stripe_checkout_session",
    "get_platform_fee_percent",
    "compute_platform_fee_amount",
    "to_stripe_amount",
    "is_trainer_approved",
    "is_trainer_stripe_ready",
    # Availability/slots
    "week_start_sunday",
    "week_dates",
    "compute_desired_slots_for_week",
    "sync_timeslots_for_week",
    "available_timeslots_for_week",
]
