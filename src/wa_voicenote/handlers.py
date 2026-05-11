"""Webhook request handlers. Full state machine lands in c12.

This file currently exposes only the allowlist guard. Full state-machine
transitions (idle, awaiting_context) come in c12 once state_repo (c7)
and blob_repo (c8) and transcoder (c9) and aoai_client (c10) are in place.
"""

from __future__ import annotations

from collections.abc import Iterable


def is_sender_allowed(sender: str, allowlist: Iterable[str]) -> bool:
    """Return True iff `sender` is an exact match for an entry in `allowlist`.

    No prefix matching, no normalization beyond exact string equality.
    The Settings model already strips whitespace from entries at load time.
    """
    return sender in set(allowlist)
