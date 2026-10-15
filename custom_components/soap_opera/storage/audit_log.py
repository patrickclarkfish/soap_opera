"""Append-only JSONL audit log.

This is telemetry, not the medical record: unlike the database, it is
deliberately bounded and rotated. See CLAUDE.md for why that asymmetry
matters.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB


class AuditLog:
    """Writes one JSON object per line to a size-bounded, rotated file.

    capture_event is meant to be called via hass.async_add_executor_job,
    i.e. from more than one executor thread concurrently -- the rotate
    check-then-write and the rotation itself are guarded by a lock so two
    threads can't both decide to rotate at once and clobber or lose events.
    """

    def __init__(self, path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def capture_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Append one event. Blocking: call via hass.async_add_executor_job."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event_type,
            "data": data,
        }
        with self._lock:
            self._rotate_if_needed()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")

    def _rotate_if_needed(self) -> None:
        if self._path.exists() and self._path.stat().st_size >= self._max_bytes:
            backup = self._path.with_suffix(self._path.suffix + ".1")
            backup.unlink(missing_ok=True)
            self._path.rename(backup)
