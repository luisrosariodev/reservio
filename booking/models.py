from django.db import models
from django.utils.crypto import get_random_string
from django.utils.text import slugify
from django.utils import timezone
from datetime import timedelta, datetime
from django.conf import settings
from django.core.exceptions import ValidationError
import uuid
from decimal import Decimal


class Trainer(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trainer_profile",
    )
    business_name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, blank=True)

    # Booking settings
    slot_duration_minutes = models.PositiveIntegerField(default=60)
    buffer_minutes = models.PositiveIntegerField(
        default=0,
        help_text="Minutos de descanso/buffer entre sesiones (opcional).",
    )

    @property
    def slot_step_minutes(self) -> int:
        """Minutes between slot starts (duration + buffer)."""
        return int(self.slot_duration_minutes) + int(self.buffer_minutes or 0)

    # Precio por sesión (default). Ideal para MVP.
    session_price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("10.00"),
        help_text="Precio por sesión (por defecto).",
    )

    # Capacidad default por timeslot (si luego generas slots automáticamente desde disponibilidad)
    capacity_per_slot = models.PositiveIntegerField(
        default=5,
        help_text="Capacidad por slot (por defecto).",
    )

    # Pago
    ath_mobile_handle = models.CharField(
        max_length=100,
        help_text="Nombre/comercio en ATH Móvil",
    )
    ath_qr_image = models.ImageField(
        upload_to="ath_qr/",
        blank=True,
        null=True,
        help_text="Imagen del QR de ATH Móvil que verá el cliente para pagar.",
    )

    # Configuración general
    currency = models.CharField(max_length=10, default="USD")
    discount_code = models.CharField(
        max_length=50,
        blank=True,
        help_text="Código de descuento opcional para clientes (ej: VERANO10).",
    )
    discount_percent_off = models.PositiveSmallIntegerField(
        default=0,
        help_text="Porcentaje de descuento del código (0-100).",
    )
    discount_expires_on = models.DateField(
        null=True,
        blank=True,
        help_text="Fecha de expiración del cupón (opcional).",
    )
    discount_max_uses = models.PositiveIntegerField(
        default=0,
        help_text="Máximo de usos del cupón (0 = sin límite).",
    )

    booking_window_days = models.PositiveIntegerField(
        default=7,
        help_text="Cuántos días hacia adelante se puede reservar.",
    )
    cancellation_hours_before = models.PositiveIntegerField(
        default=12,
        help_text="Horas mínimas antes para poder cancelar/modificar.",
    )

    custom_instructions = models.TextField(
        blank=True,
        help_text="Texto que se mostrará al cliente al finalizar la reserva.",
    )
    show_qr_on_checkout = models.BooleanField(default=True)
    max_slots_per_week_per_client = models.PositiveIntegerField(
        default=7,
        help_text="Máx de sesiones por semana por cliente.",
    )

    active = models.BooleanField(default=True)
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(blank=True, null=True)

    # Stripe Connect (admin approves first)
    is_approved = models.BooleanField(
        default=False,
        help_text="Admin must approve the trainer before enabling Stripe onboarding.",
    )
    stripe_account_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Stripe Connect account id (acct_...).",
    )
    stripe_onboarded = models.BooleanField(
        default=False,
        help_text="True when Stripe onboarding is completed.",
    )
    platform_fee_percent_override = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Override de fee de plataforma para este trainer (ej: 10.00). Si está vacío, usa el global.",
    )
    allow_stripe_refunds = models.BooleanField(
        default=False,
        help_text="Permite reembolsos automáticos de pagos Stripe al cancelar reservas.",
    )

    def save(self, *args, **kwargs):
        # Auto-generate slug from business_name (trainer should NOT type it).
        if not self.slug and self.business_name:
            base = slugify(self.business_name)[:50] or "trainer"
            candidate = base
            i = 2
            while Trainer.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base}-{i}"
                i += 1
            self.slug = candidate

        super().save(*args, **kwargs)

    def __str__(self):
        return self.business_name


class TrainerAvailability(models.Model):
    WEEKDAYS = [
        (0, "Lunes"),
        (1, "Martes"),
        (2, "Miércoles"),
        (3, "Jueves"),
        (4, "Viernes"),
        (5, "Sábado"),
        (6, "Domingo"),
    ]

    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="availabilities")
    weekday = models.IntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    slot_capacity = models.PositiveIntegerField(
        default=5,
        help_text="Máx clientes por slot en este bloque.",
    )
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("trainer", "weekday", "start_time", "end_time")

    def clean(self):
        # Basic sanity check
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError("La hora de fin debe ser mayor que la hora de inicio.")

        # Default capacity from trainer setting
        if self.slot_capacity in (None, 0):
            self.slot_capacity = int(getattr(self.trainer, "capacity_per_slot", 1) or 1)

        # Prevent overlapping blocks on the same weekday for the same trainer
        # Overlap condition: start < other_end AND end > other_start
        if self.trainer_id and self.weekday is not None and self.start_time and self.end_time:
            overlap_qs = (
                TrainerAvailability.objects
                .filter(trainer_id=self.trainer_id, weekday=self.weekday, active=True)
                .exclude(pk=self.pk)
                .filter(start_time__lt=self.end_time, end_time__gt=self.start_time)
            )
            if overlap_qs.exists():
                raise ValidationError(
                    "Este bloque se solapa con otro horario existente para ese día. "
                    "Ajusta las horas para que no se crucen."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.trainer} - {self.get_weekday_display()} {self.start_time}-{self.end_time}"


