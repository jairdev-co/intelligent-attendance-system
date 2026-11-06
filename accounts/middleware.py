# Standard library imports
import time

# Django imports
from django.conf import settings
from django.contrib.auth import logout


class SessionMaxAgeMiddleware:
    # Log out sessions older than SESSION_MAX_AGE, even if they stay active
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            # Sessions opened before this check existed start counting now
            login_at = request.session.setdefault('login_at', time.time())
            if time.time() - login_at > settings.SESSION_MAX_AGE:
                logout(request)
        return self.get_response(request)
