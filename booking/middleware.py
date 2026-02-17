# booking/middleware.py
from django.contrib.auth import logout

class TrainerPortalLogoutMiddleware:
    """
    If a trainer is in 'trainer portal mode' and navigates outside allowed paths,
    log them out automatically.
    """

    # Rutas permitidas cuando estás en modo portal
    ALLOWED_PREFIXES = (
        "/trainer/",          # portal y todo lo que cuelga de ahí
        "/accounts/logout/",  # logout endpoint (por si acaso)
        "/admin/",            # opcional: si quieres permitir admin (puedes quitarlo)
    )

    # Rutas que NO deben causar logout aunque no sean portal (estáticos, etc.)
    SAFE_PREFIXES = (
        "/static/",
        "/media/",
        "/favicon.ico",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or "/"

        # Siempre permitir assets
        if path.startswith(self.SAFE_PREFIXES):
            return self.get_response(request)

        user = getattr(request, "user", None)

        # Si no está logueado, no hacemos nada
        if not user or not user.is_authenticated:
            return self.get_response(request)

        # Si el usuario entra al portal, activamos modo portal
        if path.startswith("/trainer/"):
            request.session["trainer_portal_mode"] = True
            return self.get_response(request)

        # Si no está en modo portal, no hacemos nada
        if not request.session.get("trainer_portal_mode"):
            return self.get_response(request)

        # Si está en modo portal y sale a una ruta no permitida → logout
        if not path.startswith(self.ALLOWED_PREFIXES):
            logout(request)
            request.session.pop("trainer_portal_mode", None)

        return self.get_response(request)