from datetime import timedelta
from decimal import Decimal

import stripe

from django.conf import settings
from django.db import transaction
from django.db.utils import IntegrityError
from django.db.models import Count, F
from django.core.exceptions import MultipleObjectsReturned
from django.http import HttpResponseBadRequest
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Checkout, Client, Reservation, TimeSlot, Trainer

from .forms import TrainerSettingsForm

# Availability editing (inline formset)
# Note: the formset must be defined in forms.py.
from .forms import TrainerAvailabilityFormSet

#
# Import services as a module to avoid ImportError during reloads when services.py changes.

from . import services as services

# ---- services safe-call helpers (avoid hard crashes during refactors) ----

def _services_has(name: str) -> bool:
    return hasattr(services, name) and callable(getattr(services, name))


def _maybe_sync_timeslots_for_week(*, trainer, week_start):
    if _services_has("sync_timeslots_for_week"):
        services.sync_timeslots_for_week(trainer=trainer, week_start=week_start)


def _available_timeslots_queryset(*, trainer, week_start):
    # Prefer services implementation; fallback to DB query.
    if _services_has("available_timeslots_for_week"):
        return services.available_timeslots_for_week(trainer=trainer, week_start=week_start)
    # Fallback: all active timeslots for trainer in that week.
    end_date = week_start + timedelta(days=6)
    return TimeSlot.objects.filter(trainer=trainer, active=True, date__gte=week_start, date__lte=end_date)

from django.core.exceptions import ValidationError


from django.contrib import messages
from django.contrib.auth.decorators import login_required

