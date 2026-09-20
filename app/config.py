"""应用配置，全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("SIM_DB_PATH", "/data/simulator.db")
    autoload_dir: str = os.getenv("SIM_SCENARIOS_DIR", "/app/scenarios")
    default_scenario: str | None = os.getenv("SIM_DEFAULT_SCENARIO") or None
    busy_timeout_ms: int = int(os.getenv("SIM_BUSY_TIMEOUT_MS", "5000"))
    db_retries: int = int(os.getenv("SIM_DB_RETRIES", "20"))
    log_level: str = os.getenv("SIM_LOG_LEVEL", "info")

settings = Settings()
