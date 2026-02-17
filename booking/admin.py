from django.contrib import admin
from .models import Trainer, TrainerAvailability, TimeSlot, Client, Reservation


class TrainerAvailabilityInline(admin.TabularInline):
    model = TrainerAvailability
    extra = 1


@admin.register(Trainer)
class TrainerAdmin(admin.ModelAdmin):
    list_display = (
        "business_name",
        "ath_mobile_handle",
        "active",
        "is_approved",
        "stripe_onboarded",
    )
    list_filter = ("active", "is_approved", "stripe_onboarded")
    search_fields = ("business_name", "user__email")
    readonly_fields = ("stripe_account_id",)
    prepopulated_fields = {"slug": ("business_name",)}
    fieldsets = (
        ("Basic info", {
            "fields": ("user", "business_name", "slug", "ath_mobile_handle", "active")
        }),
        ("Stripe / Payments", {
            "fields": ("is_approved", "stripe_onboarded", "stripe_account_id"),
            "description": "Approve trainers before allowing Stripe Connect onboarding."
        }),
    )
    inlines = [TrainerAvailabilityInline]


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("trainer", "date", "time", "capacity", "active")
    list_filter = ("trainer", "date", "active")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "trainer", "email", "phone")
    list_filter = ("trainer",)
    search_fields = ("name", "email")


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("reference_code", "trainer", "client", "timeslot", "amount_due", "paid", "payment_date")
    list_filter = ("trainer", "paid")
    search_fields = ("reference_code", "client__name", "client__email")