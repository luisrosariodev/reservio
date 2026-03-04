from django.contrib import admin, messages
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone
import stripe
import logging
from decimal import Decimal
from datetime import timedelta

from .models import (
    Checkout,
    Trainer,
    TrainerAvailability,
    TimeSlot,
    Client,
    ClientProfile,
    ClientDependent,
    Reservation,
    AdminAuditLog,
    StripeWebhookEvent,
    StripeRefundEvent,
    UserTwoFactorAuth,
)

logger = logging.getLogger(__name__)


class TrainerAvailabilityInline(admin.TabularInline):
    model = TrainerAvailability
    extra = 1


@admin.register(Trainer)
class TrainerAdmin(admin.ModelAdmin):
    change_list_template = "admin/booking/trainer/change_list.html"
    list_select_related = ("user",)
    list_display = (
        "business_name",
        "user_email",
        "slug",
        "ath_mobile_handle",
        "active",
        "email_verified",
        "is_approved",
        "stripe_onboarded",
        "platform_fee_percent_override",
        "allow_stripe_refunds",
    )
    list_filter = ("active", "email_verified", "is_approved", "stripe_onboarded", "allow_stripe_refunds")
    search_fields = ("business_name", "slug", "ath_mobile_handle", "user__email")
    readonly_fields = ("stripe_account_id",)
    prepopulated_fields = {"slug": ("business_name",)}
    ordering = ("business_name",)
    list_per_page = 50
    actions = (
        "approve_selected",
        "unapprove_selected",
        "publish_selected",
        "pause_selected",
        "mark_email_verified",
        "mark_email_unverified",
    )
    fieldsets = (
        ("Basic info", {
            "fields": ("user", "business_name", "slug", "ath_mobile_handle", "active", "email_verified", "email_verified_at")
        }),
        ("Stripe / Payments", {
            "fields": ("is_approved", "stripe_onboarded", "stripe_account_id", "platform_fee_percent_override", "allow_stripe_refunds"),
            "description": "Approve trainers before allowing Stripe Connect onboarding."
        }),
    )
    inlines = [TrainerAvailabilityInline]

    @admin.display(description="Email")
    def user_email(self, obj):
        return getattr(obj.user, "email", "")

    @admin.action(description="Aprobar entrenadores seleccionados")
    def approve_selected(self, request, queryset):
        to_approve = list(queryset.filter(is_approved=False).select_related("user"))
        trainer_ids = [t.id for t in to_approve]
        updated = Trainer.objects.filter(id__in=trainer_ids).update(is_approved=True) if trainer_ids else 0
        for trainer in to_approve:
            self._send_trainer_approved_email(trainer)
        self.message_user(request, f"Entrenadores aprobados: {updated}", level=messages.SUCCESS)

    @admin.action(description="Quitar aprobación a entrenadores seleccionados")
    def unapprove_selected(self, request, queryset):
        updated = queryset.update(is_approved=False)
        self.message_user(request, f"Aprobación removida: {updated}", level=messages.WARNING)

    def _send_trainer_approved_email(self, trainer):
        user = getattr(trainer, "user", None)
        to_email = (getattr(user, "email", "") or "").strip()
        if not to_email:
            return
        app_base_url = (getattr(settings, "APP_BASE_URL", "") or "").strip().rstrip("/")
        portal_path = "/trainer/"
        portal_url = f"{app_base_url}{portal_path}" if app_base_url else portal_path
        subject = "Tu cuenta de entrenador fue aprobada"
        body = (
            f"Hola {trainer.business_name},\n\n"
            "Tu perfil de entrenador en Reserv.io fue aprobado por el equipo administrador.\n"
            f"Ya puedes entrar a tu portal y continuar la configuración: {portal_url}\n\n"
            "Si no solicitaste esta cuenta, responde este correo."
        )
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[to_email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("No se pudo enviar email de aprobacion trainer_id=%s", trainer.id)

    def save_model(self, request, obj, form, change):
        previously_approved = False
        if change and obj.pk:
            previously_approved = bool(
                Trainer.objects.filter(pk=obj.pk, is_approved=True).exists()
            )
        super().save_model(request, obj, form, change)
        if obj.is_approved and not previously_approved:
            self._send_trainer_approved_email(obj)

    @admin.action(description="Publicar perfil seleccionado")
    def publish_selected(self, request, queryset):
        updated = queryset.update(active=True)
        self.message_user(request, f"Perfiles publicados: {updated}", level=messages.SUCCESS)

    @admin.action(description="Pausar perfil seleccionado")
    def pause_selected(self, request, queryset):
        updated = queryset.update(active=False)
        self.message_user(request, f"Perfiles pausados: {updated}", level=messages.INFO)

    @admin.action(description="Marcar correo como verificado")
    def mark_email_verified(self, request, queryset):
        now = timezone.now()
        updated = queryset.update(email_verified=True, email_verified_at=now)
        self.message_user(request, f"Correos verificados: {updated}", level=messages.SUCCESS)

    @admin.action(description="Marcar correo como NO verificado")
    def mark_email_unverified(self, request, queryset):
        updated = queryset.update(email_verified=False, email_verified_at=None)
        self.message_user(request, f"Correos marcados como no verificados: {updated}", level=messages.WARNING)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["admin_kpis"] = {
            "trainers_pending_approval": Trainer.objects.filter(is_approved=False).count(),
            "checkouts_pending": Checkout.objects.filter(status=Checkout.STATUS_PENDING).count(),
            "reservations_unpaid": Reservation.objects.filter(paid=False).count(),
        }
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("trainer", "date", "time", "capacity", "active")
    list_filter = ("trainer", "date", "active")
    search_fields = ("trainer__business_name",)
    date_hierarchy = "date"
    list_per_page = 50


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "trainer", "email", "phone", "user", "notes_preview")
    list_filter = ("trainer",)
    search_fields = ("name", "email", "phone", "trainer_notes", "trainer__business_name", "user__email")
    list_per_page = 50

    @admin.display(description="Notas")
    def notes_preview(self, obj):
        text = (obj.trainer_notes or "").strip()
        if not text:
            return "-"
        return text[:40] + ("..." if len(text) > 40 else "")


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "full_name", "phone", "email_verified", "active", "created_at")
    list_filter = ("email_verified", "active", "created_at")
    search_fields = ("user__email", "full_name", "phone")
    ordering = ("-created_at",)


