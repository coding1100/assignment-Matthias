from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.config import PipelineConfig
from src.pipeline import AnalyticsPipeline
from src.types import AnswerGenerationOutput, SQLGenerationOutput


def _stats(model: str = "test-model", llm_calls: int = 1, prompt: int = 10, completion: int = 5) -> dict:
    return {
        "llm_calls": llm_calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "model": model,
    }


class MultiTurnFakeLLMClient:
    def __init__(self) -> None:
        self.model = "test-model"
        self.sql_calls = 0
        self.answer_calls = 0

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        self.sql_calls += 1
        q = (question or "").lower()
        if "addiction" in q and "gender" in q:
            sql = (
                "SELECT gender, AVG(addiction_level) AS avg_addiction_level "
                "FROM gaming_mental_health GROUP BY gender"
            )
        else:
            sql = (
                "SELECT gender, AVG(anxiety_score) AS avg_anxiety_score "
                "FROM gaming_mental_health GROUP BY gender"
            )
        return SQLGenerationOutput(
            sql=sql,
            timing_ms=1.0,
            llm_stats=_stats(),
            intermediate_outputs=[],
            error=None,
        )

    def generate_answer(self, question: str, sql: str | None, rows: list[dict]) -> AnswerGenerationOutput:
        self.answer_calls += 1
        if not sql:
            answer = "I cannot answer this with the available table and schema. Please rephrase using known survey fields."
        elif not rows:
            answer = "Query executed, but no rows were returned."
        else:
            answer = f"Using {len(rows)} rows, the result suggests a clear pattern."
        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=1.0,
            llm_stats=_stats(),
            intermediate_outputs=[],
            error=None,
        )


class ConversationPipelineUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE gaming_mental_health (
                    age REAL,
                    gender TEXT,
                    addiction_level REAL,
                    anxiety_score REAL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO gaming_mental_health (age, gender, addiction_level, anxiety_score)
                VALUES
                    (22, 'male', 6.0, 7.5),
                    (24, 'male', 5.0, 6.5),
                    (30, 'female', 3.0, 5.2),
                    (32, 'female', 2.5, 4.9);
                """
            )
        self.fake_llm = MultiTurnFakeLLMClient()
        self.pipeline = AnalyticsPipeline(
            db_path=self.db_path,
            llm_client=self.fake_llm,
            config=PipelineConfig.from_env(),
        )

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_follow_up_refine_uses_sql_rewrite_without_new_sql_generation(self) -> None:
        session_id = "session-refine"
        first = self.pipeline.run("What is the average anxiety score for each gender?", session_id=session_id)
        second = self.pipeline.run("What about males specifically?", session_id=session_id)

        self.assertEqual(first.status, "success")
        self.assertEqual(second.status, "success")
        self.assertIsNotNone(second.sql)
        self.assertIn("lower(gender) = 'male'", (second.sql or "").lower())
        self.assertEqual(self.fake_llm.sql_calls, 1, "Follow-up rewrite should avoid an extra SQL generation call.")

        history = self.pipeline.get_conversation_history(session_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["intent"], "refine_previous_sql")

    def test_explain_follow_up_reuses_previous_sql_context(self) -> None:
        session_id = "session-explain"
        first = self.pipeline.run("How does gaming addiction level vary between genders?", session_id=session_id)
        second = self.pipeline.run("Can you explain the highest value?", session_id=session_id)

        self.assertEqual(first.status, "success")
        self.assertEqual(second.status, "success")
        self.assertEqual(self.fake_llm.sql_calls, 1, "Explain follow-up should not regenerate SQL.")
        self.assertEqual(self.fake_llm.answer_calls, 1, "Explain follow-up should avoid extra LLM answer call.")
        self.assertEqual(second.sql, first.sql)
        self.assertIn("leading row", second.answer.lower())

    def test_ambiguous_follow_up_without_context_requests_clarification(self) -> None:
        result = self.pipeline.run("What about that?", session_id="session-ambiguous")
        self.assertEqual(result.status, "unanswerable")
        self.assertIn("clarify", result.answer.lower())

    def test_clear_conversation(self) -> None:
        session_id = "session-clear"
        self.pipeline.run("What is the average anxiety score for each gender?", session_id=session_id)
        self.assertGreaterEqual(len(self.pipeline.get_conversation_history(session_id)), 1)
        self.pipeline.clear_conversation(session_id)
        self.assertEqual(self.pipeline.get_conversation_history(session_id), [])


if __name__ == "__main__":
    unittest.main()