class TimeSlot(models.Model):
    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="timeslots")
    date = models.DateField()
    time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override duration for this slot (minutes). If empty, uses trainer.slot_duration_minutes.",
    )
    capacity = models.PositiveIntegerField(
        default=0,
        help_text="Capacidad del slot. Si es 0, usa trainer.capacity_per_slot.",
    )
    active = models.BooleanField(default=True)

    def clean(self):
        # Ensure slot belongs to the trainer and has sensible defaults.
        if self.capacity in (None, 0):
            self.capacity = int(getattr(self.trainer, "capacity_per_slot", 1) or 1)
        if self.duration_minutes in (None, 0):
            # Keep DB row explicit once created; default to trainer setting.
            self.duration_minutes = int(getattr(self.trainer, "slot_duration_minutes", 60) or 60)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.trainer} - {self.date} {self.time}"

    @property
    def reservations_count(self):
        return self.reservations.count()

    @property
    def has_space(self):
        return self.reservations_count < self.capacity

    @property
    def end_datetime(self):
        """Hora de fin basada en la duracion del slot o la del trainer."""
        base = datetime.combine(self.date, self.time)
        minutes = self.duration_minutes or self.trainer.slot_duration_minutes
        duration = timedelta(minutes=minutes)
        return (base + duration).time()

    @property
    def spaces_left(self):
        return self.capacity - self.reservations_count

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trainer", "date", "time"],
                name="unique_timeslot_per_trainer_datetime",
            )
        ]


class Client(models.Model):
    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="clients")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_client_records",
    )
    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    trainer_notes = models.TextField(blank=True, default="")

    def __str__(self):
        return f"{self.name} ({self.trainer})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trainer", "email"],
                name="unique_client_email_per_trainer",
            )
        ]


class ClientProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="client_profile",
    )
    full_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(blank=True, null=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.full_name or self.user.email or f"Client {self.user_id}"


class UserTwoFactorAuth(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="two_factor_auth",
    )
    is_enabled = models.BooleanField(default=False)
    totp_secret = models.CharField(max_length=64, blank=True)
    backup_codes = models.JSONField(default=list, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Two-factor auth"
        verbose_name_plural = "Two-factor auth"

    def __str__(self):
        return f"2FA {self.user_id} ({'enabled' if self.is_enabled else 'disabled'})"


class ClientDependent(models.Model):
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="dependents",
    )
    full_name = models.CharField(max_length=200)
    relationship = models.CharField(max_length=80, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("full_name",)
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "full_name"],
                name="unique_dependent_name_per_profile",
            )
        ]

    def __str__(self):
        if self.relationship:
            return f"{self.full_name} ({self.relationship})"
        return self.full_name


