from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock, Thread
from typing import Any, Callable
from uuid import uuid4


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


@dataclass
class ApplyJob:
    id: str
    status: str = STATUS_PENDING
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    selected_location: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "results": list(self.results),
            "error": self.error,
            "selected_location": dict(self.selected_location or {}),
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }


_JOBS: dict[str, ApplyJob] = {}
_LOCK = Lock()


def start_apply_job(
    runner: Callable[[], tuple[list[dict[str, Any]], dict[str, Any] | None]],
) -> ApplyJob:
    job = ApplyJob(id=uuid4().hex)
    with _LOCK:
        _JOBS[job.id] = job

    def target() -> None:
        _set_status(job.id, STATUS_RUNNING)
        try:
            results, selected_location = runner()
        except Exception as exc:
            _finish_job(
                job.id,
                STATUS_FAILED,
                [
                    {
                        "id": "apply_unexpected_error",
                        "title": "Apply setup",
                        "status": "failed",
                        "summary": "Apply stopped because the wizard hit an unexpected error.",
                        "details": [str(exc)],
                    }
                ],
                None,
                str(exc),
            )
            return

        succeeded = all(result.get("status") == "passed" for result in results)
        _finish_job(
            job.id,
            STATUS_SUCCEEDED if succeeded else STATUS_FAILED,
            results,
            selected_location,
            None,
        )

    Thread(target=target, daemon=True).start()
    return job


def get_apply_job(job_id: str | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    with _LOCK:
        job = _JOBS.get(job_id)
        return job.snapshot() if job else None


def _set_status(job_id: str, status: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.status = status
        job.updated_at = datetime.now(timezone.utc)


def _finish_job(
    job_id: str,
    status: str,
    results: list[dict[str, Any]],
    selected_location: dict[str, Any] | None,
    error: str | None,
) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.status = status
        job.results = results
        job.selected_location = selected_location
        job.error = error
        job.updated_at = datetime.now(timezone.utc)
