import os
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from main import scheduler


class SchedulerLockLocationTests(SimpleTestCase):
    """The scheduler must start under any container user, so its lock lives in the temp directory, not beside the code."""

    def test_lock_file_is_in_temp_directory(self):
        self.assertEqual(os.path.dirname(scheduler.lock_file_path), tempfile.gettempdir())


class SchedulerHeartbeatTests(SimpleTestCase):
    """The heartbeat file proves the scheduler is alive: refreshed at start and after every successful run, never after a failed one."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.heartbeat = os.path.join(directory.name, "heartbeat")
        self.lock = os.path.join(directory.name, "lock")
        for name, value in (("heartbeat_file_path", self.heartbeat), ("lock_file_path", self.lock)):
            patcher = mock.patch.object(scheduler, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _age_heartbeat(self):
        open(self.heartbeat, "a").close()
        os.utime(self.heartbeat, (0, 0))

    def test_successful_run_refreshes_heartbeat(self):
        self._age_heartbeat()
        with mock.patch.object(scheduler, "call_command"):
            scheduler.run_scheduled_sync()
        self.assertGreater(os.path.getmtime(self.heartbeat), 0)

    def test_failed_run_leaves_heartbeat_untouched(self):
        self._age_heartbeat()
        with mock.patch.object(scheduler, "call_command", side_effect=RuntimeError("database unreachable")):
            with self.assertRaises(RuntimeError):
                scheduler.run_scheduled_sync()
        self.assertEqual(os.path.getmtime(self.heartbeat), 0)

    def test_start_refreshes_heartbeat(self):
        with mock.patch.object(scheduler, "scheduler", None), \
                mock.patch.object(scheduler, "BackgroundScheduler"), \
                mock.patch.object(scheduler.atexit, "register"):
            scheduler.start_scheduler()
        self.assertTrue(os.path.exists(self.heartbeat))
