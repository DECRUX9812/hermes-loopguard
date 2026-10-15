from __future__ import annotations

import json
import math
import string
import urllib.request
from typing import Any

from hermes_loopguard import paths
from hermes_loopguard.backends._json_prompt import build_prompt, parse_answers
from hermes_loopguard.backends.base import BackendError, GateResult, Usage
from hermes_loopguard.fingerprint import canonical
from hermes_loopguard.questions import Answer, Choice, Question, choice_answer


class OpenAICompatBackend:
    name = "openai"

    def __init__(self, cfg: dict | None = None) -> None:
        self.global_cfg = cfg or {}
        self.cfg = self.global_cfg.get("openai", {})

    def available(self) -> bool:
        base_url = self.cfg.get("base_url")
        model = self.cfg.get("model")
        api_key_env = self.cfg.get("api_key_env")
        has_key = True if not api_key_env else bool(paths.get_secret(api_key_env))
        return bool(base_url and model and has_key)

    def _post(self, payload: dict) -> dict:
        base_url = (self.cfg.get("base_url") or "").rstrip("/")
        url = f"{base_url}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key_env = self.cfg.get("api_key_env")
        if api_key_env:
            secret = paths.get_secret(api_key_env)
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        timeout = float(self.cfg.get("timeout", 30))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except Exception as e:
            raise BackendError(f"OpenAICompat request failed: {e}") from e

    def _calc_usage(self, usage_dict: dict) -> Usage:
        in_tok = int(usage_dict.get("prompt_tokens", 0) or 0)
        out_tok = int(usage_dict.get("completion_tokens", 0) or 0)
        in_cost = (in_tok * float(self.cfg.get("usd_per_mtok_in", 0.0))) / 1e6
        out_cost = (out_tok * float(self.cfg.get("usd_per_mtok_out", 0.0))) / 1e6
        return Usage(input_tokens=in_tok, output_tokens=out_tok, cost_usd=in_cost + out_cost)

    def _ask_json(self, state_str: str, questions: dict[str, Question]) -> tuple[dict[str, Answer], Usage, Any]:
        prompt = build_prompt(questions)
        payload = {
            "model": self.cfg["model"],
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"State:\n{state_str}"},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        resp = self._post(payload)
        choices = resp.get("choices", [])
        if not choices:
            raise BackendError("OpenAICompat returned no choices")
        content = choices[0].get("message", {}).get("content", "").strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        try:
            obj = json.loads(content)
        except Exception as e:
            raise BackendError(f"Failed to parse JSON response from OpenAI: {e}") from e

        answers = parse_answers(obj, questions)
        usage = self._calc_usage(resp.get("usage", {}))
        return answers, usage, resp

    def ask(self, state: str | dict, questions: dict[str, Question]) -> GateResult:
        state_str = canonical(state) if isinstance(state, (dict, list)) else str(state)
        mode = self.cfg.get("mode", "json")

        if mode == "logprobs":
            # For each Choice question, attempt logprobs request
            answers: dict[str, Answer] = {}
            total_usage = Usage()
            raws = []
            remaining_questions = dict(questions)

            for name, q in list(questions.items()):
                if isinstance(q, Choice):
                    letters = string.ascii_uppercase[: len(q.criteria)]
                    letters_to_cats = {letter: cat for letter, cat in zip(letters, q.criteria.keys())}
                    opts_text = "\n".join(
                        f"  {letter}: {cat}" + (f" ({q.criteria[cat]})" if q.criteria[cat] else "")
                        for letter, cat in zip(letters, q.criteria.keys())
                    )
                    payload = {
                        "model": self.cfg["model"],
                        "messages": [
                            {
                                "role": "system",
                                "content": "Select the single best category option for the given item. Respond with ONLY the single option letter (e.g. A, B, C...).",
                            },
                            {
                                "role": "user",
                                "content": f"State:\n{state_str}\n\nQuestion: {q.instructions}\nOptions:\n{opts_text}",
                            },
                        ],
                        "temperature": 0.0,
                        "max_tokens": 1,
                        "logprobs": True,
                        "top_logprobs": 10,
                    }
                    try:
                        resp = self._post(payload)
                        raws.append(resp)
                        u = self._calc_usage(resp.get("usage", {}))
                        total_usage.input_tokens += u.input_tokens
                        total_usage.output_tokens += u.output_tokens
                        total_usage.cost_usd += u.cost_usd

                        choice_logprobs = resp.get("choices", [{}])[0].get("logprobs")
                        content_lps = choice_logprobs.get("content", []) if choice_logprobs else []
                        top_lps = content_lps[0].get("top_logprobs", []) if content_lps else []

                        if not top_lps:
                            # Fallback to json mode for all
                            break

                        probs: dict[str, float] = {}
                        for item in top_lps:
                            tok = item.get("token", "").strip().upper()
                            if tok in letters_to_cats:
                                cat = letters_to_cats[tok]
                                lp = float(item.get("logprob", -99.0))
                                probs[cat] = math.exp(lp)

                        if not probs:
                            break

                        # Fill any missing categories with 0.0
                        for cat in q.criteria.keys():
                            if cat not in probs:
                                probs[cat] = 0.0

                        answers[name] = choice_answer(probs)
                        remaining_questions.pop(name)
                    except Exception:
                        # Fallback to json mode
                        break

            if not remaining_questions and len(answers) == len(questions):
                return GateResult(
                    answers=answers,
                    backend=self.name,
                    model=self.cfg.get("model", "openai"),
                    usage=total_usage,
                    raw=raws,
                )

            # Fall back to json mode for remaining (or all if break occurred)
            # If fallback needed, re-ask remaining_questions or all
            need_questions = {k: v for k, v in questions.items() if k not in answers}
            if need_questions:
                j_answers, j_usage, j_raw = self._ask_json(state_str, need_questions)
                answers.update(j_answers)
                total_usage.input_tokens += j_usage.input_tokens
                total_usage.output_tokens += j_usage.output_tokens
                total_usage.cost_usd += j_usage.cost_usd
                raws.append(j_raw)

            return GateResult(
                answers=answers,
                backend=self.name,
                model=self.cfg.get("model", "openai"),
                usage=total_usage,
                raw=raws,
            )

        # Default json mode
        answers, usage, raw = self._ask_json(state_str, questions)
        return GateResult(
            answers=answers,
            backend=self.name,
            model=self.cfg.get("model", "openai"),
            usage=usage,
            raw=raw,
        )