def _get_trainer_for_user(user):
    """Return the Trainer for the logged-in user.

    We keep this in one place so every trainer-only view behaves the same.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    try:
        return Trainer.objects.get(user=user)
    except Trainer.DoesNotExist:
        return None
    except MultipleObjectsReturned:
        # This shouldn't happen if Trainer.user is OneToOneField, but if data is dirty
        # we prefer to fail safely rather than showing the wrong trainer.
        return "__MULTIPLE__"


# Helper: week start (Sunday)
def _week_start_sunday(d):
    """Return the Sunday for the week that contains date `d` (local date)."""
    weekday = d.weekday()  # Mon=0 ... Sun=6
    return d - timedelta(days=(weekday + 1) % 7)


def _build_booking_context(*, trainer, week_param, form_data=None, error_message=None):
    """Shared context builder for the booking page.

    Keeps UI consistent when we need to re-render the form with a friendly error.
    """
    form_data = form_data or {}

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

    # Keep the UX rule: current week only shows from today forward.
    start_date = today if week_param == "current" else sunday

    # Ensure DB has the latest slots for the requested week.
    # (Idempotent: safe to call repeatedly.)
    _maybe_sync_timeslots_for_week(trainer=trainer, week_start=sunday)

    # Pull available slots from DB for that week.
    timeslots = (
        _available_timeslots_queryset(trainer=trainer, week_start=sunday)
        .filter(date__gte=start_date, date__lte=end_date)
        .order_by("date", "time")
    )

    dates_with_slots = {slot.date.isoformat() for slot in timeslots}

    ctx = {
        "trainer": trainer,
        "timeslots": timeslots,
        "week_dates": week_dates,
        "dates_with_slots": list(dates_with_slots),
        "form_data": form_data,
        "week_param": week_param,
        "week_range_label": f"{sunday.strftime('%m/%d/%y')} – {end_date.strftime('%m/%d/%y')}",
    }

    if error_message:
        ctx["error_message"] = error_message

    return ctx


def booking_view(request, slug):
    """Booking page: shows availability for current week or next week only."""
    trainer = get_object_or_404(Trainer, slug=slug, active=True)
    week_param = (request.GET.get("week") or "current")

    ctx = _build_booking_context(trainer=trainer, week_param=week_param, form_data={})
    return render(request, "booking/booking_form.html", ctx)


@require_POST
def create_checkout_view(request, slug):
    """POST-only: create a Checkout + reservations.

    - If payment_method == STRIPE, we will create a Stripe Checkout Session and redirect.
    - If payment_method == ATH, we will show the success page with QR/instructions (pending manual confirmation).
    """
    trainer = get_object_or_404(Trainer, slug=slug, active=True)
    week_param = (request.GET.get("week") or "current")

    def _redirect_back_to_booking(message: str, *, level: str = "error"):
        """Redirect back to the trainer booking form without doing DB queries inside a broken atomic transaction."""
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

    # 1) Read selected time slots
    timeslot_ids = request.POST.getlist("timeslot_ids")
    # de-duplicate while keeping order
    timeslot_ids = list(dict.fromkeys(timeslot_ids))
    if not timeslot_ids:
        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
            form_data={"name": request.POST.get("name", ""), "email": request.POST.get("email", ""), "phone": request.POST.get("phone", ""), "payment_method": request.POST.get("payment_method", "STRIPE")},
            error_message="Selecciona al menos un horario antes de reservar.",
        )
        return render(request, "booking/booking_form.html", ctx)

    # 2) Client info
    name = (request.POST.get("name") or "").strip()
    email = (request.POST.get("email") or "").strip().lower()
    phone = (request.POST.get("phone") or "").strip()
    if not name or not email:
        pm = (request.POST.get("payment_method") or Reservation.PAYMENT_STRIPE).strip().upper()
        if pm not in {Reservation.PAYMENT_STRIPE, Reservation.PAYMENT_ATH}:
            pm = Reservation.PAYMENT_STRIPE

        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
            form_data={"name": name, "email": email, "phone": phone, "payment_method": pm, "timeslot_ids": timeslot_ids},
            error_message="Completa tu nombre y email para continuar.",
        )
        return render(request, "booking/booking_form.html", ctx)

    # 3) Payment method
    payment_method = (request.POST.get("payment_method") or Reservation.PAYMENT_STRIPE).strip().upper()
    if payment_method not in {Reservation.PAYMENT_STRIPE, Reservation.PAYMENT_ATH}:
        payment_method = Reservation.PAYMENT_STRIPE

    # Guardrail: don't allow Stripe checkout for trainers that are not approved / not onboarded.
    # (Prevents clients from paying a trainer who isn't ready to receive payouts.)
    if payment_method == Reservation.PAYMENT_STRIPE and (
        (not services.is_trainer_approved(trainer)) or (not services.is_trainer_stripe_ready(trainer))
    ):
        reason = []
        if not services.is_trainer_approved(trainer):
            reason.append("este trainer aún no está aprobado")
        if not services.is_trainer_stripe_ready(trainer):
            reason.append("este trainer aún no ha conectado Stripe")

        ctx = _build_booking_context(
            trainer=trainer,
            week_param=week_param,
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

    # 4) Load & lock slots + create DB records inside a SHORT transaction
    today = timezone.localdate()

    try:
        with transaction.atomic():
            slots_qs = (
                TimeSlot.objects
                .select_for_update()
                .filter(trainer=trainer, active=True, id__in=timeslot_ids)
                .filter(date__gte=today)
                .annotate(num_reservations=Count("reservations"))
                .order_by("date", "time")
            )
            slots = list(slots_qs)

            # Ensure all requested IDs exist and belong to this trainer
            if len(slots) != len(timeslot_ids):
                ctx = _build_booking_context(
                    trainer=trainer,
                    week_param=week_param,
                    form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                    error_message="Uno o más horarios ya no están disponibles. Selecciona otros y vuelve a intentar.",
                )
                return render(request, "booking/booking_form.html", ctx)

            # Capacity check
            for s in slots:
                if s.num_reservations >= s.capacity:
                    ctx = _build_booking_context(
                        trainer=trainer,
                        week_param=week_param,
                        form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                        error_message="Uno de los horarios seleccionados se llenó. Selecciona otro horario.",
                    )
                    return render(request, "booking/booking_form.html", ctx)

            # 5) Get/Create client for this trainer
            client, _ = Client.objects.get_or_create(
                trainer=trainer,
                email=email,
                defaults={"name": name, "phone": phone},
            )
            # Keep client info updated
            changed = False
            if client.name != name:
                client.name = name
                changed = True
            if phone and getattr(client, "phone", "") != phone:
                client.phone = phone
                changed = True
            if changed:
                client.save()

            # Prevent duplicate booking: same client + same slot
            if Reservation.objects.filter(client=client, timeslot__in=slots).exists():
                ctx = _build_booking_context(
                    trainer=trainer,
                    week_param=week_param,
                    form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                    error_message="Ya tienes una reserva para uno de esos horarios. Selecciona un horario diferente.",
                )
                return render(request, "booking/booking_form.html", ctx)

            # 6) Compute total
            raw_price = getattr(trainer, "session_price", None)
            if raw_price is None:
                raw_price = getattr(trainer, "price_per_session", None)
            if raw_price is None:
                raw_price = Decimal("0")

            price = Decimal(str(raw_price))
            currency = getattr(trainer, "currency", None) or "USD"
            total_amount = (price * Decimal(len(slots))).quantize(Decimal("0.01"))

            if total_amount <= Decimal("0.00"):
                ctx = _build_booking_context(
                    trainer=trainer,
                    week_param=week_param,
                    form_data={"name": name, "email": email, "phone": phone, "payment_method": payment_method, "timeslot_ids": timeslot_ids},
                    error_message="Este trainer todavía no tiene precio configurado. Intenta más tarde.",
                )
                return render(request, "booking/booking_form.html", ctx)

            # 7) Create checkout (pending by default)
            checkout = Checkout.objects.create(
                trainer=trainer,
                client=client,
                payment_method=payment_method,
                status=Checkout.STATUS_PENDING,
                currency=currency,
                total_amount=total_amount,
            )

            # 8) Create reservations linked to checkout
            reservations = []
            for slot in slots:
                res = Reservation.objects.create(
                    trainer=trainer,
                    client=client,
                    timeslot=slot,
                    checkout=checkout,
                    amount_due=price,
                    payment_method=payment_method,
                    paid=False,
                )
                reservations.append(res)

    except (IntegrityError, ValidationError):
        # Keep this outside the transaction to avoid TransactionManagementError
        return _redirect_back_to_booking(
            "Ya tienes una reserva para uno de esos horarios (o alguien lo tomó justo ahora). Selecciona un horario diferente.",
            level="error",
        )

    # If Stripe is selected, create the Stripe Checkout Session OUTSIDE the DB transaction.
    if payment_method == Reservation.PAYMENT_STRIPE:
        try:
            # Services layer should raise user-friendly errors for common cases
            # (not approved, not onboarded, missing Stripe config, etc.)
            session_obj = services.create_stripe_checkout_session(
                request=request,
                trainer=trainer,
                client=client,
                checkout=checkout,
                unit_amount=price,
                quantity=len(slots),
                currency=currency,
                week_param=week_param,
            )

            # Accept multiple shapes: url string | dict with 'url' | object with .url
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
                raise ValueError("Stripe checkout session could not be created.")

            # Persist session id if we have it
            if session_id and getattr(checkout, "stripe_session_id", None) != session_id:
                Checkout.objects.filter(id=checkout.id).update(stripe_session_id=session_id)

            return redirect(session_url)

        except getattr(services, "ServiceUserError", Exception) as e:
            # Expected, user-facing errors from services.py
            user_msg = getattr(e, "user_message", None) or str(e) or "Este trainer todavía no está listo para recibir pagos con tarjeta."
            # Mark checkout cancelled to avoid dangling pending checkouts in dev.
            Checkout.objects.filter(id=checkout.id).update(status=Checkout.STATUS_CANCELLED)
            return _redirect_back_to_booking(
                f"Pago con tarjeta no disponible: {user_msg}. Selecciona ATH Móvil o intenta más tarde.",
                level="error",
            )

        except stripe.error.StripeError:
            # Stripe SDK errors (network, invalid params, etc.)
            Checkout.objects.filter(id=checkout.id).update(status=Checkout.STATUS_CANCELLED)
            return _redirect_back_to_booking(
                "No pudimos iniciar el pago con Stripe ahora mismo. Intenta de nuevo en unos segundos.",
                level="error",
            )

        except ValueError as e:
            Checkout.objects.filter(id=checkout.id).update(status=Checkout.STATUS_CANCELLED)
            msg = str(e) or "Este trainer todavía no está listo para recibir pagos con tarjeta."
            return _redirect_back_to_booking(
                f"Pago con tarjeta no disponible: {msg}. Selecciona ATH Móvil o intenta más tarde.",
                level="error",
            )

    # ATH (manual): show receipt/instructions (pending manual confirmation)
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
        },
    )


def booking_success_view(request):
    """Success page.

    For Stripe: user may land here immediately after payment, but the webhook is the source of truth.
    We still show the receipt-like page based on the Checkout.
    """
    checkout_id = (request.GET.get("checkout_id") or "").strip()
    if not checkout_id:
        return HttpResponseBadRequest("Missing checkout_id")

    checkout = get_object_or_404(Checkout, id=checkout_id)
    trainer = checkout.trainer
    client = checkout.client

    # DEV fallback: if Stripe webhooks aren't configured/received yet, try to
    # reconcile the payment state from Stripe when the user lands on success.
    # NOTE: Webhooks are still the source of truth in production.
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

            # Mark confirmed when Stripe indicates payment is completed.
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
        except stripe.error.StripeError:
            # Ignore and let the page render as pending; webhook may arrive later.
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
        },
    )


# Stripe webhook endpoint
@csrf_exempt
def stripe_webhook_view(request):
    """Stripe webhook endpoint.

    Webhooks are the source of truth for confirming Stripe payments.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    # Must be configured in .env and loaded into settings
    if not getattr(settings, "STRIPE_WEBHOOK_SECRET", ""):
        return HttpResponse(status=500)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    # Stripe SDK uses the secret key for API operations (not strictly needed for signature verification)
    stripe.api_key = getattr(settings, "STRIPE_SECRET_KEY", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse(status=400)

    event_type = event.get("type")

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
            # Idempotent: safe to call multiple times
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

    return HttpResponse(status=200)


# ====== PHASE 1: Public pages ======

def home_io_view(request):
    """Simple landing page (/)"""
    return render(request, "home_io.html")


def home_full_view(request):
    """Full landing page (/home/)"""
    return render(request, "home.html")

# Backwards-compatible alias if anything still references `home_view` (full home)
home_view = home_full_view


# --- Funnel page for trainers: choose Sign in vs Create account
def trainer_access_view(request):
    """Funnel page for trainers.

    This is the entry point when the user clicks “I’m a personal trainer” on home_io.
    It should NOT auto-send them to register; it offers:
    - Sign in
    - Create account
    """
    return render(request, "booking/trainer_access.html")


# Backwards/alternate name in case any URL was wired to `trainer_access`
trainer_access = trainer_access_view


def trainer_list_view(request):
    """Public trainer list page (/trainers/)"""
    trainers = Trainer.objects.filter(active=True).order_by("business_name")
    return render(request, "booking/trainer_list.html", {"trainers": trainers})


@login_required
def trainer_portal_view(request):
    """Trainer portal (/trainer/).

    This page is protected:
    - Must be authenticated
    - Must have a Trainer profile

    If the user is logged in but has no Trainer profile, we redirect them to register.

    UX:
    - Settings should be prefilled on GET (view-only unless ?edit=1)
    - Availability formset is saved independently to avoid ManagementForm errors.
    """
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(
            request,
            "Multiple trainer profiles are linked to your user. Please contact support/admin to fix this.",
        )
        return redirect("booking:home_io")
    if not trainer:
        messages.info(request, "Create your trainer profile first.")
        return redirect("booking:trainer_register")

    # View-only by default; enable editing with ?edit=1
    edit_mode = request.GET.get("edit") == "1"

    # Which form was submitted (we use a hidden input named `form_name`)
    posted_form_name = (request.POST.get("form_name") or "").strip().lower() if request.method == "POST" else ""
    # Fallback: if template didn't send form_name, infer which form was posted.
    # Availability formsets always include the ManagementForm keys.
    if request.method == "POST" and not posted_form_name:
        if any(k.startswith("availability-") for k in request.POST.keys()):
            posted_form_name = "availability"
        else:
            posted_form_name = "settings"

    # Default (unbound) forms for GET and for the non-submitted form in POST
    form = TrainerSettingsForm(instance=trainer)
    availability_formset = TrainerAvailabilityFormSet(instance=trainer, prefix="availability")

    if request.method == "POST":
        # When any form is posted, keep edit mode on so validation errors are visible.
        edit_mode = True

        if posted_form_name == "settings":
            form = TrainerSettingsForm(request.POST, instance=trainer)
            # IMPORTANT: do NOT bind the availability formset when saving settings,
            # otherwise you can get "ManagementForm data is missing".
            availability_formset = TrainerAvailabilityFormSet(instance=trainer, prefix="availability")

            if form.is_valid():
                form.save()
                messages.success(request, "Settings saved.")
                return redirect("booking:trainer_portal")
            messages.error(request, "Please fix the errors below.")

        elif posted_form_name == "availability":
            availability_formset = TrainerAvailabilityFormSet(
                request.POST,
                instance=trainer,
                prefix="availability",
            )
            # IMPORTANT: do NOT bind the settings form when saving availability.
            form = TrainerSettingsForm(instance=trainer)

            if availability_formset.is_valid():
                with transaction.atomic():
                    availability_formset.save()
                    # Sync timeslots for current and next week so clients see updates immediately.
                    today = timezone.localdate()
                    this_sunday = _week_start_sunday(today)
                    next_sunday = this_sunday + timedelta(days=7)
                    _maybe_sync_timeslots_for_week(trainer=trainer, week_start=this_sunday)
                    _maybe_sync_timeslots_for_week(trainer=trainer, week_start=next_sunday)
                messages.success(request, "Availability saved.")
                return redirect("booking:trainer_portal")
            messages.error(request, "Please fix the errors below.")

        else:
            # Unknown POST; avoid confusing errors.
            # We default to settings to reduce "it refreshed but didn't save" confusion.
            messages.error(request, "We couldn't detect which form you submitted. Please try again.")
            return redirect("booking:trainer_portal")

    # --- Stripe Connect status (for UI) ---
    stripe_status = services.get_stripe_connect_status(trainer)

    # --- Agenda (next sessions) ---
    today = timezone.localdate()
    upcoming_reservations = (
        Reservation.objects
        .select_related("client", "timeslot", "checkout")
        .filter(trainer=trainer, timeslot__date__gte=today)
        .order_by("timeslot__date", "timeslot__time")
    )

    # Keep the portal fast: show only the next 25 items by default.
    upcoming_reservations = upcoming_reservations[:25]

    return render(
        request,
        "booking/trainer_portal.html",
        {
            "trainer": trainer,
            "form": form,
            "availability_formset": availability_formset,
            "stripe_status": stripe_status,
            "edit_mode": edit_mode,
            "upcoming_reservations": upcoming_reservations,
        },
    )


# ====== Stripe Connect Views ======

@login_required
def trainer_stripe_connect_start(request):
    """Start Stripe Connect (Express) onboarding for the logged-in trainer."""
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(
            request,
            "Multiple trainer profiles are linked to your user. Please contact support/admin to fix this.",
        )
        return redirect("booking:trainer_portal")

    if not trainer:
        messages.error(request, "Create your trainer profile first.")
        return redirect("booking:trainer_register")

    # Approval gate (centralized)
    if not services.is_trainer_approved(trainer):
        messages.info(
            request,
            "Your profile is pending approval. Stripe connect will be available after approval.",
        )
        return redirect("booking:trainer_portal")

    # Stripe configuration gate
    if not getattr(settings, "STRIPE_SECRET_KEY", ""):
        messages.error(request, "Stripe is not configured yet (missing STRIPE_SECRET_KEY).")
        return redirect("booking:trainer_portal")

    try:
        # 1) Create/reuse connected account on Stripe and persist ID on our Trainer.
        services.create_or_get_connected_account(trainer)

        # 2) Create onboarding link and redirect user to Stripe.
        onboarding = services.create_account_onboarding_link(request=request, trainer=trainer)

        # Accept multiple shapes from services: url str | Stripe object with .url | dict with 'url'
        url = None
        if isinstance(onboarding, str):
            url = onboarding
        elif hasattr(onboarding, "url"):
            url = getattr(onboarding, "url")
        elif isinstance(onboarding, dict):
            url = onboarding.get("url")

        if not url:
            raise ValueError("Stripe onboarding link could not be created.")

        return redirect(url)

    except getattr(services, "ServiceUserError", Exception) as e:
        messages.error(request, getattr(e, "user_message", None) or str(e) or "Stripe connect could not be started.")
        return redirect("booking:trainer_portal")

    except ValueError as e:
        messages.error(request, str(e) or "Stripe onboarding link could not be created.")
        return redirect("booking:trainer_portal")

    except stripe.error.InvalidRequestError:
        messages.error(
            request,
            "Stripe Connect is not enabled on this Stripe account yet. "
            "Go to Stripe Dashboard → Connect and activate it (Test mode is OK), then try again.",
        )
        return redirect("booking:trainer_portal")

    except stripe.error.AuthenticationError:
        messages.error(
            request,
            "Stripe authentication failed. Double-check your STRIPE_SECRET_KEY in .env (test key vs live key).",
        )
        return redirect("booking:trainer_portal")

    except stripe.error.StripeError:
        messages.error(request, "Stripe is temporarily unavailable. Please try again in a moment.")
        return redirect("booking:trainer_portal")


@login_required
def trainer_stripe_connect_return(request):
    """Return URL after onboarding. We re-fetch account and mark onboarded if details are submitted."""
    trainer = _get_trainer_for_user(request.user)
    if trainer == "__MULTIPLE__":
        messages.error(request, "Multiple trainer profiles are linked to your user. Please contact support/admin to fix this.")
        return redirect("booking:trainer_portal")

    if not trainer or not getattr(trainer, "stripe_account_id", ""):
        return HttpResponseBadRequest("No Stripe account for this trainer.")

    if not getattr(settings, "STRIPE_SECRET_KEY", ""):
        return HttpResponseBadRequest("Stripe is not configured.")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        acct = stripe.Account.retrieve(trainer.stripe_account_id)
    except stripe.error.InvalidRequestError:
        messages.error(
            request,
            "We couldn't retrieve your Stripe Connect account. "
            "Make sure Stripe Connect is enabled in your Stripe Dashboard and try again.",
        )
        return redirect("booking:trainer_portal")
    except stripe.error.AuthenticationError:
        messages.error(request, "Stripe authentication failed. Double-check your STRIPE_SECRET_KEY in .env (test key vs live key).")
        return redirect("booking:trainer_portal")
    except stripe.error.StripeError:
        messages.error(request, "Stripe is temporarily unavailable. Please try again in a moment.")
        return redirect("booking:trainer_portal")

    details_submitted = bool(getattr(acct, "details_submitted", False))
    payouts_enabled = bool(getattr(acct, "payouts_enabled", False))

    if details_submitted and payouts_enabled:
        trainer.stripe_onboarded = True
        trainer.save(update_fields=["stripe_onboarded"])
        messages.success(request, "Stripe connected successfully.")
    else:
        messages.info(request, "Stripe onboarding not completed yet. Please finish the steps.")

    return redirect("booking:trainer_portal")


@login_required
def trainer_stripe_connect_refresh(request):
    """Refresh URL if the trainer needs to restart onboarding."""
    messages.info(request, "Let's try connecting Stripe again.")
    return redirect("booking:trainer_stripe_connect_start")


def trainer_register_view(request):
    """Public trainer registration page (/trainer/register/)."""
    from .forms import TrainerRegisterForm

    if request.method == "POST":
        form = TrainerRegisterForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                "Account created! Please sign in to access your trainer dashboard.",
            )
            # After registration, user must sign in; send them to login and then back to the trainer portal.
            login_url = reverse("login")
            next_url = reverse("booking:trainer_portal")
            return redirect(f"{login_url}?next={next_url}")

        # Show a friendlier top-level message; field errors will still render on the form.
        if form.errors.get("email"):
            messages.error(request, "That email is already registered. Please sign in instead.")
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = TrainerRegisterForm()

    return render(request, "booking/trainer_register.html", {"form": form})


@login_required
def trainer_dashboard_view(request):
    """Trainer dashboard (/trainer/dashboard/).

    For Phase 1, the portal page is the dashboard.
    """
    return redirect("booking:trainer_portal")