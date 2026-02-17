from django.urls import path
from . import views

app_name = "booking"

urlpatterns = [
    # Home / landing pages
    path("", views.home_io_view, name="home_io"),          # simple home (CTA only)
    path("home/", views.home_view, name="home"),           # full home
    path("trainer/access/", views.trainer_access_view, name="trainer_access"),

    # Trainer booking pages
    path("t/<slug:slug>/book/", views.booking_view, name="booking"),
    path("t/<slug:slug>/checkout/", views.create_checkout_view, name="checkout"),

    # Booking result / receipt
    path("success/", views.booking_success_view, name="booking_success"),

    # Public trainer list
    path("trainers/", views.trainer_list_view, name="trainer_list"),

    # Trainer portal + registration
    path("trainer/", views.trainer_portal_view, name="trainer_portal"),
    path("trainer/register/", views.trainer_register_view, name="trainer_register"),
    path("trainer/dashboard/", views.trainer_dashboard_view, name="trainer_dashboard"),

    # Stripe Connect (trainer payouts)
    path("trainer/stripe/connect/", views.trainer_stripe_connect_start, name="trainer_stripe_connect_start"),
    path("trainer/stripe/return/", views.trainer_stripe_connect_return, name="trainer_stripe_connect_return"),
    path("trainer/stripe/refresh/", views.trainer_stripe_connect_refresh, name="trainer_stripe_connect_refresh"),

    # Stripe Webhook (must be public, no login)
    path("stripe/webhook/", views.stripe_webhook_view, name="stripe_webhook"),
]
