from __future__ import annotations

from typing import Any

from hermes_loopguard.backends._json_prompt import build_prompt, build_schema, parse_answers
from hermes_loopguard.backends.base import BackendError, GateResult, Usage
from hermes_loopguard.fingerprint import canonical
from hermes_loopguard.questions import Question

_LLM: Any = None


def set_llm(llm: Any) -> None:
    global _LLM
    _LLM = llm


def get_llm() -> Any:
    return _LLM


class HermesLlmBackend:
    name = "hermes"

    def __init__(self, cfg: dict | None = None) -> None:
        self.global_cfg = cfg or {}
        self.cfg = self.global_cfg.get("hermes", {})

    def available(self) -> bool:
        if _LLM is not None:
            return True
        try:
            import agent.plugin_llm  # noqa: F401

            return True
        except ImportError:
            return False

    def ask(self, state: str | dict, questions: dict[str, Question]) -> GateResult:
        global _LLM
        if _LLM is None:
            try:
                from hermes_cli.plugins import discover_plugins

                discover_plugins()
            except Exception:
                pass

        if _LLM is None:
            raise BackendError("Hermes LLM is not available (set_llm not called)")

        instructions = build_prompt(questions)
        state_str = canonical(state) if isinstance(state, (dict, list)) else str(state)
        schema = build_schema(questions)

        task = self.cfg.get("task", "loopguard_gate")
        max_tokens = self.cfg.get("max_tokens", 400)

        try:
            result = _LLM.complete_structured(
                instructions=instructions,
                input=[{"type": "text", "text": state_str}],
                json_schema=schema,
                schema_name="loopguard.gate",
                task=task,
                temperature=0.0,
                max_tokens=max_tokens,
                purpose="loopguard.gate",
            )
        except Exception as e:
            raise BackendError(f"Hermes LLM complete_structured failed: {e}") from e

        if result is None or getattr(result, "parsed", None) is None:
            raise BackendError("Hermes LLM returned no parsed output")

        parsed = result.parsed
        answers = parse_answers(parsed, questions)

        usage_obj = getattr(result, "usage", None)
        in_tok = int(getattr(usage_obj, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage_obj, "output_tokens", 0) or 0)
        cost_usd = float(getattr(usage_obj, "cost_usd", 0.0) or 0.0)
        usage = Usage(input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost_usd)

        provider = getattr(result, "provider", "hermes")
        model_name = getattr(result, "model", "llm")
        model = f"{provider}/{model_name}"

        return GateResult(
            answers=answers,
            backend=self.name,
            model=model,
            usage=usage,
            raw=result,
        )
