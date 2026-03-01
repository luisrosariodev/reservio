from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms import inlineformset_factory
from django.forms.models import BaseInlineFormSet

from .models import ClientProfile, Trainer, TrainerAvailability

User = get_user_model()


# -----------------------------
# Auth + Trainer profile create
# -----------------------------
class TrainerRegisterForm(UserCreationForm):
    """Creates a Django user + a linked Trainer profile."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={
            "placeholder": "Ej: you@email.com",
            "class": "ui-input",
            "autocomplete": "email",
        }),
    )

    business_name = forms.CharField(
        max_length=200,
        required=True,
        widget=forms.TextInput(attrs={
            "placeholder": "Ej: RosarioDev Fitness",
            "class": "ui-input",
            "autocomplete": "organization",
        }),
        help_text="Este será el nombre público que verán tus clientes.",
        label="Nombre público (trainer / negocio)",
    )

    ath_mobile_handle = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            "placeholder": "Ej: @rosariodev o 787-555-1234",
            "class": "ui-input",
            "autocomplete": "off",
        }),
        help_text="Opcional. Si no lo pones, podrás usar Stripe cuando lo conectes.",
        label="ATH Móvil (handle o número)",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        # Only include fields that exist on the User model.
        fields = ("email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs.update(
            {
                "class": "ui-input",
                "placeholder": "Mínimo 8 caracteres",
                "autocomplete": "new-password",
            }
        )
        self.fields["password2"].widget.attrs.update(
            {
                "class": "ui-input",
                "placeholder": "Repite tu contraseña",
                "autocomplete": "new-password",
            }
        )

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise ValidationError("Email is required.")

        # Avoid duplicate accounts (case-insensitive)
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists. Please sign in instead.")

        # If using Django's default User (username), also avoid username collisions.
        if hasattr(User, "USERNAME_FIELD") and User.USERNAME_FIELD == "username":
            if User.objects.filter(username__iexact=email).exists():
                raise ValidationError("An account with this email already exists. Please sign in instead.")

        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["email"]  # normalized by clean_email()

        # Default Django User has username; set it to the email.
        if hasattr(user, "username"):
            user.username = email

        user.email = email

        if commit:
            with transaction.atomic():
                user.save()
                Trainer.objects.create(
                    user=user,
                    business_name=self.cleaned_data["business_name"].strip(),
                    ath_mobile_handle=(self.cleaned_data.get("ath_mobile_handle") or "").strip(),
                    active=True,
                    email_verified=False,
                    is_approved=False,
                    stripe_onboarded=False,
                )

        return user


class ClientRegisterForm(UserCreationForm):
    """Creates a Django user + a linked client profile."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={
                "placeholder": "Ej: you@email.com",
                "class": "ui-input",
                "autocomplete": "email",
            }
        ),
    )
    full_name = forms.CharField(
        max_length=200,
        required=True,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Tu nombre completo",
                "class": "ui-input",
                "autocomplete": "name",
            }
        ),
        label="Nombre completo",
    )
    phone = forms.CharField(
        max_length=30,
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Opcional",
                "class": "ui-input",
                "autocomplete": "tel",
            }
        ),
        label="Teléfono (opcional)",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs.update(
            {
                "class": "ui-input",
                "placeholder": "Mínimo 8 caracteres",
                "autocomplete": "new-password",
            }
        )
        self.fields["password2"].widget.attrs.update(
            {
                "class": "ui-input",
                "placeholder": "Repite tu contraseña",
                "autocomplete": "new-password",
            }
        )

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise ValidationError("Email is required.")

        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists. Please sign in instead.")

        if hasattr(User, "USERNAME_FIELD") and User.USERNAME_FIELD == "username":
            if User.objects.filter(username__iexact=email).exists():
                raise ValidationError("An account with this email already exists. Please sign in instead.")

        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["email"]

        if hasattr(user, "username"):
            user.username = email
        user.email = email

        if commit:
            with transaction.atomic():
                user.save()
                ClientProfile.objects.create(
                    user=user,
                    full_name=(self.cleaned_data.get("full_name") or "").strip(),
                    phone=(self.cleaned_data.get("phone") or "").strip(),
                    active=True,
                )
        return user


class TrainerRoleActivationForm(forms.Form):
    business_name = forms.CharField(
        max_length=200,
        required=True,
        label="Nombre público (trainer / negocio)",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Ej: RosarioDev Fitness",
                "class": "ui-input",
                "autocomplete": "organization",
            }
        ),
    )
    ath_mobile_handle = forms.CharField(
        max_length=100,
        required=False,
        label="ATH Móvil (handle o número)",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Opcional",
                "class": "ui-input",
                "autocomplete": "off",
            }
        ),
    )


