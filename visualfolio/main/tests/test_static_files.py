import tempfile

from django.core.management import call_command
from django.test import TestCase, override_settings


class StaticFilesServingTests(TestCase):
    """Static assets are served by the application itself, with no reverse proxy, when DEBUG is off."""

    def test_collected_asset_is_served_without_debug(self):
        with tempfile.TemporaryDirectory() as static_root, override_settings(DEBUG=False, STATIC_ROOT=static_root):
            call_command("collectstatic", interactive=False, verbosity=0)
            response = self.client.get("/static/main/img/favicon.svg", secure=True)

        self.assertEqual(response.status_code, 200)
