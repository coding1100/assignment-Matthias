"""Runtime configuration for the analytics pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PipelineConfig:
    """Configuration values loaded from environment variables."""

    table_name: str = "gaming_mental_health"
    max_result_rows: int = 100
    max_rows_for_answer_prompt: int = 15
    enforce_limit: bool = True
    sql_statement_timeout_ms: int = 5000
    log_level: str = "INFO"
    enable_debug_logs: bool = False
    conversation_max_turns: int = 20
    conversation_max_rows_per_turn: int = 50

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        return cls(
            table_name=os.getenv("PIPELINE_TABLE_NAME", "gaming_mental_health").strip() or "gaming_mental_health",
            max_result_rows=max(1, _env_int("PIPELINE_MAX_RESULT_ROWS", 100)),
            max_rows_for_answer_prompt=max(1, _env_int("PIPELINE_MAX_ANSWER_ROWS", 15)),
            enforce_limit=_env_bool("PIPELINE_ENFORCE_LIMIT", True),
            sql_statement_timeout_ms=max(100, _env_int("PIPELINE_SQL_TIMEOUT_MS", 5000)),
            log_level=os.getenv("PIPELINE_LOG_LEVEL", "INFO").strip().upper() or "INFO",
            enable_debug_logs=_env_bool("PIPELINE_DEBUG", False),
            conversation_max_turns=max(1, _env_int("PIPELINE_CONVERSATION_MAX_TURNS", 20)),
            conversation_max_rows_per_turn=max(1, _env_int("PIPELINE_CONVERSATION_MAX_ROWS", 50)),
        )
