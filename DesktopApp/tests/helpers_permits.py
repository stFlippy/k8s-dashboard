"""Explicit domain-test admission, never used by production or UI integration."""
import time
from quaber.write_guard import WriteGuard
from quaber.request_policy import credentials_revision


def bound_permit(api, identity, action, parameters):
    revision = credentials_revision(api.cluster(identity[0]))
    guard = WriteGuard(lambda: True, lambda cid: credentials_revision(api.cluster(cid)))
    return guard.authorize(identity, revision, action, parameters=parameters, origin=None,
                           generation=0, validator=lambda cid: True, deadline=time.monotonic()+60)
