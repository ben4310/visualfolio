import os
import tempfile

from django.test import SimpleTestCase

from main import scheduler


class SchedulerLockLocationTests(SimpleTestCase):
    """The scheduler must start under any container user, so its lock lives in the temp directory, not beside the code."""

    def test_lock_file_is_in_temp_directory(self):
        self.assertEqual(os.path.dirname(scheduler.lock_file_path), tempfile.gettempdir())
