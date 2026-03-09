from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.config import PipelineConfig
from src.validation import SQLValidator, has_dangerous_intent


class SQLValidatorTests(unittest.TestCase):
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

        self.config = PipelineConfig.from_env()
        self.validator = SQLValidator(self.db_path, config=self.config)

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_valid_select_is_accepted_and_limited(self) -> None:
        out = self.validator.validate("SELECT gender, AVG(anxiety_score) FROM gaming_mental_health GROUP BY gender")
        self.assertTrue(out.is_valid)
        self.assertIsNotNone(out.validated_sql)
        self.assertIn("LIMIT", out.validated_sql.upper())

    def test_delete_is_blocked(self) -> None:
        out = self.validator.validate("DELETE FROM gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIn("Only SELECT statements are allowed", out.error or "")

    def test_multiple_statements_are_blocked(self) -> None:
        out = self.validator.validate("SELECT * FROM gaming_mental_health; SELECT 1")
        self.assertFalse(out.is_valid)
        self.assertIn("Multiple SQL statements", out.error or "")

    def test_wrong_table_is_blocked(self) -> None:
        out = self.validator.validate("SELECT * FROM some_other_table")
        self.assertFalse(out.is_valid)
        self.assertIn("Only table", out.error or "")

    def test_sqlite_unsupported_function_is_sanitized(self) -> None:
        out = self.validator.validate(
            "SELECT gender, STDDEV(anxiety_score) AS spread FROM gaming_mental_health GROUP BY gender"
        )
        self.assertTrue(out.is_valid)
        self.assertIn("AVG(", out.validated_sql or "")

    def test_missing_from_clause_is_sanitized(self) -> None:
        out = self.validator.validate(
            "SELECT age_group, AVG(anxiety_score) AS avg_anxiety GROUP BY age_group ORDER BY avg_anxiety ASC LIMIT 1"
        )
        self.assertTrue(out.is_valid)
        self.assertIn('FROM "gaming_mental_health"', out.validated_sql or "")

    def test_dangerous_intent_detection(self) -> None:
        self.assertTrue(has_dangerous_intent("Please delete all rows"))
        self.assertFalse(has_dangerous_intent("Show average anxiety by gender"))


if __name__ == "__main__":
    unittest.main()
