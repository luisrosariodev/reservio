from django.urls import path
from . import views

app_name = "booking"

urlpatterns = [
    path("healthz/", views.healthz_view, name="healthz"),
    # Home / landing pages
    path("", views.home_io_view, name="home_io"),          # simple home (CTA only)
    path("home/", views.home_view, name="home"),           # full home
    path("portal/", views.account_portal_home_view, name="account_portal_home"),
    path("account/roles/", views.account_role_management_view, name="account_role_management"),
    path("account/2fa/setup/", views.account_two_factor_setup_view, name="account_two_factor_setup"),
    path("account/2fa/disable/", views.account_two_factor_disable_view, name="account_two_factor_disable"),
    path("account/2fa/backup-codes/regenerate/", views.account_two_factor_regenerate_codes_view, name="account_two_factor_regenerate_codes"),
    path("accounts/2fa/verify/", views.two_factor_verify_view, name="two_factor_verify"),
    path("account/mode/", views.account_mode_select_view, name="account_mode_select"),
    path("account/delete/", views.account_delete_view, name="account_delete"),
    path("account/deleted/", views.account_deleted_view, name="account_deleted"),
    path("trainer/access/", views.trainer_access_view, name="trainer_access"),
    path("clients/access/", views.client_access_view, name="client_portal_access"),
    path("clients/sign-up/", views.client_register_view, name="client_portal_register"),
    path("clients/dashboard/", views.client_dashboard_view, name="client_portal_dashboard"),
    path(
        "clients/reservations/<int:reservation_id>/cancel/",
        views.client_cancel_reservation_view,
        name="client_cancel_reservation",
    ),
    path(
        "clients/reservations/<int:reservation_id>/reschedule/",
        views.client_reschedule_reservation_view,
        name="client_reschedule_reservation",
    ),

    # Trainer booking pages
    path("t/<slug:slug>/book/", views.booking_view, name="booking"),
    path("t/<slug:slug>/checkout/", views.create_checkout_view, name="checkout"),

    # Booking result / receipt
    path("success/", views.booking_success_view, name="booking_success"),

    # Public trainer list
    path("trainers/", views.trainer_list_view, name="trainer_list"),

    # Trainer portal + registration
    path("trainer/", views.trainer_portal_view, name="trainer_portal"),
    path("trainer/exit/", views.trainer_portal_exit_view, name="trainer_portal_exit"),
    path("trainer/register/", views.trainer_register_view, name="trainer_register"),
    path("trainer/verify/pending/", views.trainer_verify_pending_view, name="trainer_verify_pending"),
    path("trainer/verify/email/", views.trainer_verify_email_view, name="trainer_verify_email"),
    path("trainer/verify/resend/", views.trainer_verify_resend_view, name="trainer_verify_resend"),
    path("trainer/dashboard/", views.trainer_dashboard_view, name="trainer_dashboard"),
    path("trainer/clients/export/", views.trainer_clients_export_view, name="trainer_clients_export"),
    path(
        "trainer/reservations/<int:reservation_id>/cancel/",
        views.trainer_cancel_reservation_view,
        name="trainer_cancel_reservation",
    ),
    path(
        "trainer/reservations/<int:reservation_id>/confirm-manual-payment/",
        views.trainer_confirm_manual_payment_view,
        name="trainer_confirm_manual_payment",
    ),

    # Stripe Connect (trainer payouts)
    path("trainer/stripe/connect/", views.trainer_stripe_connect_start, name="trainer_stripe_connect_start"),
    path("trainer/stripe/return/", views.trainer_stripe_connect_return, name="trainer_stripe_connect_return"),
    path("trainer/stripe/refresh/", views.trainer_stripe_connect_refresh, name="trainer_stripe_connect_refresh"),

    # Stripe Webhook (must be public, no login)
    path("stripe/webhook/", views.stripe_webhook_view, name="stripe_webhook"),
]
