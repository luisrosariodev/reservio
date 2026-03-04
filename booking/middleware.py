from django.contrib.auth import logout


class TrainerPortalLogoutMiddleware:
    """
    Enforce "portal session only":
    if an authenticated user leaves portal/account routes, end session.
    """

    # Rutas consideradas "dentro del portal" (no hacen logout)
    PORTAL_PREFIXES = (
        "/trainer/",           # portal entrenador
        "/client/",            # verificación cliente
        "/clients/",           # portal cliente
        "/portal/",            # resolver portal home
        "/account/",           # gestión de roles/cuenta
        "/trainers/",          # listado de entrenadores
        "/t/",                 # booking form público de trainer
        "/success/",           # booking success/receipt
        "/accounts/password_", # cambio de contraseña dentro de cuenta
        "/accounts/reset/",    # reset confirm/complete
        "/accounts/logout/",   # endpoint de logout
        "/admin/",             # opcional
    )

    # Rutas exactas públicas/auth permitidas
    PORTAL_EXACT_PATHS = {
        "/accounts/login/",
        "/accounts/2fa/verify/",
        "/accounts/password_reset/",
        "/accounts/password_reset/done/",
    }

    # Rutas técnicas que nunca deben disparar logout
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

        def _is_allowed(p):
            if p in self.PORTAL_EXACT_PATHS:
                return True
            return p.startswith(self.PORTAL_PREFIXES)

        # Marca modo portal al entrar en cualquier ruta permitida.
        if _is_allowed(path):
            request.session["trainer_portal_mode"] = True
            return self.get_response(request)

        # Si no está marcado como sesión de portal, no forzamos logout.
        if not request.session.get("trainer_portal_mode"):
            return self.get_response(request)

        # En modo portal: salir a una ruta no-portal => cerrar sesión.
        if not _is_allowed(path):
            request.session.pop("trainer_portal_mode", None)
            logout(request)

        return self.get_response(request)
