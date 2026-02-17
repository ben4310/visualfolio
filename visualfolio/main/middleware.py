import zoneinfo

from django.conf import settings
from django.shortcuts import redirect
from django.contrib.auth import logout
from django.contrib.auth import get_user_model
from django.utils import timezone


class TimezoneMiddleware:
    """Activates the authenticated user's timezone for the current request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                user_tz = zoneinfo.ZoneInfo(request.user.user_timezone)
            except (KeyError, zoneinfo.ZoneInfoNotFoundError):
                user_tz = zoneinfo.ZoneInfo("UTC")
            timezone.activate(user_tz)
        else:
            timezone.deactivate()
        return self.get_response(request)


class ThemeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            user_theme = request.user.theme
            # Resolve 'system' to 'dark' for server-side rendering
            # Client-side JS will handle actual system preference
            if user_theme == 'system':
                request.theme = 'dark'
            else:
                request.theme = user_theme
        else:
            request.theme = 'dark'
        return self.get_response(request)
