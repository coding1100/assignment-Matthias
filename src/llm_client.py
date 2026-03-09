from __future__ import annotations

import json
import os
import time
from typing import Any

from src.types import SQLGenerationOutput, AnswerGenerationOutput

DEFAULT_MODEL = "openai/gpt-5-nano"


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"
    _MAX_RETRIES = 4
    _RETRY_BACKOFF_SEC = 0.4

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except Exception:
            return 0

    @classmethod
    def _extract_usage_stats(cls, response: Any) -> tuple[int, int, int]:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        if usage is None:
            return 0, 0, 0

        if isinstance(usage, dict):
            prompt_tokens = cls._safe_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
            completion_tokens = cls._safe_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
            total_tokens = cls._safe_int(usage.get("total_tokens", prompt_tokens + completion_tokens))
            return prompt_tokens, completion_tokens, total_tokens

        prompt_tokens = cls._safe_int(
            getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0)
        )
        completion_tokens = cls._safe_int(
            getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0)
        )
        total_tokens = cls._safe_int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens))
        return prompt_tokens, completion_tokens, total_tokens

    def _chat(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        extra_params: dict[str, Any] | None = None,
    ) -> str:
        res = None
        last_error: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                request_payload: dict[str, Any] = {
                    "messages": messages,
                    "model": self.model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "reasoning": {"effort": "minimal"},
                    "timeout_ms": 60000,
                    "stream": False,
                }
                if extra_params:
                    request_payload.update(extra_params)
                res = self._client.chat.send(**request_payload)
                break
            except Exception as exc:
                last_error = exc
                if attempt >= self._MAX_RETRIES:
                    raise
                time.sleep(self._RETRY_BACKOFF_SEC * (attempt + 1))
        if res is None:
            raise RuntimeError(f"OpenRouter request failed: {last_error}")

        self._stats["llm_calls"] += 1
        prompt_tokens, completion_tokens, total_tokens = self._extract_usage_stats(res)
        self._stats["prompt_tokens"] += prompt_tokens
        self._stats["completion_tokens"] += completion_tokens
        self._stats["total_tokens"] += total_tokens

        choices = getattr(res, "choices", None) or []
        if not choices:
            raise RuntimeError("OpenRouter response contained no choices.")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_chunks: list[str] = []
            for chunk in content:
                if isinstance(chunk, str):
                    text_chunks.append(chunk)
                elif isinstance(chunk, dict):
                    text_value = chunk.get("text")
                    if isinstance(text_value, str):
                        text_chunks.append(text_value)
            text = "".join(text_chunks).strip()
            if text:
                return text
        reasoning_text = getattr(message, "reasoning", None)
        if isinstance(reasoning_text, str) and reasoning_text.strip():
            return reasoning_text.strip()
        raise RuntimeError("OpenRouter response content is not text.")

    @staticmethod
    def _extract_sql(text: str) -> str | None:
        maybe_json = text.strip()
        if maybe_json.startswith("```"):
            maybe_json = maybe_json.strip("`").strip()
            if maybe_json.lower().startswith("json"):
                maybe_json = maybe_json[4:].strip()
            if maybe_json.lower().startswith("sql"):
                maybe_json = maybe_json[3:].strip()

        if maybe_json.startswith("{") and maybe_json.endswith("}"):
            try:
                parsed = json.loads(maybe_json)
                sql = parsed.get("sql")
                if isinstance(sql, str) and sql.strip():
                    return sql.strip()
                return None
            except json.JSONDecodeError:
                pass

        lower = maybe_json.lower()
        idx_select = lower.find("select ")
        idx_with = lower.find("with ")
        candidates = [x for x in (idx_select, idx_with) if x >= 0]
        if candidates:
            idx = min(candidates)
            return maybe_json[idx:].strip()
        return None

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        system_prompt = (
            "Generate one valid SQLite SELECT query using only provided schema.\n"
            "Requirements:\n"
            "1) Always include a FROM clause referencing the provided table.\n"
            "2) Use SQLite-compatible functions only (avoid MEDIAN, STDDEV, VARIANCE).\n"
            "3) For unanswerable questions, return {\"sql\": null}.\n"
            "Output strict JSON only: {\"sql\":\"...\"} or {\"sql\":null}."
        )
        table_name = str(context.get("table_name", "gaming_mental_health"))
        columns = context.get("columns", [])
        if isinstance(columns, list):
            column_list = ", ".join(str(c) for c in columns)
        else:
            column_list = str(columns)
        conversation_payload = context.get("conversation")
        user_prompt = (
            f"Table: {table_name}\n"
            f"Columns: {column_list}\n"
            f"Question: {question}"
        )
        if conversation_payload:
            user_prompt += (
                "\nConversation context (JSON): "
                f"{json.dumps(conversation_payload, ensure_ascii=True)}\n"
                "If this is a follow-up, preserve prior query scope unless user asks to change it."
            )

        start = time.perf_counter()
        error = None
        sql = None
        intermediate_outputs: list[dict[str, Any]] = []

        try:
            text = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.0,
                max_tokens=170,
                extra_params={"response_format": {"type": "json_object"}},
            )
            intermediate_outputs.append({"raw_response": text})
            sql = self._extract_sql(text)
        except Exception as exc:
            error = str(exc)

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return SQLGenerationOutput(
            sql=sql,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            intermediate_outputs=intermediate_outputs,
            error=error,
        )

    def generate_answer(self, question: str, sql: str | None, rows: list[dict[str, Any]]) -> AnswerGenerationOutput:
        if not sql:
            return AnswerGenerationOutput(
                answer="I cannot answer this with the available table and schema. Please rephrase using known survey fields.",
                timing_ms=0.0,
                llm_stats={"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": self.model},
                error=None,
            )
        if not rows:
            return AnswerGenerationOutput(
                answer="Query executed, but no rows were returned.",
                timing_ms=0.0,
                llm_stats={"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": self.model},
                error=None,
            )

        system_prompt = (
            "You are a concise analytics assistant. "
            "Use only the provided SQL results. Do not invent data."
        )
        user_prompt = (
            f"Question:\n{question}\n\nSQL:\n{sql}\n\n"
            f"Rows (JSON):\n{json.dumps(rows, ensure_ascii=True)}\n\n"
            "Answer in 1-3 sentences using only these rows."
        )

        start = time.perf_counter()
        error = None
        answer = ""
        intermediate_outputs: list[dict[str, Any]] = []

        try:
            answer = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.2,
                max_tokens=220,
            )
            intermediate_outputs.append({"raw_response": answer})
        except Exception as exc:
            error = str(exc)
            answer = f"Error generating answer: {error}"

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            intermediate_outputs=intermediate_outputs,
            error=error,
        )

    def pop_stats(self) -> dict[str, Any]:
        out = dict(self._stats or {})
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return out


def build_default_llm_client() -> OpenRouterLLMClient:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    return OpenRouterLLMClient(api_key=api_key)
