"""Plain threading helpers (no Qt).

run_async(fn, on_finished, on_error) starts a daemon thread; callbacks run in
that thread. CLI callers synchronize with threading.Event.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class TaskCancelled(Exception):
    pass


class _AppTask:
    def __init__(self, fn, on_finished=None, on_error=None, cancel_event=None):
        self.fn = fn
        self._on_finished = on_finished
        self._on_error = on_error
        self._cancel_event = cancel_event or threading.Event()
        self._pass_event = cancel_event is not None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        if self._cancel_event.is_set():
            return
        try:
            if self._pass_event:
                result = self.fn(self._cancel_event)
            else:
                result = self.fn()
        except TaskCancelled:
            return
        except Exception as exc:  # noqa: BLE001
            if not self._cancel_event.is_set() and self._on_error:
                self._on_error(str(exc))
            return
        if not self._cancel_event.is_set() and self._on_finished:
            self._on_finished(result)

    def cancel(self):
        self._cancel_event.set()


    @property
    def cancel_event(self):
        return self._cancel_event

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def kill(self, timeout=1.0):
        self.cancel()
        if self._thread:
            self._thread.join(timeout)
        return not self.is_running()


def run_async(fn: Callable[[], Any], on_finished=None, on_error=None,
              cancel_event: threading.Event | None = None) -> _AppTask:
    task = _AppTask(fn, on_finished=on_finished, on_error=on_error, cancel_event=cancel_event)
    task.start()
    return task
