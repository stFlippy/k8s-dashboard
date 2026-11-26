"""Fail closed on unhandled Python Qt callbacks; never resume unknown state."""
import logging
import traceback
from .app_logging import redact


def failure_hook(app, shutdown=None):
    failed = False

    def report(exc_type, value, tb):
        nonlocal failed
        if issubclass(exc_type, KeyboardInterrupt):
            if failed: return
            try:
                logging.getLogger('quaber').info('User interrupt requested shutdown.')
            except Exception:
                pass
            if shutdown is not None: shutdown.request('user_interrupt',130)
            else: app.exit(130)
            return
        if failed:
            return
        failed = True
        try:
            diagnostic = redact(''.join(traceback.format_exception(exc_type, value, tb)))
            logging.getLogger('quaber').critical('Unhandled callback; shutting down.\n%s', diagnostic)
        except Exception:
            # Logging failures must not print exception args to stderr.
            pass
        finally:
            if shutdown is not None: shutdown.request('callback_failure',1,True)
            else: app.exit(1)

    return report
