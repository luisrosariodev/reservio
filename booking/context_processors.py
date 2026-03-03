from django.conf import settings
from django.templatetags.static import static


def branding(request):
    favicon_url = (getattr(settings, "SITE_FAVICON_URL", "") or "").strip()
    try:
        if favicon_url.startswith("/static/"):
            favicon_url = static(favicon_url[len("/static/"):])
        elif not favicon_url:
            favicon_url = static("img/favicon-256.png")
    except ValueError:
        # Graceful fallback if manifest doesn't include the icon yet.
        if not favicon_url:
            favicon_url = "/static/img/favicon-256.png"
    return {
        "SITE_FAVICON_URL": favicon_url,
    }