class ClientRoleActivationForm(forms.Form):
    full_name = forms.CharField(
        max_length=200,
        required=True,
        label="Nombre completo",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Tu nombre completo",
                "class": "ui-input",
                "autocomplete": "name",
            }
        ),
    )
    phone = forms.CharField(
        max_length=30,
        required=False,
        label="Teléfono (opcional)",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Opcional",
                "class": "ui-input",
                "autocomplete": "tel",
            }
        ),
    )


# -----------------------------
# Trainer settings (single form)
# -----------------------------

# Allowed values for slot duration and buffer
ALLOWED_SLOT_DURATIONS = [30, 45, 60, 75, 90]
ALLOWED_BUFFERS = [0, 5, 10, 15, 20]

class TrainerSettingsForm(forms.ModelForm):
    class Meta:
        model = Trainer
        fields = [
            "session_price",
            "slot_duration_minutes",
            "buffer_minutes",
            "capacity_per_slot",
            "discount_code",
            "discount_percent_off",
            "discount_expires_on",
            "discount_max_uses",
            "allow_stripe_refunds",
        ]

        widgets = {
            "session_price": forms.NumberInput(
                attrs={"step": "0.01", "min": "0", "placeholder": "Ej: 40.00", "class": "ui-input"}
            ),
            "slot_duration_minutes": forms.NumberInput(
                attrs={"min": "30", "step": "15", "placeholder": "Ej: 60", "class": "ui-input"}
            ),
            "buffer_minutes": forms.NumberInput(
                attrs={"min": "0", "step": "5", "placeholder": "Ej: 0", "class": "ui-input"}
            ),
            "capacity_per_slot": forms.NumberInput(
                attrs={"min": "1", "step": "1", "placeholder": "Ej: 1", "class": "ui-input"}
            ),
            "discount_code": forms.TextInput(
                attrs={"placeholder": "Ej: VERANO10", "class": "ui-input"}
            ),
            "discount_percent_off": forms.NumberInput(
                attrs={"min": "0", "max": "100", "step": "1", "placeholder": "Ej: 10", "class": "ui-input"}
            ),
            "discount_expires_on": forms.DateInput(
                attrs={"type": "date", "class": "ui-input"}
            ),
            "discount_max_uses": forms.NumberInput(
                attrs={"min": "0", "step": "1", "placeholder": "0 = sin límite", "class": "ui-input"}
            ),
            "allow_stripe_refunds": forms.CheckboxInput(
                attrs={"class": "ui-checkbox"}
            ),
        }

        labels = {
            "session_price": "Precio por sesión (USD)",
            "slot_duration_minutes": "Duración de cada sesión (min)",
            "buffer_minutes": "Break entre sesiones (min)",
            "capacity_per_slot": "Capacidad por horario",
            "discount_code": "Código de descuento",
            "discount_percent_off": "Descuento (%)",
            "discount_expires_on": "Expira el",
            "discount_max_uses": "Límite de usos",
            "allow_stripe_refunds": "Permitir reembolsos con Stripe",
        }

        help_texts = {
            "session_price": "Lo que el cliente paga por cada sesión.",
            "capacity_per_slot": "Cuántos clientes pueden reservar el mismo horario.",
            "slot_duration_minutes": "Se usa para generar tus horarios (30, 45, 60...).",
            "buffer_minutes": "Minutos opcionales entre sesiones (0 = ninguno).",
            "discount_code": "Opcional. Si lo dejas vacío, no habrá cupón activo.",
            "discount_percent_off": "Porcentaje de descuento para ese código.",
            "discount_expires_on": "Opcional. Fecha límite para usar el cupón.",
            "discount_max_uses": "Máximo total de usos del cupón (0 = ilimitado).",
            "allow_stripe_refunds": "Si activas esto, podrás procesar reembolsos de Stripe al cancelar reservas.",
        }

    def clean_slot_duration_minutes(self):
        v = self.cleaned_data.get("slot_duration_minutes")
        if v not in ALLOWED_SLOT_DURATIONS:
            raise ValidationError(f"Selecciona una duración válida: {ALLOWED_SLOT_DURATIONS}")
        return v

    def clean_buffer_minutes(self):
        v = self.cleaned_data.get("buffer_minutes")
        if v not in ALLOWED_BUFFERS:
            raise ValidationError(f"Selecciona un buffer válido: {ALLOWED_BUFFERS}")
        return v

    def clean_capacity_per_slot(self):
        v = self.cleaned_data.get("capacity_per_slot")
        if v is None:
            return v
        if v < 1:
            raise ValidationError("La capacidad por horario debe ser al menos 1.")
        return v

    def clean_session_price(self):
        v = self.cleaned_data.get("session_price")
        if v is None:
            return v
        if v < 0:
            raise ValidationError("El precio no puede ser negativo.")
        return v

    def clean_discount_code(self):
        code = (self.cleaned_data.get("discount_code") or "").strip().upper()
        return code

    def clean_discount_percent_off(self):
        value = self.cleaned_data.get("discount_percent_off")
        if value is None:
            return 0
        if value < 0 or value > 100:
            raise ValidationError("El descuento debe estar entre 0 y 100.")
        return value

    def clean_discount_max_uses(self):
        value = self.cleaned_data.get("discount_max_uses")
        if value is None:
            return 0
        if value < 0:
            raise ValidationError("El límite de usos no puede ser negativo.")
        return value


