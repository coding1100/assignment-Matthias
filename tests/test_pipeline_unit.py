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


class FakeLLMClient:
    def __init__(self) -> None:
        self.model = "test-model"

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        lower = question.lower()
        if "delete" in lower:
            sql = None
        elif "zodiac" in lower:
            sql = None
        else:
            sql = (
                "SELECT gender, AVG(anxiety_score) AS avg_anxiety "
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
        if not sql:
            answer = "I cannot answer this with the available table and schema. Please rephrase using known survey fields."
            calls = 0
        elif not rows:
            answer = "Query executed, but no rows were returned."
            calls = 0
        else:
            answer = "Male respondents show slightly higher average anxiety in this sample."
            calls = 1
        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=1.0,
            llm_stats=_stats(llm_calls=calls, prompt=8, completion=4),
            intermediate_outputs=[],
            error=None,
        )


class PipelineUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.sqlite"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE gaming_mental_health (
                    age_group TEXT,
                    gender TEXT,
                    addiction_level REAL,
                    anxiety_score REAL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO gaming_mental_health (age_group, gender, addiction_level, anxiety_score)
                VALUES
                    ('18-24', 'male', 5.0, 7.0),
                    ('25-34', 'female', 3.0, 5.0);
                """
            )
        self.pipeline = AnalyticsPipeline(
            db_path=self.db_path,
            llm_client=FakeLLMClient(),
            config=PipelineConfig.from_env(),
        )

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_success_path(self) -> None:
        result = self.pipeline.run("What is average anxiety score by gender?")
        self.assertEqual(result.status, "success")
        self.assertIsNotNone(result.sql)
        self.assertGreater(result.sql_execution.row_count, 0)
        self.assertGreaterEqual(result.total_llm_stats["llm_calls"], 1)

    def test_unanswerable_path(self) -> None:
        result = self.pipeline.run("Which zodiac sign has highest anxiety?")
        self.assertEqual(result.status, "unanswerable")
        self.assertIn("cannot answer", result.answer.lower())

    def test_invalid_sql_path_for_dangerous_intent(self) -> None:
        result = self.pipeline.run("Please delete all rows from the table")
        self.assertEqual(result.status, "invalid_sql")
        self.assertIn("cannot answer", result.answer.lower())


if __name__ == "__main__":
    unittest.main()
