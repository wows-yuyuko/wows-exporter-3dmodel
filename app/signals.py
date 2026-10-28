"""Plain-Python signal bus (no Qt)."""
from __future__ import annotations


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        if fn not in self._slots:
            self._slots.append(fn)


    def emit(self, *args):
        for fn in list(self._slots):
            try:
                fn(*args)
            except Exception:
                pass


class SignalBus:
    def __init__(self):
        for name in (
            "log_message", "data_loaded", "data_processed", "localization_ready",
            "task_progress", "folder_selected", "file_selected", "wows_type_changed",
            "game_path_changed", "theme_changed", "can_process_data",
            "copy_panel_screenshot", "update_check_done",
        ):
            setattr(self, name, _Signal())


bus = SignalBus()
