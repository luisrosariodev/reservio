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
    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)

    def __str__(self):
        return f"{self.name} ({self.trainer})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trainer", "email"],
                name="unique_client_email_per_trainer",
            )
        ]


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

    # Stripe reconciliation (set when using Stripe)
    stripe_session_id = models.CharField(max_length=255, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Checkout {self.id} / {self.trainer} / {self.client} / {self.payment_method} / {self.status}"


class Reservation(models.Model):
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
    amount_due = models.DecimalField(max_digits=7, decimal_places=2)
    paid = models.BooleanField(default=False)
    payment_date = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["timeslot", "client"],
                name="unique_reservation_per_client_timeslot",
            )
        ]

    def save(self, *args, **kwargs):
        # Generate reference_code before validation to avoid unique/blank edge cases.
        if not self.reference_code:
            random_part = get_random_string(6).upper()
            self.reference_code = f"T{self.trainer_id}-{random_part}"

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.trainer} / {self.client} / {self.timeslot} / {self.reference_code}"

    def clean(self):
        # Validar que el timeslot pertenece al trainer de la reserva
        if self.timeslot.trainer_id != self.trainer_id:
            raise ValidationError("El timeslot no pertenece al trainer de la reserva.")
        if self.client.trainer_id != self.trainer_id:
            raise ValidationError("El cliente no pertenece al trainer de la reserva.")
        if self.checkout_id:
            if self.checkout.trainer_id != self.trainer_id:
                raise ValidationError("El checkout no pertenece al trainer de la reserva.")
            if self.checkout.client_id != self.client_id:
                raise ValidationError("El checkout no pertenece al cliente de la reserva.")

        # Evitar overbooking
        if self.timeslot_id and not self.timeslot.has_space:
            raise ValidationError("Este timeslot ya está lleno.")

        # Evitar doble reserva del mismo cliente en el mismo timeslot
        if self.timeslot_id and self.client_id:
            qs = Reservation.objects.filter(timeslot_id=self.timeslot_id, client_id=self.client_id)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("Este cliente ya tiene una reserva en este timeslot.")