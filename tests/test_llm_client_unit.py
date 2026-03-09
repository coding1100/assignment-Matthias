from __future__ import annotations

import unittest

from src.llm_client import OpenRouterLLMClient


class _UsageObj:
    def __init__(self, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _ResponseObj:
    def __init__(self, usage) -> None:
        self.usage = usage


class LLMClientUnitTests(unittest.TestCase):
    def test_extract_usage_from_dict(self) -> None:
        response = {"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
        prompt, completion, total = OpenRouterLLMClient._extract_usage_stats(response)
        self.assertEqual((prompt, completion, total), (11, 7, 18))

    def test_extract_usage_from_object(self) -> None:
        response = _ResponseObj(_UsageObj(prompt_tokens=5, completion_tokens=4, total_tokens=9))
        prompt, completion, total = OpenRouterLLMClient._extract_usage_stats(response)
        self.assertEqual((prompt, completion, total), (5, 4, 9))

    def test_extract_sql_from_json(self) -> None:
        text = '{"sql":"SELECT * FROM gaming_mental_health LIMIT 5"}'
        sql = OpenRouterLLMClient._extract_sql(text)
        self.assertEqual(sql, "SELECT * FROM gaming_mental_health LIMIT 5")

    def test_extract_sql_returns_none_when_missing(self) -> None:
        text = '{"sql": null, "reason": "not answerable"}'
        sql = OpenRouterLLMClient._extract_sql(text)
        self.assertIsNone(sql)


if __name__ == "__main__":
    unittest.main()
