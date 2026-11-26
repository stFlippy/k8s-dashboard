"""Explicit panel read owners; no guessing mutable widget fields."""
from typing import Protocol
from weakref import WeakSet
from PyQt5 import sip


class ReadOwner(Protocol):
    def suspend_reads(self) -> None: ...


def register_read_owner(parent, owner: ReadOwner):
    if not hasattr(parent, '_read_owners'):
        parent._read_owners = WeakSet()
    parent._read_owners.add(owner)


def suspend_children(parent, subtree=None):
    for owner in tuple(getattr(parent, '_read_owners', ())):
        if not sip.isdeleted(owner) and (subtree is None or owner is subtree or subtree.isAncestorOf(owner)):
            owner.suspend_reads()