# ------------------------------------
# Availability blocks (inline formset)
# ------------------------------------
class TrainerAvailabilityForm(forms.ModelForm):
    """A weekly availability block.

    Supports multiple blocks per day (e.g., 8–12 and 5–8 on the same weekday).
    The formset will prevent overlaps on the same weekday.
    """

    # NOTE: We default this to False so the extra blank form in the formset
    # stays truly empty and doesn't trigger required-field errors on POST.
    # If a user fills weekday/start/end, we auto-enable it in clean().
    active = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "ui-checkbox"}),
        label="Activo",
    )

    # Use native time inputs with 15-min interval.
    # iPhone Safari renders this as an iOS wheel picker (scroll-style).
    start_time = forms.TimeField(
        input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(
            format="%H:%M",
            attrs={"type": "time", "class": "ui-input ui-time-scroll", "step": "900"},
        ),
        required=True,
        label="Inicio",
    )
    end_time = forms.TimeField(
        input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(
            format="%H:%M",
            attrs={"type": "time", "class": "ui-input ui-time-scroll", "step": "900"},
        ),
        required=True,
        label="Fin",
    )

    class Meta:
        model = TrainerAvailability
        fields = [
            "weekday",
            "start_time",
            "end_time",
            "active",
        ]

        widgets = {
            "weekday": forms.Select(attrs={"class": "ui-input"}),
        }

        labels = {
            "weekday": "Día",
            "active": "Activo",
        }

        help_texts = {
            "active": "Desactívalo si no quieres que este bloque genere horarios.",
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_time")
        end = cleaned.get("end_time")
        weekday = cleaned.get("weekday")
        active = cleaned.get("active")

        if start and end and start >= end:
            raise ValidationError("La hora de fin debe ser después de la hora de inicio.")

        # UX: if the user filled a valid block, auto-enable it.
        # This keeps the blank extra row quiet, but makes real entries active by default.
        if weekday is not None and start and end and not active:
            cleaned["active"] = True

        return cleaned


class BaseTrainerAvailabilityFormSet(BaseInlineFormSet):
    """Validates that active blocks don't overlap on the same weekday."""

    def clean(self):
        super().clean()

        blocks = []
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            if form.errors:
                continue

            weekday = form.cleaned_data.get("weekday")
            start = form.cleaned_data.get("start_time")
            end = form.cleaned_data.get("end_time")
            active = form.cleaned_data.get("active")

            # Skip incomplete rows (common when the extra blank row is present)
            if weekday is None or start is None or end is None:
                continue

            # Only enforce overlap rules for active rows
            if not active:
                continue

            blocks.append((weekday, start, end))

        # Check overlaps per weekday
        by_day = {}
        for weekday, start, end in blocks:
            by_day.setdefault(weekday, []).append((start, end))

        for weekday, ranges in by_day.items():
            ranges.sort(key=lambda x: x[0])
            for i in range(1, len(ranges)):
                prev_start, prev_end = ranges[i - 1]
                cur_start, cur_end = ranges[i]
                if cur_start < prev_end:
                    raise ValidationError(
                        "Puedes tener varios bloques el mismo día (mañana/tarde), pero no pueden solaparse. Ajusta los horarios para que no se encimen."
                    )


TrainerAvailabilityFormSet = inlineformset_factory(
    Trainer,
    TrainerAvailability,
    form=TrainerAvailabilityForm,
    formset=BaseTrainerAvailabilityFormSet,
    extra=1,
    can_delete=True,
)


# -----------------------------
# Slot generation helper
# -----------------------------
class GenerateSlotsForm(forms.Form):
    days_ahead = forms.IntegerField(
        min_value=7,
        max_value=60,
        initial=14,
        label="Generar horarios para",
        help_text="Cuántos días hacia adelante (7–60).",
        widget=forms.NumberInput(attrs={"class": "ui-input", "min": "7", "max": "60"}),
    )

    prune_unbooked_future = forms.BooleanField(
        required=False,
        initial=True,
        label="Reemplazar horarios futuros sin reservas",
        help_text="Si está marcado, desactivaremos horarios futuros sin reservas antes de generar nuevos.",
        widget=forms.CheckboxInput(attrs={"class": "ui-checkbox"}),
    )
