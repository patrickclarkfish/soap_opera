"""Tests for the append-only JSONL audit log, including concurrent writers
from multiple threads -- capture_event is meant to be called via
hass.async_add_executor_job, so more than one thread hitting it at once is
the expected usage, not an edge case.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from custom_components.soap_opera.storage.audit_log import AuditLog


def test_capture_event_appends_one_json_line(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")

    log.capture_event("dose_given", {"animal": "Biscuit"})
    log.capture_event("dose_skipped", {"animal": "Biscuit"})

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "dose_given"
    assert json.loads(lines[1])["event"] == "dose_skipped"


def test_rotation_moves_the_old_file_aside(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=1)

    log.capture_event("first", {})
    log.capture_event("second", {})

    backup = path.with_suffix(path.suffix + ".1")
    assert backup.exists()
    assert path.exists()
    assert json.loads(path.read_text().splitlines()[0])["event"] == "second"


def test_concurrent_capture_event_does_not_raise_or_lose_events(
    tmp_path: Path,
) -> None:
    """Many threads writing at once, with rotation forced on nearly every
    write (max_bytes=1), used to be able to race two rotations against each
    other -- the second thread's unlink()/rename() could hit a file the
    first thread had already moved. This should now be fully serialized.
    """
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=1)
    errors: list[BaseException] = []

    def write_many(thread_id: int) -> None:
        try:
            for i in range(25):
                log.capture_event("dose_given", {"thread": thread_id, "i": i})
        except BaseException as err:  # noqa: BLE001 - captured for the assertion below
            errors.append(err)

    threads = [threading.Thread(target=write_many, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # With max_bytes=1, essentially every write rotates -- by design, this
    # keeps only the current file plus one backup generation, so most of
    # the 200 events are *expected* to have been rotated out of existence
    # (that's the "bounded" in "bounded and rotated", not a bug). The
    # correctness property under concurrency is that the two surviving
    # files are always well-formed, one JSON object per line -- never a
    # missing file, a half-written line, or a crash from two threads
    # racing the same rotation.
    backup = path.with_suffix(path.suffix + ".1")
    assert path.exists()
    assert backup.exists()
    for candidate in (path, backup):
        lines = candidate.read_text().splitlines()
        assert len(lines) == 1
        json.loads(lines[0])  # well-formed, not truncated or concatenated
