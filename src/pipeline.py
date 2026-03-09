from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from src.config import PipelineConfig
from src.conversation import (
    ConversationTurn,
    FollowUpIntent,
    FollowUpIntentDetector,
    InMemoryConversationStore,
    SQLFollowUpRewriter,
)
from src.llm_client import OpenRouterLLMClient, build_default_llm_client
from src.observability import (
    GLOBAL_METRICS,
    RequestContext,
    configure_logging,
    log_request_end,
    log_request_start,
    traced_stage,
)
from src.types import (
    AnswerGenerationOutput,
    SQLGenerationOutput,
    SQLValidationOutput,
    SQLExecutionOutput,
    PipelineOutput,
)
from src.validation import (
    SQLValidator,
    has_dangerous_intent,
    validate_answer_text,
    validate_rows_shape,
)


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "gaming_mental_health.sqlite"
DEFAULT_CSV_PATH = BASE_DIR / "data" / "gaming_mental_health_10M_40features.csv"


class SQLiteExecutor:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, config: PipelineConfig | None = None) -> None:
        self.db_path = Path(db_path)
        self.config = config or PipelineConfig.from_env()

    def run(self, sql: str | None) -> SQLExecutionOutput:
        start = time.perf_counter()
        error = None
        rows = []
        row_count = 0

        if sql is None:
            return SQLExecutionOutput(
                rows=[],
                row_count=0,
                timing_ms=(time.perf_counter() - start) * 1000,
                error="No SQL to execute.",
            )

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only = ON;")
                timeout_seconds = self.config.sql_statement_timeout_ms / 1000.0
                query_start = time.perf_counter()

                def _progress_handler() -> int:
                    if (time.perf_counter() - query_start) > timeout_seconds:
                        return 1
                    return 0

                conn.set_progress_handler(_progress_handler, 5000)
                cur = conn.cursor()
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchmany(self.config.max_result_rows)]
                row_count = len(rows)
                is_shape_valid, shape_error = validate_rows_shape(rows)
                if not is_shape_valid:
                    error = shape_error
                    rows = []
                    row_count = 0
                conn.set_progress_handler(None, 0)
        except Exception as exc:
            error = str(exc)
            rows = []
            row_count = 0

        return SQLExecutionOutput(
            rows=rows,
            row_count=row_count,
            timing_ms=(time.perf_counter() - start) * 1000,
            error=error,
        )


class AnalyticsPipeline:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        llm_client: OpenRouterLLMClient | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config or PipelineConfig.from_env()
        self._maybe_bootstrap_database()
        configure_logging(self.config.log_level)
        self.llm = llm_client or build_default_llm_client()
        self.executor = SQLiteExecutor(self.db_path, config=self.config)
        self.validator = SQLValidator(self.db_path, config=self.config)
        self._conversation_store = InMemoryConversationStore(
            max_turns_per_session=self.config.conversation_max_turns,
            max_rows_per_turn=self.config.conversation_max_rows_per_turn,
        )
        self._intent_detector = FollowUpIntentDetector()
        self._sql_rewriter = SQLFollowUpRewriter()

    def _maybe_bootstrap_database(self) -> None:
        if self.db_path.exists():
            return
        if not DEFAULT_CSV_PATH.exists():
            return
        try:
            from scripts.gaming_csv_to_db import csv_to_sqlite
            csv_to_sqlite(
                csv_path=DEFAULT_CSV_PATH,
                db_path=self.db_path,
                table_name=self.config.table_name,
                if_exists="replace",
            )
        except Exception:
            # Bootstrap is best-effort; execution-time errors remain explicit.
            return

    @staticmethod
    def _empty_answer_output(answer: str, model: str, error: str | None = None) -> AnswerGenerationOutput:
        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=0.0,
            llm_stats={
                "llm_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "model": model,
            },
            intermediate_outputs=[],
            error=error,
        )

    @staticmethod
    def _format_scalar(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, float):
            return f"{value:.4f}".rstrip("0").rstrip(".")
        return str(value)

    def _build_explain_follow_up_answer(self, previous_turn: ConversationTurn) -> str:
        rows = previous_turn.rows or []
        if not rows:
            return (
                "I can explain the previous result, but there were no rows returned in that result set. "
                "Please rerun the query with broader filters."
            )

        first = rows[0]
        numeric_keys = [k for k, v in first.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        label_keys = [k for k, v in first.items() if not isinstance(v, (int, float)) or isinstance(v, bool)]

        metric_key: str | None = None
        for candidate in ("max", "avg", "count", "sum", "score", "level"):
            for key in numeric_keys:
                if candidate in key.lower():
                    metric_key = key
                    break
            if metric_key:
                break
        if metric_key is None and numeric_keys:
            metric_key = numeric_keys[-1]

        labels: list[str] = []
        for key in label_keys[:2]:
            labels.append(f"{key}={self._format_scalar(first.get(key))}")
        label_text = ", ".join(labels) if labels else "the first returned row"

        if metric_key:
            metric_value = self._format_scalar(first.get(metric_key))
            return (
                f"In the previous result, the leading row is {label_text} with {metric_key}={metric_value}. "
                "Because the prior SQL was ranked for the target metric, this row explains the highest value in that result set."
            )

        return (
            f"In the previous result, the leading row is {label_text}. "
            "This is the top row returned by the prior SQL ordering, which is why it represents the highest value in that result."
        )

    @staticmethod
    def _merge_llm_stats(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "llm_calls": int(base.get("llm_calls", 0)) + int(extra.get("llm_calls", 0)),
            "prompt_tokens": int(base.get("prompt_tokens", 0)) + int(extra.get("prompt_tokens", 0)),
            "completion_tokens": int(base.get("completion_tokens", 0)) + int(extra.get("completion_tokens", 0)),
            "total_tokens": int(base.get("total_tokens", 0)) + int(extra.get("total_tokens", 0)),
            "model": str(base.get("model") or extra.get("model") or "unknown"),
        }

    @staticmethod
    def _should_retry_sql(question: str, validation_output: SQLValidationOutput) -> bool:
        if has_dangerous_intent(question):
            return False
        if validation_output.is_valid:
            return False
        err = (validation_output.error or "").lower()
        if err == "no sql provided":
            return True
        retry_markers = [
            "sql parse/plan error",
            "must reference a table",
            "only table",
            "empty after normalization",
        ]
        return any(marker in err for marker in retry_markers)

    def _build_retry_question(self, question: str, previous_sql: str | None, validation_error: str | None) -> str:
        prev = previous_sql or "null"
        err = validation_error or "unknown validation error"
        return (
            f"Original question: {question}\n"
            f"Previous SQL (invalid): {prev}\n"
            f"Validation error: {err}\n"
            "Return a corrected SQLite SELECT query for this schema."
        )

    @staticmethod
    def _is_obviously_unanswerable(question: str) -> bool:
        q = (question or "").lower()
        unsupported_markers = ("zodiac", "horoscope", "star sign", "astrology")
        return any(marker in q for marker in unsupported_markers)

    @staticmethod
    def _empty_llm_stats(model: str) -> dict[str, Any]:
        return {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model": model,
        }

    def _build_conversation_context(
        self,
        session_id: str,
        turns: list[ConversationTurn],
        last_success_turn: ConversationTurn | None,
        follow_up_intent: str,
    ) -> dict[str, Any]:
        tail = turns[-3:]
        turn_summaries = [
            {
                "turn_id": t.turn_id,
                "question": t.question,
                "status": t.status,
                "sql": t.sql,
                "answer_excerpt": (t.answer or "")[:160],
            }
            for t in tail
        ]
        return {
            "session_id": session_id,
            "turn_count": len(turns),
            "follow_up_intent": follow_up_intent,
            "recent_turns": turn_summaries,
            "last_success_question": (last_success_turn.question if last_success_turn else None),
            "last_success_sql": (last_success_turn.sql if last_success_turn else None),
            "last_success_answer_excerpt": ((last_success_turn.answer or "")[:160] if last_success_turn else None),
        }

    def get_conversation_history(self, session_id: str) -> list[dict[str, Any]]:
        turns = self._conversation_store.get_turns(session_id)
        return [
            {
                "turn_id": t.turn_id,
                "question": t.question,
                "status": t.status,
                "sql": t.sql,
                "answer": t.answer,
                "row_count": len(t.rows),
                "request_id": t.request_id,
                "intent": t.intent,
                "timestamp_utc": t.timestamp_utc,
            }
            for t in turns
        ]

    def clear_conversation(self, session_id: str) -> None:
        self._conversation_store.clear_session(session_id)

    def run(
        self,
        question: str,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> PipelineOutput:
        start = time.perf_counter()
        ctx = RequestContext.create(request_id=request_id)
        log_request_start(ctx, question)
        schema_context: dict[str, Any] = self.validator.schema_context()

        safe_session_id = (session_id or "").strip()
        conversation_turns: list[ConversationTurn] = []
        last_success_turn: ConversationTurn | None = None
        follow_up_intent = FollowUpIntent.NEW_QUERY
        rewritten_sql: str | None = None
        rewrite_operations: list[str] = []
        ambiguous_follow_up = False
        explain_follow_up = False
        blocked_unanswerable = False

        if safe_session_id:
            conversation_turns = self._conversation_store.get_turns(safe_session_id)
            last_success_turn = self._conversation_store.get_last_successful_turn(safe_session_id)
            follow_up_intent = self._intent_detector.detect(question, conversation_turns)
            ambiguous_follow_up = follow_up_intent == FollowUpIntent.AMBIGUOUS_REFERENCE
            explain_follow_up = (
                follow_up_intent == FollowUpIntent.EXPLAIN_PREVIOUS_RESULT
                and last_success_turn is not None
                and last_success_turn.sql is not None
                and bool(last_success_turn.rows)
            )
            if (
                follow_up_intent == FollowUpIntent.REFINE_PREVIOUS_SQL
                and last_success_turn is not None
                and last_success_turn.sql
            ):
                rewritten_sql, rewrite_operations = self._sql_rewriter.rewrite(
                    last_success_turn.sql,
                    question,
                )
            schema_context["conversation"] = self._build_conversation_context(
                session_id=safe_session_id,
                turns=conversation_turns,
                last_success_turn=last_success_turn,
                follow_up_intent=follow_up_intent,
            )

        # Stage 1: SQL Generation
        with traced_stage(ctx, "sql_generation"):
            if ambiguous_follow_up:
                sql_gen_output = SQLGenerationOutput(
                    sql=None,
                    timing_ms=0.0,
                    llm_stats=self._empty_llm_stats(self.llm.model),
                    intermediate_outputs=[
                        {
                            "follow_up_intent": follow_up_intent,
                            "reason": "ambiguous_reference",
                            "session_id": safe_session_id or None,
                        }
                    ],
                    error=None,
                )
            elif explain_follow_up and last_success_turn:
                sql_gen_output = SQLGenerationOutput(
                    sql=last_success_turn.sql,
                    timing_ms=0.0,
                    llm_stats=self._empty_llm_stats(self.llm.model),
                    intermediate_outputs=[
                        {
                            "follow_up_intent": follow_up_intent,
                            "reused_previous_sql": True,
                            "source_turn_id": last_success_turn.turn_id,
                        }
                    ],
                    error=None,
                )
            elif self._is_obviously_unanswerable(question):
                blocked_unanswerable = True
                sql_gen_output = SQLGenerationOutput(
                    sql=None,
                    timing_ms=0.0,
                    llm_stats=self._empty_llm_stats(self.llm.model),
                    intermediate_outputs=[{"blocked_as_unanswerable": True, "reason": "unsupported_domain_keyword"}],
                    error=None,
                )
            elif has_dangerous_intent(question):
                sql_gen_output = SQLGenerationOutput(
                    sql=f'DELETE FROM "{self.config.table_name}"',
                    timing_ms=0.0,
                    llm_stats=self._empty_llm_stats(self.llm.model),
                    intermediate_outputs=[{"blocked_as_invalid_sql": True, "reason": "dangerous_intent"}],
                    error=None,
                )
            elif rewritten_sql:
                sql_gen_output = SQLGenerationOutput(
                    sql=rewritten_sql,
                    timing_ms=0.0,
                    llm_stats=self._empty_llm_stats(self.llm.model),
                    intermediate_outputs=[
                        {
                            "follow_up_intent": follow_up_intent,
                            "sql_rewrite_applied": True,
                            "rewrite_operations": rewrite_operations,
                            "session_id": safe_session_id or None,
                        }
                    ],
                    error=None,
                )
            else:
                sql_gen_output = self.llm.generate_sql(question, schema_context)
                if safe_session_id:
                    sql_gen_output.intermediate_outputs.append(
                        {
                            "follow_up_intent": follow_up_intent,
                            "sql_rewrite_applied": False,
                            "session_id": safe_session_id,
                        }
                    )
        sql = sql_gen_output.sql

        # Stage 2: SQL Validation
        with traced_stage(ctx, "sql_validation"):
            validation_output = self.validator.validate(sql)
            allow_retry = not ambiguous_follow_up and not explain_follow_up and not blocked_unanswerable
            if allow_retry and self._should_retry_sql(question, validation_output):
                retry_question = self._build_retry_question(
                    question=question,
                    previous_sql=sql_gen_output.sql,
                    validation_error=validation_output.error,
                )
                retry_output = self.llm.generate_sql(retry_question, schema_context)
                sql_gen_output.llm_stats = self._merge_llm_stats(sql_gen_output.llm_stats, retry_output.llm_stats)
                sql_gen_output.intermediate_outputs.extend(
                    [
                        {
                            "retry": True,
                            "validation_error": validation_output.error,
                            "retry_sql": retry_output.sql,
                            "retry_error": retry_output.error,
                        },
                        *retry_output.intermediate_outputs,
                    ]
                )
                if retry_output.error and not sql_gen_output.error:
                    sql_gen_output.error = retry_output.error
                if retry_output.sql:
                    sql = retry_output.sql
                    validation_output = self.validator.validate(sql)
                    if validation_output.is_valid:
                        sql_gen_output.sql = sql

        if not validation_output.is_valid:
            sql = None
        else:
            sql = validation_output.validated_sql

        # Stage 3: SQL Execution
        with traced_stage(ctx, "sql_execution"):
            if explain_follow_up and last_success_turn:
                reused_rows = [dict(r) for r in last_success_turn.rows[: self.config.max_result_rows]]
                execution_output = SQLExecutionOutput(
                    rows=reused_rows,
                    row_count=len(reused_rows),
                    timing_ms=0.0,
                    error=None,
                )
            else:
                execution_output = self.executor.run(sql)
        rows = execution_output.rows

        # Stage 4: Answer Generation
        with traced_stage(ctx, "answer_generation"):
            if ambiguous_follow_up:
                answer_output = self._empty_answer_output(
                    answer=(
                        "Your follow-up is ambiguous. Please clarify the metric or filter "
                        "you want to apply (for example: 'for males only' or 'sort by anxiety score')."
                    ),
                    model=self.llm.model,
                    error=None,
                )
            elif explain_follow_up and last_success_turn:
                answer_output = self._empty_answer_output(
                    answer=self._build_explain_follow_up_answer(last_success_turn),
                    model=self.llm.model,
                    error=None,
                )
                answer_output.intermediate_outputs.append(
                    {
                        "local_follow_up_fast_path": True,
                        "intent": follow_up_intent,
                        "source_turn_id": last_success_turn.turn_id,
                    }
                )
            elif execution_output.error and execution_output.error != "No SQL to execute.":
                answer_output = self._empty_answer_output(
                    answer="I could not produce a reliable answer because the SQL query failed to execute.",
                    model=self.llm.model,
                    error=execution_output.error,
                )
            else:
                answer_output = self.llm.generate_answer(
                    question,
                    sql,
                    rows[: self.config.max_rows_for_answer_prompt],
                )

            answer_ok, answer_error = validate_answer_text(answer_output.answer)
            if not answer_ok:
                answer_output = self._empty_answer_output(
                    answer="I cannot answer this with the available table and schema. Please rephrase using known survey fields.",
                    model=self.llm.model,
                    error=answer_error,
                )

        # Determine status
        status = "success"
        if ambiguous_follow_up:
            status = "unanswerable"
        elif sql_gen_output.error:
            status = "error"
        elif not validation_output.is_valid:
            if validation_output.error == "No SQL provided" and not has_dangerous_intent(question):
                status = "unanswerable"
            else:
                status = "invalid_sql"
        elif execution_output.error:
            status = "error"
        elif answer_output.error:
            status = "error"
        elif sql is None:
            status = "unanswerable"

        # Build timings aggregate
        timings = {
            "sql_generation_ms": sql_gen_output.timing_ms,
            "sql_validation_ms": validation_output.timing_ms,
            "sql_execution_ms": execution_output.timing_ms,
            "answer_generation_ms": answer_output.timing_ms,
            "total_ms": (time.perf_counter() - start) * 1000,
        }

        # Build total LLM stats
        total_llm_stats = {
            "llm_calls": int(sql_gen_output.llm_stats.get("llm_calls", 0)) + int(answer_output.llm_stats.get("llm_calls", 0)),
            "prompt_tokens": int(sql_gen_output.llm_stats.get("prompt_tokens", 0)) + int(answer_output.llm_stats.get("prompt_tokens", 0)),
            "completion_tokens": int(sql_gen_output.llm_stats.get("completion_tokens", 0)) + int(answer_output.llm_stats.get("completion_tokens", 0)),
            "total_tokens": int(sql_gen_output.llm_stats.get("total_tokens", 0)) + int(answer_output.llm_stats.get("total_tokens", 0)),
            "model": str(sql_gen_output.llm_stats.get("model", self.llm.model)),
        }
        GLOBAL_METRICS.inc("llm_calls_total", int(total_llm_stats.get("llm_calls", 0)))
        GLOBAL_METRICS.inc("llm_tokens_total", int(total_llm_stats.get("total_tokens", 0)))

        output = PipelineOutput(
            status=status,
            question=question,
            request_id=ctx.request_id,
            sql_generation=sql_gen_output,
            sql_validation=validation_output,
            sql_execution=execution_output,
            answer_generation=answer_output,
            sql=sql,
            rows=rows,
            answer=answer_output.answer,
            timings=timings,
            total_llm_stats=total_llm_stats,
        )

        if safe_session_id:
            self._conversation_store.append_turn(
                safe_session_id,
                question=question,
                status=status,
                sql=sql,
                rows=rows,
                answer=answer_output.answer,
                request_id=ctx.request_id,
                intent=follow_up_intent,
            )

        log_request_end(
            ctx,
            status=status,
            total_ms=timings["total_ms"],
            total_tokens=int(total_llm_stats.get("total_tokens", 0)),
        )

        return output