@admin.register(ClientDependent)
class ClientDependentAdmin(admin.ModelAdmin):
    list_select_related = ("profile", "profile__user")
    list_display = ("full_name", "relationship", "profile", "profile_user_email", "active", "created_at")
    list_filter = ("active", "created_at")
    search_fields = ("full_name", "relationship", "profile__full_name", "profile__user__email")
    ordering = ("full_name",)
    list_per_page = 50

    @admin.display(description="Email cliente")
    def profile_user_email(self, obj):
        if obj.profile and obj.profile.user:
            return obj.profile.user.email
        return "-"


@admin.register(UserTwoFactorAuth)
class UserTwoFactorAuthAdmin(admin.ModelAdmin):
    list_select_related = ("user",)
    list_display = ("user", "is_enabled", "backup_codes_count", "last_verified_at", "updated_at")
    list_filter = ("is_enabled", "updated_at")
    search_fields = ("user__email", "user__username")
    readonly_fields = ("created_at", "updated_at", "last_verified_at")
    ordering = ("-updated_at",)

    @admin.display(description="Backup codes")
    def backup_codes_count(self, obj):
        return len(obj.backup_codes or [])


@admin.register(Checkout)
class CheckoutAdmin(admin.ModelAdmin):
    list_select_related = ("trainer", "client")
    list_display = (
        "short_id",
        "trainer",
        "client",
        "payment_method",
        "status",
        "total_amount",
        "platform_fee_amount",
        "trainer_net_amount",
        "created_at",
        "confirmed_at",
    )
    list_filter = ("status", "payment_method", "trainer")
    search_fields = (
        "id",
        "trainer__business_name",
        "client__name",
        "client__email",
        "stripe_session_id",
        "stripe_payment_intent_id",
    )
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    actions = ("mark_confirmed", "mark_pending", "mark_cancelled", "cancel_and_refund_stripe_if_allowed")

    @admin.display(description="Checkout")
    def short_id(self, obj):
        return str(obj.id)[:8]

    @admin.action(description="Marcar como CONFIRMED y reservas pagadas")
    def mark_confirmed(self, request, queryset):
        with transaction.atomic():
            now = timezone.now()
            count = 0
            for checkout in queryset:
                if checkout.status != Checkout.STATUS_CONFIRMED:
                    checkout.status = Checkout.STATUS_CONFIRMED
                    checkout.confirmed_at = now
                    checkout.save(update_fields=["status", "confirmed_at"])
                    Reservation.objects.filter(checkout=checkout).update(
                        paid=True,
                        payment_method=checkout.payment_method,
                        payment_date=now,
                    )
                    count += 1
        self.message_user(request, f"Checkouts confirmados: {count}", level=messages.SUCCESS)

    @admin.action(description="Marcar como PENDING")
    def mark_pending(self, request, queryset):
        with transaction.atomic():
            now = timezone.now()
            count = 0
            for checkout in queryset:
                if checkout.status != Checkout.STATUS_PENDING:
                    checkout.status = Checkout.STATUS_PENDING
                    checkout.confirmed_at = None
                    checkout.save(update_fields=["status", "confirmed_at"])
                    Reservation.objects.filter(checkout=checkout).update(
                        paid=False,
                        payment_date=None,
                    )
                    count += 1
        self.message_user(request, f"Checkouts pendientes: {count}", level=messages.INFO)

    @admin.action(description="Marcar como CANCELLED")
    def mark_cancelled(self, request, queryset):
        with transaction.atomic():
            now = timezone.now()
            count = 0
            for checkout in queryset:
                if checkout.status != Checkout.STATUS_CANCELLED:
                    checkout.status = Checkout.STATUS_CANCELLED
                    checkout.confirmed_at = None
                    checkout.save(update_fields=["status", "confirmed_at"])
                    Reservation.objects.filter(checkout=checkout).update(
                        paid=False,
                        payment_date=None,
                    )
                    count += 1
        self.message_user(request, f"Checkouts cancelados: {count}", level=messages.WARNING)

    @admin.action(description="Cancelar + reembolsar Stripe (según setting del trainer)")
    def cancel_and_refund_stripe_if_allowed(self, request, queryset):
        if not getattr(settings, "STRIPE_SECRET_KEY", ""):
            self.message_user(request, "Falta STRIPE_SECRET_KEY. No se pueden procesar reembolsos.", level=messages.ERROR)
            return

        stripe.api_key = settings.STRIPE_SECRET_KEY
        refunded = 0
        skipped = 0
        failed = 0

        for checkout in queryset.select_related("trainer", "client"):
            trainer = checkout.trainer
            if not trainer.allow_stripe_refunds:
                skipped += 1
                continue
            if checkout.payment_method != Checkout.PAYMENT_STRIPE:
                skipped += 1
                continue
            if checkout.status != Checkout.STATUS_CONFIRMED:
                skipped += 1
                continue
            if not (checkout.stripe_payment_intent_id or "").strip():
                skipped += 1
                continue

            try:
                refund_obj = stripe.Refund.create(
                    payment_intent=checkout.stripe_payment_intent_id,
                    metadata={
                        "checkout_id": str(checkout.id),
                        "trainer_id": str(trainer.id),
                        "triggered_by": "django_admin",
                    },
                )
                refund_id = refund_obj.get("id") if isinstance(refund_obj, dict) else getattr(refund_obj, "id", "")
                refund_amount_minor = refund_obj.get("amount") if isinstance(refund_obj, dict) else getattr(refund_obj, "amount", 0)
                refund_currency = (refund_obj.get("currency") if isinstance(refund_obj, dict) else getattr(refund_obj, "currency", "")) or checkout.currency or "USD"
                refund_status = (refund_obj.get("status") if isinstance(refund_obj, dict) else getattr(refund_obj, "status", "")) or ""
                decimal_amount = Decimal(str(refund_amount_minor or 0)) / Decimal("100")
                StripeRefundEvent.objects.create(
                    trainer=trainer,
                    checkout=checkout,
                    reservation=None,
                    refund_id=refund_id or "",
                    payment_intent_id=checkout.stripe_payment_intent_id or "",
                    amount=decimal_amount,
                    currency=str(refund_currency).upper(),
                    source=StripeRefundEvent.SOURCE_ADMIN,
                    status=refund_status,
                    metadata={
                        "triggered_by_user_id": request.user.id,
                        "checkout_id": str(checkout.id),
                        "stripe_refund_raw": {
                            "id": refund_id or "",
                            "amount": int(refund_amount_minor or 0),
                            "currency": str(refund_currency),
                            "status": refund_status,
                        },
                    },
                )
                with transaction.atomic():
                    checkout.status = Checkout.STATUS_CANCELLED
                    checkout.confirmed_at = None
                    checkout.save(update_fields=["status", "confirmed_at"])
                    Reservation.objects.filter(checkout=checkout).update(
                        paid=False,
                        payment_date=None,
                    )
                refunded += 1
            except stripe.error.StripeError as exc:
                failed += 1
                self.message_user(
                    request,
                    f"Checkout {checkout.id}: error de Stripe al reembolsar ({exc}).",
                    level=messages.ERROR,
                )

        if refunded:
            self.message_user(
                request,
                f"Reembolsos procesados: {refunded}",
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f"Omitidos: {skipped} (no Stripe, no confirmado, sin payment_intent o trainer con reembolso desactivado).",
                level=messages.WARNING,
            )
        if failed and not refunded:
            self.message_user(
                request,
                f"No se pudo reembolsar ninguno. Errores: {failed}.",
                level=messages.ERROR,
            )


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_select_related = ("trainer", "client", "timeslot", "checkout", "dependent")
    list_display = (
        "reference_code",
        "trainer",
        "client",
        "attendee_type",
        "attendee_name",
        "dependent",
        "timeslot",
        "payment_method",
        "amount_due",
        "paid",
        "payment_date",
        "checkout_status",
    )
    list_filter = ("trainer", "paid", "payment_method", "attendee_type", "timeslot__date")
    search_fields = (
        "reference_code",
        "client__name",
        "client__email",
        "trainer__business_name",
        "attendee_name",
        "attendee_key",
        "dependent__full_name",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    actions = ("mark_paid", "mark_unpaid")

    @admin.display(description="Checkout status")
    def checkout_status(self, obj):
        if not obj.checkout_id:
            return "-"
        return obj.checkout.status

    @admin.action(description="Marcar reservas como pagadas")
    def mark_paid(self, request, queryset):
        now = timezone.now()
        updated = queryset.update(paid=True, payment_date=now)
        self.message_user(request, f"Reservas marcadas como pagadas: {updated}", level=messages.SUCCESS)

    @admin.action(description="Marcar reservas como no pagadas")
    def mark_unpaid(self, request, queryset):
        updated = queryset.update(paid=False, payment_date=None)
        self.message_user(request, f"Reservas marcadas como no pagadas: {updated}", level=messages.WARNING)


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "model_name", "object_pk")
    list_filter = ("action", "model_name", "created_at")
    search_fields = ("action", "model_name", "object_pk", "object_repr", "actor__email")
    readonly_fields = ("created_at", "actor", "action", "model_name", "object_pk", "object_repr", "metadata")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "event_type", "event_id", "livemode", "processed_ok")
    list_filter = ("processed_ok", "livemode", "event_type", "received_at")
    search_fields = ("event_id", "event_type", "error_message")
    readonly_fields = ("received_at", "event_id", "event_type", "livemode", "processed_ok", "error_message", "payload")
    ordering = ("-received_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StripeRefundEvent)
class StripeRefundEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "refund_id", "trainer", "checkout", "amount", "currency", "source", "status")
    list_filter = ("source", "currency", "status", "created_at")
    search_fields = ("refund_id", "payment_intent_id", "trainer__business_name", "checkout__id")
    readonly_fields = (
        "created_at",
        "trainer",
        "checkout",
        "reservation",
        "refund_id",
        "payment_intent_id",
        "amount",
        "currency",
        "source",
        "status",
        "metadata",
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _period_start_from_key(period_key: str):
    now = timezone.now()
    if period_key == "7d":
        return now - timedelta(days=7)
    if period_key == "30d":
        return now - timedelta(days=30)
    if period_key == "90d":
        return now - timedelta(days=90)
    return None


def admin_finance_dashboard_view(request):
    period = (request.GET.get("period") or "30d").strip().lower()
    if period not in {"7d", "30d", "90d", "all"}:
        period = "30d"
    start_dt = _period_start_from_key(period)

    confirmed_qs = Checkout.objects.filter(status=Checkout.STATUS_CONFIRMED)
    if start_dt:
        confirmed_qs = confirmed_qs.filter(confirmed_at__gte=start_dt)

    pending_qs = Checkout.objects.filter(status=Checkout.STATUS_PENDING)
    cancelled_qs = Checkout.objects.filter(status=Checkout.STATUS_CANCELLED)
    refunds_qs = StripeRefundEvent.objects.all()
    if start_dt:
        pending_qs = pending_qs.filter(created_at__gte=start_dt)
        cancelled_qs = cancelled_qs.filter(created_at__gte=start_dt)
        refunds_qs = refunds_qs.filter(created_at__gte=start_dt)

    money_zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))

    confirmed_totals = confirmed_qs.aggregate(
        gross=Coalesce(Sum("total_amount"), money_zero),
        platform_fees=Coalesce(Sum("platform_fee_amount"), money_zero),
        trainer_net=Coalesce(Sum("trainer_net_amount"), money_zero),
        count=Coalesce(Count("id"), 0),
    )
    pending_totals = pending_qs.aggregate(
        amount=Coalesce(Sum("total_amount"), money_zero),
        count=Coalesce(Count("id"), 0),
    )
    cancelled_totals = cancelled_qs.aggregate(
        amount=Coalesce(Sum("total_amount"), money_zero),
        count=Coalesce(Count("id"), 0),
    )
    refunds_totals = refunds_qs.aggregate(
        amount=Coalesce(Sum("amount"), money_zero),
        count=Coalesce(Count("id"), 0),
    )

    top_trainers = (
        confirmed_qs.values("trainer_id", "trainer__business_name")
        .annotate(
            checkouts=Count("id"),
            gross=Coalesce(Sum("total_amount"), money_zero),
            platform_fees=Coalesce(Sum("platform_fee_amount"), money_zero),
            trainer_net=Coalesce(Sum("trainer_net_amount"), money_zero),
        )
        .order_by("-gross", "-checkouts")[:10]
    )

    net_after_refunds = (confirmed_totals["gross"] - refunds_totals["amount"]).quantize(Decimal("0.01"))

    context = {
        **admin.site.each_context(request),
        "title": "Dashboard financiero",
        "subtitle": "Resumen financiero de la plataforma",
        "period": period,
        "period_options": [
            {"key": "7d", "label": "Últimos 7 días"},
            {"key": "30d", "label": "Últimos 30 días"},
            {"key": "90d", "label": "Últimos 90 días"},
            {"key": "all", "label": "Todo"},
        ],
        "kpis": {
            "gross_confirmed": confirmed_totals["gross"],
            "platform_fees": confirmed_totals["platform_fees"],
            "trainer_net": confirmed_totals["trainer_net"],
            "refunds": refunds_totals["amount"],
            "net_after_refunds": net_after_refunds,
            "confirmed_count": confirmed_totals["count"],
            "pending_amount": pending_totals["amount"],
            "pending_count": pending_totals["count"],
            "cancelled_amount": cancelled_totals["amount"],
            "cancelled_count": cancelled_totals["count"],
            "refund_count": refunds_totals["count"],
        },
        "top_trainers": top_trainers,
    }
    return TemplateResponse(request, "admin/booking/finance_dashboard.html", context)


if not getattr(admin.site, "_booking_finance_urls_patched", False):
    _original_get_urls = admin.site.get_urls

    def _booking_get_urls():
        return [
            path(
                "booking/finance/",
                admin.site.admin_view(admin_finance_dashboard_view),
                name="booking_finance_dashboard",
            ),
        ] + _original_get_urls()

    admin.site.get_urls = _booking_get_urls
    admin.site._booking_finance_urls_patched = True
