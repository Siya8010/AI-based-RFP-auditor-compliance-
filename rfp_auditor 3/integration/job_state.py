"""
integration/job_state.py
─────────────────────────
Persistent job-state store.

Writes/reads a small JSON sidecar file alongside each audit run so that
if the browser disconnects (laptop sleep, network drop, tab close) the
Gradio UI can recover the completed results when the user reopens the page.

File layout (output/<job_id>_state.json):
{
    "job_id":      "audit_my_rfp_2025-01-01-12-00-00_abc123",
    "status":      "running" | "complete" | "error",
    "started_at":  "2025-01-01T12:00:00",
    "finished_at": "2025-01-01T12:05:23",   # null while running
    "pdf_name":    "my_rfp.pdf",
    "excel_path":  "/abs/path/output/audit_my_rfp_...xlsx",  # null until export
    "log":         ["line1", "line2", ...],
    "progress":    0.97,
    "table_data":  [[...], [...], ...]       # null until pipeline finishes
}

The file at output/latest_job.txt always contains the job_id of the most
recent job so the UI can find it without scanning the directory.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Public API ─────────────────────────────────────────────────────────────

class JobState:
    """Thin wrapper around a single JSON state file."""

    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._data: dict[str, Any] = {}

    # ── Factory helpers ────────────────────────────────────────────────────

    @classmethod
    def create(cls, output_dir: str, job_id: str, pdf_name: str) -> "JobState":
        """Initialise a new state file; overwrites any existing file."""
        p = Path(output_dir) / f"{job_id}_state.json"
        obj = cls(p)
        obj._data = {
            "job_id": job_id,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "pdf_name": pdf_name,
            "excel_path": None,
            "log": [],
            "progress": 0.0,
            "table_data": None,
        }
        obj._write()
        # Update the pointer file
        pointer = Path(output_dir) / "latest_job.txt"
        pointer.write_text(job_id, encoding="utf-8")
        return obj

    @classmethod
    def load_latest(cls, output_dir: str) -> Optional["JobState"]:
        """
        Load the most recently created job state.
        Returns None if no state exists.
        """
        pointer = Path(output_dir) / "latest_job.txt"
        if not pointer.exists():
            return None
        job_id = pointer.read_text(encoding="utf-8").strip()
        state_path = Path(output_dir) / f"{job_id}_state.json"
        if not state_path.exists():
            return None
        obj = cls(state_path)
        try:
            obj._data = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return obj

    # ── Mutation helpers ───────────────────────────────────────────────────

    def append_log(self, line: str) -> None:
        self._data.setdefault("log", []).append(line)
        self._write()

    def set_progress(self, progress: float) -> None:
        self._data["progress"] = round(progress, 3)
        self._write()

    def update(self, line: str, progress: float) -> None:
        """Combine log append + progress update in a single write."""
        self._data.setdefault("log", []).append(line)
        self._data["progress"] = round(progress, 3)
        self._write()

    def set_excel_path(self, path: str) -> None:
        self._data["excel_path"] = path
        self._write()

    def set_table_data(self, table: list[list[str]]) -> None:
        self._data["table_data"] = table
        self._write()

    def complete(self, table: list[list[str]], excel_path: Optional[str]) -> None:
        self._data.update({
            "status": "complete",
            "finished_at": _now(),
            "table_data": table,
            "excel_path": excel_path,
            "progress": 1.0,
        })
        self._write()

    def error(self, message: str) -> None:
        self._data.update({
            "status": "error",
            "finished_at": _now(),
        })
        self._data.setdefault("log", []).append(message)
        self._write()

    # ── Read helpers ───────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._data.get("status", "unknown")

    @property
    def is_complete(self) -> bool:
        return self._data.get("status") == "complete"

    @property
    def is_running(self) -> bool:
        return self._data.get("status") == "running"

    @property
    def progress(self) -> float:
        return float(self._data.get("progress", 0.0))

    @property
    def log_text(self) -> str:
        """Return log lines joined newest-first (matches UI convention)."""
        lines = self._data.get("log", [])
        return "\n".join(reversed(lines))

    @property
    def table_data(self) -> Optional[list[list[str]]]:
        return self._data.get("table_data")

    @property
    def excel_path(self) -> Optional[str]:
        p = self._data.get("excel_path")
        if p and Path(p).exists():
            return p
        return None

    @property
    def pdf_name(self) -> str:
        return self._data.get("pdf_name", "")

    @property
    def job_id(self) -> str:
        return self._data.get("job_id", "")

    @property
    def started_at(self) -> str:
        return self._data.get("started_at", "")

    @property
    def finished_at(self) -> Optional[str]:
        return self._data.get("finished_at")

    # ── Stale-detection ────────────────────────────────────────────────────

    def is_stale(self, timeout_seconds: int = 3600) -> bool:
        """
        A 'running' job is considered stale if it hasn't been updated
        in `timeout_seconds`. This catches the case where the backend
        process itself died without setting status='error'.
        """
        if self.status != "running":
            return False
        try:
            mtime = self._path.stat().st_mtime
            return (time.time() - mtime) > timeout_seconds
        except OSError:
            return True

    # ── Internal ───────────────────────────────────────────────────────────

    def _write(self) -> None:
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )