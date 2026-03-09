"""Conversation context management for optional multi-turn support."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any


class FollowUpIntent:
    """Intent labels for follow-up question handling."""

    NEW_QUERY = "new_query"
    REFINE_PREVIOUS_SQL = "refine_previous_sql"
    EXPLAIN_PREVIOUS_RESULT = "explain_previous_result"
    AMBIGUOUS_REFERENCE = "ambiguous_reference"


@dataclass
class ConversationTurn:
    """Persisted turn-level state for a session."""

    turn_id: int
    question: str
    status: str
    sql: str | None
    rows: list[dict[str, Any]]
    answer: str
    request_id: str | None
    timestamp_utc: float
    intent: str


class InMemoryConversationStore:
    """Thread-safe in-memory store for conversation turns."""

    def __init__(self, max_turns_per_session: int = 20, max_rows_per_turn: int = 50) -> None:
        self.max_turns_per_session = max(1, max_turns_per_session)
        self.max_rows_per_turn = max(1, max_rows_per_turn)
        self._lock = threading.Lock()
        self._sessions: dict[str, list[ConversationTurn]] = {}

    def append_turn(
        self,
        session_id: str,
        *,
        question: str,
        status: str,
        sql: str | None,
        rows: list[dict[str, Any]],
        answer: str,
        request_id: str | None,
        intent: str,
    ) -> ConversationTurn:
        safe_session_id = (session_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required for conversation storage")

        with self._lock:
            turns = self._sessions.setdefault(safe_session_id, [])
            next_turn_id = (turns[-1].turn_id + 1) if turns else 1
            copied_rows = [dict(r) for r in rows[: self.max_rows_per_turn]]
            turn = ConversationTurn(
                turn_id=next_turn_id,
                question=question,
                status=status,
                sql=sql,
                rows=copied_rows,
                answer=answer,
                request_id=request_id,
                timestamp_utc=time.time(),
                intent=intent,
            )
            turns.append(turn)
            if len(turns) > self.max_turns_per_session:
                turns[:] = turns[-self.max_turns_per_session :]
            return turn

    def get_turns(self, session_id: str) -> list[ConversationTurn]:
        safe_session_id = (session_id or "").strip()
        if not safe_session_id:
            return []
        with self._lock:
            turns = self._sessions.get(safe_session_id, [])
            return [ConversationTurn(**vars(t)) for t in turns]

    def get_last_successful_turn(self, session_id: str) -> ConversationTurn | None:
        turns = self.get_turns(session_id)
        for turn in reversed(turns):
            if turn.status == "success" and turn.sql:
                return turn
        return None

    def clear_session(self, session_id: str) -> None:
        safe_session_id = (session_id or "").strip()
        if not safe_session_id:
            return
        with self._lock:
            self._sessions.pop(safe_session_id, None)


class FollowUpIntentDetector:
    """Deterministic intent detector for follow-up turns."""

    _REFERENCE_TERMS = {
        "this",
        "that",
        "those",
        "it",
        "them",
        "these",
        "value",
        "results",
        "result",
    }
    _EXPLAIN_TERMS = {"explain", "interpret", "why", "meaning"}
    _REFINE_TERMS = {
        "what about",
        "specifically",
        "only",
        "now sort by",
        "sort by",
        "instead",
        "filter",
    }
    _METRIC_TERMS = {
        "anxiety",
        "addiction",
        "stress",
        "depression",
        "gender",
        "age",
        "male",
        "female",
        "males",
        "females",
    }

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    def detect(self, question: str, prior_turns: list[ConversationTurn]) -> str:
        normalized = self._normalize(question)
        tokens = set(re.findall(r"[a-zA-Z]+", normalized))
        has_reference = any(term in normalized for term in self._REFERENCE_TERMS)
        has_metric = any(term in normalized for term in self._METRIC_TERMS)
        token_count = len(tokens)

        if not prior_turns:
            if has_reference and not has_metric and token_count <= 6:
                return FollowUpIntent.AMBIGUOUS_REFERENCE
            return FollowUpIntent.NEW_QUERY

        if any(term in normalized for term in self._EXPLAIN_TERMS) and has_reference:
            return FollowUpIntent.EXPLAIN_PREVIOUS_RESULT

        if any(term in normalized for term in self._REFINE_TERMS):
            return FollowUpIntent.REFINE_PREVIOUS_SQL

        if normalized.startswith("what about ") or normalized.startswith("and "):
            return FollowUpIntent.REFINE_PREVIOUS_SQL

        if has_reference and not has_metric and token_count <= 7:
            return FollowUpIntent.AMBIGUOUS_REFERENCE

        return FollowUpIntent.NEW_QUERY


class SQLFollowUpRewriter:
    """Safe SQL rewriter for common follow-up refinements."""

    _ORDER_METRIC_MAP = {
        "anxiety": ("avg_anxiety_score", "avg_anxiety", "anxiety_score"),
        "addiction": ("avg_addiction_level", "avg_addiction", "addiction_level"),
        "stress": ("avg_stress_level", "avg_stress", "stress_level"),
    }
    _WORD_NUMBERS = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    def rewrite(self, previous_sql: str, question: str) -> tuple[str | None, list[str]]:
        sql = (previous_sql or "").strip()
        if not sql:
            return None, []

        lower_sql = sql.lower()
        if " union " in lower_sql:
            return None, []

        normalized_question = self._normalize(question)
        operations: list[str] = []
        updated_sql = sql

        gender_condition = self._extract_gender_condition(normalized_question)
        if gender_condition:
            updated_sql = self._inject_condition(updated_sql, gender_condition)
            operations.append("gender_filter")

        sort_expr, sort_dir = self._extract_sort(normalized_question, updated_sql)
        if sort_expr:
            updated_sql = self._upsert_order_by(updated_sql, sort_expr, sort_dir)
            operations.append("sort_update")

        limit_value = self._extract_limit(normalized_question)
        if limit_value is not None:
            updated_sql = self._upsert_limit(updated_sql, limit_value)
            operations.append("limit_update")

        if not operations:
            return None, []
        return updated_sql, operations

    @staticmethod
    def _extract_gender_condition(question: str) -> str | None:
        if "male" in question or "males" in question or "men" in question:
            return "LOWER(gender) = 'male'"
        if "female" in question or "females" in question or "women" in question:
            return "LOWER(gender) = 'female'"
        return None

    def _extract_sort(self, question: str, sql: str) -> tuple[str | None, str]:
        if "sort by" not in question and "order by" not in question and "highest" not in question and "lowest" not in question:
            return None, "DESC"

        metric_key = None
        for key in self._ORDER_METRIC_MAP:
            if key in question:
                metric_key = key
                break
        if metric_key is None:
            return None, "DESC"

        direction = "DESC"
        if "ascending" in question or "asc" in question or "lowest" in question:
            direction = "ASC"

        sql_lower = sql.lower()
        for candidate in self._ORDER_METRIC_MAP[metric_key]:
            if re.search(rf"\b{re.escape(candidate.lower())}\b", sql_lower):
                return candidate, direction
        return self._ORDER_METRIC_MAP[metric_key][-1], direction

    def _extract_limit(self, question: str) -> int | None:
        digit_match = re.search(r"\b(top|first|limit)\s+(\d+)\b", question)
        if digit_match:
            return max(1, int(digit_match.group(2)))

        word_match = re.search(r"\b(top|first)\s+(one|two|three|four|five|six|seven|eight|nine|ten)\b", question)
        if word_match:
            return self._WORD_NUMBERS[word_match.group(2)]
        return None

    @staticmethod
    def _inject_condition(sql: str, condition: str) -> str:
        clause_match = re.search(r"\b(group\s+by|order\s+by|having|limit)\b", sql, flags=re.IGNORECASE)
        where_exists = re.search(r"\bwhere\b", sql, flags=re.IGNORECASE) is not None

        if clause_match:
            head = sql[: clause_match.start()].rstrip()
            tail = sql[clause_match.start() :].lstrip()
            if where_exists:
                return f"{head} AND {condition} {tail}".strip()
            return f"{head} WHERE {condition} {tail}".strip()

        if where_exists:
            return f"{sql.rstrip()} AND {condition}".strip()
        return f"{sql.rstrip()} WHERE {condition}".strip()

    @staticmethod
    def _upsert_order_by(sql: str, order_expr: str, direction: str) -> str:
        order_match = re.search(r"\border\s+by\b", sql, flags=re.IGNORECASE)
        limit_match = re.search(r"\blimit\b", sql, flags=re.IGNORECASE)
        order_clause = f"ORDER BY {order_expr} {direction}"

        if order_match:
            start = order_match.start()
            end = limit_match.start() if limit_match and limit_match.start() > start else len(sql)
            prefix = sql[:start].rstrip()
            suffix = sql[end:].lstrip()
            return f"{prefix} {order_clause} {suffix}".strip()

        if limit_match:
            prefix = sql[: limit_match.start()].rstrip()
            suffix = sql[limit_match.start() :].lstrip()
            return f"{prefix} {order_clause} {suffix}".strip()
        return f"{sql.rstrip()} {order_clause}".strip()

    @staticmethod
    def _upsert_limit(sql: str, limit_value: int) -> str:
        limit_clause = f"LIMIT {limit_value}"
        limit_match = re.search(r"\blimit\s+\d+\b", sql, flags=re.IGNORECASE)
        if limit_match:
            return f"{sql[:limit_match.start()].rstrip()} {limit_clause}".strip()
        return f"{sql.rstrip()} {limit_clause}".strip()
