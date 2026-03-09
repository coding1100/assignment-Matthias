"""Validation utilities for SQL safety and result/answer quality."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from src.config import PipelineConfig
from src.types import SQLValidationOutput


_BLOCKED_SQL_PATTERNS = [
    r"\bdelete\b",
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdrop\b",
    r"\balter\b",
    r"\bcreate\b",
    r"\breplace\b",
    r"\btruncate\b",
    r"\battach\b",
    r"\bdetach\b",
    r"\bpragma\b",
    r"\bvacuum\b",
    r"\breindex\b",
    r"\bgrant\b",
    r"\brevoke\b",
]

_TABLE_PATTERN = re.compile(r"\b(from|join)\s+([`\"\[]?)([a-zA-Z_][\w]*)\2", re.IGNORECASE)
_DANGEROUS_INTENT_PATTERN = re.compile(
    r"\b(delete|drop|truncate|remove|erase|update|insert|alter|create|destroy)\b",
    re.IGNORECASE,
)
_SQLITE_UNSUPPORTED_FUNC_REWRITES: list[tuple[str, str]] = [
    (r"\bstddev\s*\(", "AVG("),
    (r"\bmedian\s*\(", "AVG("),
    (r"\bvariance\s*\(", "AVG("),
]


def _strip_wrapping_fences(sql: str) -> str:
    value = sql.strip()
    if value.startswith("```"):
        value = value.strip("`")
        lines = value.splitlines()
        if lines and lines[0].lower().startswith("sql"):
            lines = lines[1:]
        value = "\n".join(lines).strip()
    return value


def _normalize_sql(sql: str) -> str:
    cleaned = _strip_wrapping_fences(sql)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(";").strip()
    return cleaned


def _has_multiple_statements(sql: str) -> bool:
    return ";" in sql


def _contains_blocked_operation(sql: str) -> str | None:
    for pattern in _BLOCKED_SQL_PATTERNS:
        if re.search(pattern, sql, flags=re.IGNORECASE):
            return pattern.replace(r"\b", "").strip("\\")
    if "--" in sql or "/*" in sql or "*/" in sql:
        return "comment_tokens"
    return None


def _extract_referenced_tables(sql: str) -> set[str]:
    return {m.group(3).lower() for m in _TABLE_PATTERN.finditer(sql)}


def _sqlite_sanitize(sql: str, table_name: str) -> str:
    sanitized = sql
    for pattern, replacement in _SQLITE_UNSUPPORTED_FUNC_REWRITES:
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)

    lower = sanitized.lower()
    if lower.startswith("select ") and " from " not in lower:
        clause_match = re.search(r"\b(where|group\s+by|order\s+by|limit|having)\b", sanitized, flags=re.IGNORECASE)
        from_clause = f' FROM "{table_name}"'
        if clause_match:
            idx = clause_match.start()
            sanitized = sanitized[:idx].rstrip() + from_clause + " " + sanitized[idx:].lstrip()
        else:
            sanitized = sanitized.rstrip() + from_clause
    return sanitized


def validate_rows_shape(rows: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not rows:
        return True, None
    first_keys = set(rows[0].keys())
    for idx, row in enumerate(rows[1:], start=2):
        if set(row.keys()) != first_keys:
            return False, f"Row {idx} has inconsistent columns."
    return True, None


def validate_answer_text(answer: str) -> tuple[bool, str | None]:
    text = (answer or "").strip()
    if not text:
        return False, "Answer text is empty."
    if len(text) < 3:
        return False, "Answer text is too short."
    return True, None


def has_dangerous_intent(question: str) -> bool:
    return bool(_DANGEROUS_INTENT_PATTERN.search(question or ""))


class SQLValidator:
    """Safety and schema-aware SQL validation for analytics SELECT queries."""

    def __init__(self, db_path: str | Path, config: PipelineConfig) -> None:
        self.db_path = Path(db_path)
        self.config = config
        self._columns = self._load_columns()

    def _load_columns(self) -> set[str]:
        if not self.db_path.exists():
            return set()
        query = f'PRAGMA table_info("{self.config.table_name}")'
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            rows = cur.execute(query).fetchall()
        return {str(r[1]).lower() for r in rows if len(r) >= 2}

    def schema_context(self) -> dict[str, Any]:
        cols = sorted(self._columns)
        return {
            "table_name": self.config.table_name,
            "columns": cols,
            "column_count": len(cols),
            "result_row_cap": self.config.max_result_rows,
        }

    def _enforce_limit(self, sql: str) -> str:
        if not self.config.enforce_limit:
            return sql
        if re.search(r"\blimit\s+\d+\b", sql, flags=re.IGNORECASE):
            return sql
        return f"{sql} LIMIT {self.config.max_result_rows}"

    def _validate_schema_references(self, sql: str) -> str | None:
        if not self._columns:
            return None
        referenced = _extract_referenced_tables(sql)
        if not referenced:
            return "SQL must reference a table via FROM."
        if any(t != self.config.table_name.lower() for t in referenced):
            return f"Only table '{self.config.table_name}' is allowed."
        return None

    def _sqlite_parse_check(self, sql: str) -> str | None:
        if not self.db_path.exists():
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(f"EXPLAIN QUERY PLAN {sql}")
        except Exception as exc:
            return f"SQL parse/plan error: {exc}"
        return None

    def validate(self, sql: str | None) -> SQLValidationOutput:
        import time

        start = time.perf_counter()
        if sql is None:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="No SQL provided",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        normalized = _normalize_sql(sql)
        if not normalized:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="SQL is empty after normalization.",
                timing_ms=(time.perf_counter() - start) * 1000,
            )
        normalized = _sqlite_sanitize(normalized, self.config.table_name)

        lower_sql = normalized.lower()
        if not (lower_sql.startswith("select ") or lower_sql.startswith("with ")):
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="Only SELECT statements are allowed.",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        if _has_multiple_statements(normalized):
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="Multiple SQL statements are not allowed.",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        blocked = _contains_blocked_operation(lower_sql)
        if blocked:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error=f"Blocked SQL operation detected: {blocked}.",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        schema_error = self._validate_schema_references(normalized)
        if schema_error:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error=schema_error,
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        parse_error = self._sqlite_parse_check(normalized)
        if parse_error:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error=parse_error,
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        validated_sql = self._enforce_limit(normalized)
        return SQLValidationOutput(
            is_valid=True,
            validated_sql=validated_sql,
            error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )
