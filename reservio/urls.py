from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.templatetags.static import static as static_url
from django.views.generic.base import RedirectView
from booking.views import (
    TrainerAwareLoginView,
    PortalAwarePasswordResetView,
    PortalAwarePasswordResetDoneView,
    PortalAwarePasswordResetConfirmView,
    PortalAwarePasswordResetCompleteView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("favicon.ico", RedirectView.as_view(url=static_url("img/favicon.ico"), permanent=False)),
    path("accounts/login/", TrainerAwareLoginView.as_view(), name="login"),
    path("accounts/password_reset/", PortalAwarePasswordResetView.as_view(), name="password_reset"),
    path("accounts/password_reset/done/", PortalAwarePasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "accounts/reset/<uidb64>/<token>/",
        PortalAwarePasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("accounts/reset/done/", PortalAwarePasswordResetCompleteView.as_view(), name="password_reset_complete"),

    # Django built-in auth (login/logout/password reset)
    path("accounts/", include("django.contrib.auth.urls")),

    # Booking app routes (/, /home/, /trainers/, /t/<slug>/book/, etc.)
    # Namespace everything under `booking:` so templates can reliably use {% url 'booking:...' %}
    path("", include(("booking.urls", "booking"), namespace="booking")),
]

# Para servir imágenes (como el QR) en desarrollo
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
