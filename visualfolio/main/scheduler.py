"""
APScheduler setup for running scheduled tasks.

Uses a file-based lock to ensure only one worker runs the scheduler,
preventing duplicate job execution in multi-worker deployments.
"""
import os
import logging
import atexit
import tempfile
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

scheduler = None
lock_file_path = os.path.join(tempfile.gettempdir(), 'visualfolio-scheduler.lock')


def is_process_alive(pid):
    """Check if a process with the given PID is still running."""
    try:
        # On Unix/Linux, sending signal 0 to a process checks if it exists
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        # Process doesn't exist
        return False


def remove_stale_lock():
    """Remove lock file if the process that created it is no longer alive."""
    if not os.path.exists(lock_file_path):
        return False
    
    try:
        with open(lock_file_path, 'r') as f:
            pid_str = f.read().strip()
            try:
                pid = int(pid_str)
                if not is_process_alive(pid):
                    logger.info("Removing stale scheduler lock file (PID %s is not running)", pid)
                    os.remove(lock_file_path)
                    return True
            except ValueError:
                logger.warning("Removing invalid scheduler lock file (invalid PID: %s)", pid_str)
                os.remove(lock_file_path)
                return True
    except Exception as exc:
        logger.warning("Error checking scheduler lock file: %s", exc)
        try:
            os.remove(lock_file_path)
            return True
        except Exception:
            pass
    
    return False


def start_scheduler():
    """
    Start the APScheduler background scheduler with multi-worker safety.
    
    Uses a lock file to ensure only one worker process runs the scheduler.
    The lock is cleaned up on process exit.
    """
    global scheduler
    
    if scheduler is not None:
        logger.info("Scheduler already started in this process")
        return
    
    remove_stale_lock()
    
    try:
        lock_fd = os.open(lock_file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)
        
        logger.info("Scheduler lock acquired by PID %s", os.getpid())
        atexit.register(cleanup_lock)
        
        scheduler = BackgroundScheduler()
        from django.core.management import call_command
        scheduler.add_job(
            lambda: call_command('check_and_sync_accounts'),
            'cron',
            hour='*',
            minute=0,
            id='check_and_sync_accounts',
            name='Check and sync user accounts',
            replace_existing=True
        )
        
        scheduler.start()
        logger.info("Scheduler started successfully")
        
    except FileExistsError:
        try:
            with open(lock_file_path, 'r') as f:
                pid_str = f.read().strip()
                try:
                    pid = int(pid_str)
                    if is_process_alive(pid):
                        logger.info("Scheduler lock held by process PID %s, skipping scheduler start", pid)
                    else:
                        os.remove(lock_file_path)
                        start_scheduler()
                except ValueError:
                    os.remove(lock_file_path)
                    start_scheduler()
        except Exception as exc:
            try:
                os.remove(lock_file_path)
                start_scheduler()
            except Exception:
                logger.warning("Could not remove lock file: %s", exc)
    except Exception as exc:
        logger.exception("Failed to start scheduler: %s", exc)


def cleanup_lock():
    """Remove the scheduler lock file on process exit."""
    try:
        if os.path.exists(lock_file_path):
            os.remove(lock_file_path)
            logger.info("Scheduler lock released by PID %s", os.getpid())
    except Exception as exc:
        logger.warning("Failed to remove scheduler lock: %s", exc)


def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    global scheduler
    if scheduler is not None:
        scheduler.shutdown()
        scheduler = None
        logger.info("Scheduler shut down")
