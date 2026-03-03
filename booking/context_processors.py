from django.conf import settings
from django.templatetags.static import static


def branding(request):
    favicon_url = (getattr(settings, "SITE_FAVICON_URL", "") or "").strip()
    if favicon_url.startswith("/static/"):
        favicon_url = static(favicon_url[len("/static/"):])
    elif not favicon_url:
        favicon_url = static("img/favicon-256.png")
    return {
        "SITE_FAVICON_URL": favicon_url,
    }
