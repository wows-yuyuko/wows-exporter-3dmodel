"""Application context (plain Python, no Qt)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import ConfigManager, AppConfig


@dataclass
class AppContext:
    """运行期上下文：只暴露导出链路真正会读的配置项。"""

    config: AppConfig = field(default_factory=AppConfig.default)

    @property
    def wows_type(self):
        return self.config.wows_type

    @property
    def game_path(self):
        return self.config.game_path

    @property
    def game_version(self):
        return self.config.game_version

    @property
    def game_data_state(self):
        return self.config.game_data_state

    @property
    def bin_folder(self):
        return self.config.bin_folder


class Application:
    def __init__(self):
        self._config_manager = ConfigManager()
        self._ctx = AppContext(config=self._config_manager._raw)

    @property
    def ctx(self):
        return self._ctx

    @property
    def config(self):
        return self._config_manager

    def _refresh_ctx(self):
        self._ctx = AppContext(config=self._config_manager._raw)

    def set_wows_type(self, value):
        self.config.wows_type = value
        self._refresh_ctx()

    def set_game_data_state(self, value):
        self.config.game_data_state = bool(value)
        self._refresh_ctx()


app = Application()
