"""Privacy helpers for logs and diagnostics.

Diagnostics and logs must never carry raw credentials, tokens, device names,
areas, coordinates, raw payloads, or server error bodies. This module provides
stable-within-run anonymized device labels so a support log can correlate lines
for one device without exposing its real cloud ID.
"""
from __future__ import annotations

import hashlib
import secrets

# Regenerated every process start: labels are stable within one run (so a
# support session's log lines correlate) but cannot be reversed to a device ID
# or correlated across runs/users.
_RUN_KEY = secrets.token_bytes(32)


def anonymous_device_label(device_id: int | str) -> str:
    """Return a stable-within-run, non-reversible label for a device ID."""
    digest = hashlib.blake2s(
        str(device_id).encode(),
        key=_RUN_KEY,
        digest_size=4,
    ).hexdigest()
    return f"device-{digest}"
