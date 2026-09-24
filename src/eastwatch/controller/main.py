from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn

from eastwatch import watcher
from eastwatch.controller.api import ApiSettings, create_app
from eastwatch.controller.config import controller_config, project_tokens
from eastwatch.controller.dispatch import HostedDispatcher
from eastwatch.controller.outbox import HostedOutbox
from eastwatch.controller.retention import RetentionManager
from eastwatch.controller.store import ControllerStore


def repeat(interval: float, stop: threading.Event, action) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            action()
        except Exception:  # noqa: BLE001 — one failed poll must not kill the controller loop
            watcher.log.exception("hosted controller loop failed")
        delay = max(0.0, interval - (time.monotonic() - started))
        stop.wait(delay)


def main() -> int:
    watcher.setup_logging()
    cfg, controller = controller_config()
    tokens = project_tokens(cfg, controller)
    store = ControllerStore(Path(controller["database"]))
    store.migrate()
    dispatcher = HostedDispatcher(
        store,
        admins=frozenset(controller["admins"]),
        approved_bots=frozenset(controller.get("approved_bots") or ()),
    )
    publisher = HostedOutbox(store, tokens)
    retention = RetentionManager(store, tokens)
    lease_seconds = float(controller["lease_seconds"])
    heartbeat_seconds = float(controller["heartbeat_seconds"])
    stop = threading.Event()
    threads = [
        threading.Thread(
            target=repeat,
            args=(
                5.0,
                stop,
                lambda: watcher.hosted_cycle(cfg, store, dispatcher, tokens),
            ),
            name="gitlab-poller",
            daemon=True,
        ),
        threading.Thread(
            target=repeat,
            args=(heartbeat_seconds, stop, store.reap_expired),
            name="lease-reaper",
            daemon=True,
        ),
        threading.Thread(
            target=repeat,
            args=(2.0, stop, publisher.run_once),
            name="gitlab-outbox",
            daemon=True,
        ),
        threading.Thread(
            target=repeat,
            args=(60.0, stop, retention.run_once),
            name="retention",
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    app = create_app(
        store,
        ApiSettings(
            lease_seconds=lease_seconds,
            long_poll_seconds=float(controller["long_poll_seconds"]),
        ),
    )
    try:
        uvicorn.run(
            app,
            host=str(controller["listen_host"]),
            port=int(controller["listen_port"]),
            access_log=False,
            proxy_headers=False,
        )
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
    return 0
