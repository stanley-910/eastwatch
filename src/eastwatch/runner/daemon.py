from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from eastwatch.runner.client import Lease, LeaseRejected, RunnerApiError, RunnerClient
from eastwatch.runner.executor import WorkspaceExecutor

log = logging.getLogger(__name__)


class RunnerDaemon:
    def __init__(
        self,
        client: RunnerClient,
        executor: WorkspaceExecutor,
        capacity: int,
        heartbeat_seconds: float,
    ) -> None:
        if not 1 <= capacity <= 10:
            raise ValueError("runner capacity must be between 1 and 10")
        self.client = client
        self.executor = executor
        self.capacity = capacity
        self.heartbeat_seconds = heartbeat_seconds
        self.pool = ThreadPoolExecutor(
            max_workers=capacity, thread_name_prefix="bw-job"
        )
        self.lock = threading.Lock()
        self.active: dict[
            tuple[str, int],
            tuple[Lease, Future[None], threading.Event],
        ] = {}
        self.stale: set[tuple[str, int]] = set()
        self.stop = threading.Event()

    def active_count(self) -> int:
        with self.lock:
            return sum(
                not future.done() for _lease, future, _cancel in self.active.values()
            )

    def heartbeat_targets(self) -> list[tuple[tuple[str, int], dict[str, int]]]:
        with self.lock:
            return [
                (key, {"job_id": key[0], "lease_generation": key[1]})
                for key, (_lease, future, _cancel) in self.active.items()
                if not future.done() and key not in self.stale
            ]

    def heartbeat_loop(self) -> None:
        while not self.stop.wait(self.heartbeat_seconds):
            for key, target in self.heartbeat_targets():
                try:
                    self.client.heartbeat([target])
                except LeaseRejected:
                    with self.lock:
                        self.stale.add(key)
                        current = self.active.get(key)
                        if current is not None:
                            current[2].set()
                except RunnerApiError:
                    continue

    def collect_finished(self) -> None:
        with self.lock:
            finished = [
                key
                for key, (_lease, future, _cancel) in self.active.items()
                if future.done()
            ]
            futures = [self.active.pop(key)[1] for key in finished]
            self.stale.difference_update(finished)
        for future in futures:
            try:
                future.result()
            except Exception:  # noqa: BLE001 — preserve daemon capacity after one job fails
                log.exception("runner job exited with an unhandled error")

    def submit(self, lease: Lease) -> None:
        key = (str(lease.envelope["job_id"]), lease.lease_generation)
        with self.lock:
            if key in self.active:
                return
            cancelled = threading.Event()
            future = self.pool.submit(self.executor.execute, lease, cancelled)
            self.active[key] = (lease, future, cancelled)

    def run(self) -> None:
        heartbeat = threading.Thread(
            target=self.heartbeat_loop, name="bw-heartbeat", daemon=True
        )
        heartbeat.start()
        try:
            while not self.stop.is_set():
                self.collect_finished()
                free_slots = self.capacity - self.active_count()
                if free_slots <= 0:
                    self.stop.wait(1)
                    continue
                try:
                    leases = self.client.claim(free_slots, wait_seconds=20)
                except RunnerApiError:
                    self.stop.wait(2)
                    continue
                for lease in leases:
                    self.submit(lease)
        finally:
            self.stop.set()
            heartbeat.join(timeout=self.heartbeat_seconds + 2)
            self.pool.shutdown(wait=True, cancel_futures=False)


def main() -> int:
    root = Path(os.environ.get("BW_WORKSPACE_ROOT", Path.home())).resolve()
    base_url = os.environ["BW_CONTROLLER_URL"]
    token = os.environ["BW_RUNNER_TOKEN"]
    capacity = int(os.environ.get("BW_RUNNER_CAPACITY", "10"))
    heartbeat_seconds = float(os.environ.get("BW_HEARTBEAT_SECONDS", "20"))
    client = RunnerClient(base_url, token)
    RunnerDaemon(
        client,
        WorkspaceExecutor(root, client),
        capacity,
        heartbeat_seconds,
    ).run()
    return 0