class Checkout(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_CONFIRMED = "CONFIRMED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    PAYMENT_STRIPE = "STRIPE"
    PAYMENT_ATH = "ATH"

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_STRIPE, "Stripe (Card)"),
        (PAYMENT_ATH, "ATH Móvil"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="checkouts")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="checkouts")

    payment_method = models.CharField(
        max_length=10,
        choices=PAYMENT_METHOD_CHOICES,
        default=PAYMENT_STRIPE,
    )

    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )

    currency = models.CharField(max_length=10, default="USD")
    total_amount = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    applied_discount_code = models.CharField(max_length=50, blank=True)
    applied_discount_percent = models.PositiveSmallIntegerField(default=0)
    discount_amount = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    platform_fee_percent_applied = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    platform_fee_amount = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    trainer_net_amount = models.DecimalField(max_digits=9, decimal_places=2, default=0)

    # Stripe reconciliation (set when using Stripe)
    stripe_session_id = models.CharField(max_length=255, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmation_email_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Checkout {self.id} / {self.trainer} / {self.client} / {self.payment_method} / {self.status}"


class Reservation(models.Model):
    ATTENDEE_SELF = "SELF"
    ATTENDEE_DEPENDENT = "DEPENDENT"
    ATTENDEE_CHOICES = [
        (ATTENDEE_SELF, "Titular"),
        (ATTENDEE_DEPENDENT, "Dependiente"),
    ]

    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="reservations")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="reservations")
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name="reservations")
    checkout = models.ForeignKey(
        Checkout,
        on_delete=models.CASCADE,
        related_name="reservations",
        null=True,
        blank=True,
        help_text="Group of reservations for a single payment/checkout",
    )

    PAYMENT_STRIPE = "STRIPE"
    PAYMENT_ATH = "ATH"

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_STRIPE, "Stripe (Tarjeta)"),
        (PAYMENT_ATH, "ATH Móvil"),
    ]

    payment_method = models.CharField(
        max_length=10,
        choices=PAYMENT_METHOD_CHOICES,
        default=PAYMENT_STRIPE,
    )

    reference_code = models.CharField(max_length=50, unique=True, blank=True)
    attendee_type = models.CharField(max_length=12, choices=ATTENDEE_CHOICES, default=ATTENDEE_SELF)
    attendee_name = models.CharField(max_length=200, blank=True)
    attendee_key = models.CharField(max_length=64, default="self")
    dependent = models.ForeignKey(
        ClientDependent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations",
    )
    amount_due = models.DecimalField(max_digits=7, decimal_places=2)
    paid = models.BooleanField(default=False)
    payment_date = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["timeslot", "client", "attendee_key"],
                name="unique_reservation_per_client_timeslot_attendee",
            )
        ]

    def save(self, *args, **kwargs):
        # Generate reference_code before validation to avoid unique/blank edge cases.
        if not self.reference_code:
            random_part = get_random_string(6).upper()
            self.reference_code = f"T{self.trainer_id}-{random_part}"

        if self.attendee_type == self.ATTENDEE_DEPENDENT and self.dependent_id:
            self.attendee_key = f"dep:{self.dependent_id}"
            if not self.attendee_name:
                self.attendee_name = self.dependent.full_name
        else:
            self.attendee_type = self.ATTENDEE_SELF
            self.dependent = None
            self.attendee_key = "self"

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        attendee = self.attendee_name or "Titular"
        return f"{self.trainer} / {self.client} / {self.timeslot} / {attendee} / {self.reference_code}"

    def clean(self):
        # Validar que el timeslot pertenece al trainer de la reserva
        if self.timeslot.trainer_id != self.trainer_id:
            raise ValidationError("El timeslot no pertenece al trainer de la reserva.")
        if self.client.trainer_id != self.trainer_id:
            raise ValidationError("El cliente no pertenece al trainer de la reserva.")
        if self.attendee_type == self.ATTENDEE_DEPENDENT:
            if not self.dependent_id:
                raise ValidationError("Selecciona un dependiente válido para esta reserva.")
            if self.dependent.profile.user_id != self.client.user_id:
                raise ValidationError("El dependiente no pertenece al cliente de esta reserva.")
        elif self.dependent_id:
            raise ValidationError("No puedes asignar dependiente cuando el asistente es el titular.")
        if self.checkout_id:
            if self.checkout.trainer_id != self.trainer_id:
                raise ValidationError("El checkout no pertenece al trainer de la reserva.")
            if self.checkout.client_id != self.client_id:
                raise ValidationError("El checkout no pertenece al cliente de la reserva.")

        # Evitar overbooking
        if self.timeslot_id and not self.timeslot.has_space:
            raise ValidationError("Este timeslot ya está lleno.")

        # Evitar doble reserva del mismo asistente en el mismo timeslot.
        if self.timeslot_id and self.client_id:
            attendee_key = self.attendee_key or ("dep:%s" % self.dependent_id if self.dependent_id else "self")
            qs = Reservation.objects.filter(timeslot_id=self.timeslot_id, client_id=self.client_id, attendee_key=attendee_key)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("Este asistente ya tiene una reserva en este timeslot.")


class AdminAuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_admin_audit_logs",
    )
    action = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100, blank=True)
    object_pk = models.CharField(max_length=100, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.created_at} | {self.action} | {self.model_name} {self.object_pk}"


class StripeWebhookEvent(models.Model):
    event_id = models.CharField(max_length=255, unique=True, blank=True)
    event_type = models.CharField(max_length=255, blank=True)
    livemode = models.BooleanField(default=False)
    processed_ok = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-received_at",)

    def __str__(self):
        return f"{self.event_type or 'unknown'} | ok={self.processed_ok} | {self.received_at}"


class StripeRefundEvent(models.Model):
    SOURCE_TRAINER_PORTAL = "trainer_portal"
    SOURCE_CLIENT_PORTAL = "client_portal"
    SOURCE_ADMIN = "admin"
    SOURCE_CHOICES = [
        (SOURCE_TRAINER_PORTAL, "Trainer portal"),
        (SOURCE_CLIENT_PORTAL, "Client portal"),
        (SOURCE_ADMIN, "Admin"),
    ]

    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="stripe_refund_events")
    checkout = models.ForeignKey(
        Checkout,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_refund_events",
    )
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_refund_events",
    )
    refund_id = models.CharField(max_length=255, blank=True)
    payment_intent_id = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=9, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=10, default="USD")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_ADMIN)
    status = models.CharField(max_length=40, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.refund_id or 'refund'} | {self.currency} {self.amount} | {self.source}"
