from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests


class RunnerApiError(RuntimeError):
    pass


class LeaseRejected(RunnerApiError):
    pass


@dataclass(frozen=True, slots=True)
class Lease:
    envelope: dict[str, Any]
    lease_generation: int
    lease_until: float


class RunnerClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        retries: int = 4,
    ) -> dict[str, Any] | None:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = requests.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=self.headers,
                    timeout=timeout,
                )
            except requests.RequestException as error:
                last_error = error
            else:
                if response.status_code == 204:
                    return None
                if response.status_code == 409:
                    raise LeaseRejected(response.text[:1000])
                if 200 <= response.status_code < 300:
                    return response.json()
                if response.status_code < 500:
                    raise RunnerApiError(
                        f"controller rejected request: {response.status_code} {response.text[:1000]}"
                    )
                last_error = RunnerApiError(
                    f"controller returned {response.status_code}: {response.text[:1000]}"
                )
            if attempt + 1 < retries:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
        raise RunnerApiError(
            f"controller request failed after {retries} attempts: {last_error}"
        )

    def claim(self, free_slots: int, wait_seconds: float = 20) -> tuple[Lease, ...]:
        payload = self.post(
            "/v1/runner/claim",
            {"free_slots": free_slots, "wait_seconds": wait_seconds},
            timeout=wait_seconds + 10,
            retries=1,
        )
        return tuple(Lease(**item) for item in (payload or {}).get("jobs", ()))

    def started(self, job_id: str, generation: int, refs: dict[str, Any]) -> bool:
        payload = self.post(
            f"/v1/runner/jobs/{job_id}/started",
            {"lease_generation": generation, "refs": refs},
            timeout=15,
        )
        return bool((payload or {}).get("accepted"))

    def heartbeat(self, active: list[dict[str, int]]) -> None:
        self.post("/v1/runner/heartbeat", {"active": active}, timeout=15, retries=2)

    def mark_ready(self) -> None:
        self.post("/v1/runner/ready", {}, timeout=15, retries=2)

    def fail_before_start(
        self, job_id: str, generation: int, error: dict[str, Any]
    ) -> bool:
        payload = self.post(
            f"/v1/runner/jobs/{job_id}/failed-before-start",
            {"lease_generation": generation, "error": error},
            timeout=15,
        )
        return bool((payload or {}).get("accepted"))

    def complete(
        self,
        job_id: str,
        generation: int,
        state: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        payload = self.post(
            f"/v1/runner/jobs/{job_id}/complete",
            {
                "lease_generation": generation,
                "state": state,
                "result": result,
                "error": error,
            },
            timeout=15,
        )
        return bool((payload or {}).get("accepted"))

    def claim_actions(self, limit: int = 1) -> tuple[dict[str, Any], ...]:
        payload = self.post(
            "/v1/runner/actions/claim",
            {"limit": limit},
            timeout=15,
            retries=2,
        )
        return tuple((payload or {}).get("actions", ()))

    def finish_action(
        self,
        action_id: str,
        generation: int,
        *,
        success: bool,
        error: str | None,
    ) -> bool:
        payload = self.post(
            f"/v1/runner/actions/{action_id}/finish",
            {
                "lease_generation": generation,
                "success": success,
                "error": error,
            },
            timeout=15,
        )
        return bool((payload or {}).get("accepted"))
