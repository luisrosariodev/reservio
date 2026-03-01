from django.conf import settings


def branding(request):
    return {
        "SITE_FAVICON_URL": getattr(settings, "SITE_FAVICON_URL", "/media/favicon-256.png"),
    }
