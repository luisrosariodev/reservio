from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),

    # Django built-in auth (login/logout/password reset)
    path("accounts/", include("django.contrib.auth.urls")),

    # Booking app routes (/, /home/, /trainers/, /t/<slug>/book/, etc.)
    # Namespace everything under `booking:` so templates can reliably use {% url 'booking:...' %}
    path("", include(("booking.urls", "booking"), namespace="booking")),
]

# Para servir imágenes (como el QR) en desarrollo
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)