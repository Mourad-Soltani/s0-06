"""Poll loop for a bus Worker. Stops promptly when the event is set. A killed marketplace's events are not
claimed (migration 0003), so a paused queue costs one cheap empty claim per poll, never an attempt."""
from __future__ import annotations

import threading

from shared.bus.worker import Worker


def run_loop(worker: Worker, stop: threading.Event, poll_interval: float = 1.0) -> None:
    while not stop.is_set():
        if worker.run_once().claimed == 0:
            stop.wait(poll_interval)
