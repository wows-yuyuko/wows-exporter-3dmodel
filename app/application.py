"""Application context (plain Python, no Qt)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.config import ConfigManager, AppConfig
from utils.path_utils import get_app_dir, get_data_dir, get_split_dir


@dataclass
class AppContext:
    exe_dir: Path = field(default_factory=get_app_dir)
    data_dir: Path = field(default_factory=get_data_dir)
    split_dir: Path = field(default_factory=get_split_dir)
    config: AppConfig = field(default_factory=AppConfig.default)

    @property
    def wows_type(self):
        return self.config.wows_type

    @property
    def game_path(self):
        return self.config.game_path

    @property
    def game_path_is_set(self):
        return self.config.is_path_set(self.wows_type)

    @property
    def game_version(self):
        return self.config.game_version

    @property
    def game_data_state(self):
        return self.config.game_data_state

    @property
    def keep_split_json(self):
        return self.config.keep_split_json

    @property
    def bin_folder(self):
        return self.config.bin_folder


class Application:
    def __init__(self):
        self._config_manager = ConfigManager()
        self._ctx = AppContext(config=self._config_manager._raw)
        self._schema_mismatches = []

    @property
    def ctx(self):
        return self._ctx

    @property
    def config(self):
        return self._config_manager

    @property
    def schema_mismatches(self):
        return self._schema_mismatches

    def _refresh_ctx(self):
        self._ctx = AppContext(config=self._config_manager._raw)

    def set_wows_type(self, value):
        self.config.wows_type = value
        self._refresh_ctx()

    def set_game_path(self, value):
        self.config.game_path = value
        self._refresh_ctx()

    def set_game_path_for_server(self, server, value):
        self.config.set_server_path(server, value)
        self._refresh_ctx()

    def set_game_version(self, value):
        self.config.game_version = value
        self._refresh_ctx()

    def set_game_data_state(self, value):
        self.config.game_data_state = bool(value)
        self._refresh_ctx()

    def set_bin_folder(self, value):
        self.config.bin_folder = value
        self._refresh_ctx()

    def reset_all(self):
        self._refresh_ctx()

    def _sync_data_state(self):
        pass


app = Application()
