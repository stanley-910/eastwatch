from __future__ import annotations

import os
import threading
from pathlib import Path

from eastwatch.runner.cleanup import CleanupBusy, CleanupExecutor
from eastwatch.runner.client import RunnerApiError, RunnerClient
from eastwatch.runner.daemon import RunnerDaemon
from eastwatch.runner.executor import WorkspaceExecutor


class MaintenanceLoop:
    def __init__(self, client: RunnerClient, cleanup: CleanupExecutor) -> None:
        self.client = client
        self.cleanup = cleanup
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(60):
            try:
                actions = self.client.claim_actions(limit=1)
            except RunnerApiError:
                continue
            for action in actions:
                action_id = str(action["action_id"])
                generation = int(action["lease_generation"])
                try:
                    self.cleanup.remove(action)
                except CleanupBusy:
                    continue  # Leave leased; expiry returns the idempotent action to pending.
                except Exception as error:  # noqa: BLE001 — report fenced cleanup failure
                    try:
                        self.client.finish_action(
                            action_id,
                            generation,
                            success=False,
                            error=str(error),
                        )
                    except RunnerApiError:
                        pass
                else:
                    try:
                        self.client.finish_action(
                            action_id,
                            generation,
                            success=True,
                            error=None,
                        )
                    except RunnerApiError:
                        pass


def main() -> int:
    root = Path(os.environ.get("BW_WORKSPACE_ROOT", Path.home())).resolve()
    client = RunnerClient(
        os.environ["BW_CONTROLLER_URL"],
        os.environ["BW_RUNNER_TOKEN"],
    )
    daemon = RunnerDaemon(
        client,
        WorkspaceExecutor(root, client),
        int(os.environ.get("BW_RUNNER_CAPACITY", "10")),
        float(os.environ.get("BW_HEARTBEAT_SECONDS", "20")),
    )
    maintenance = MaintenanceLoop(client, CleanupExecutor(root))
    thread = threading.Thread(
        target=maintenance.run, name="bw-maintenance", daemon=True
    )
    thread.start()
    try:
        daemon.run()
    finally:
        maintenance.stop.set()
        thread.join(timeout=2)
    return 0
