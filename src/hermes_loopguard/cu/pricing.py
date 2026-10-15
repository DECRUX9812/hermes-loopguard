"""Per-model list prices (USD per million tokens) for computer-use calls.

All numbers are the provider's public list price for the underlying model,
even when the actual access route is flat-rate (CLIProxy subscription) or
free-at-output (Jev). The bench prices both arms with this same table.

Sources:
- Jev (input $0.042/Mtok, output tokens free):
  https://docs.typesafe.ai/models ("You pay per input token. Output tokens are free.")
- Gemini 3.8 Flash (underlying model of CLIProxy `gemini-3.8-flash-high`),
  $0.75 in / $3.75 out per Mtok (promo 50% off through 2026-12-31, list
  $1.50/$7.50; the promo price is used here):
  https://ai.google.dev/gemini-api/docs/pricing (aggregated 2026-10-14 by
  https://costgoat.com/pricing/gemini-api)
- Claude Opus 4.6 (underlying model of `openai:claude-opus-4-6-thinking`),
  $5 in / $25 out per Mtok:
  https://platform.claude.com/docs/en/about-claude/pricing
- Gemini 3.1 Flash-Lite (the jev_computer planner model,
  `computer_use.planner_model` on CLIProxy), $0.25 in / $1.50 out per Mtok:
  https://pricepertoken.com/pricing-page/model/google-gemini-3.1-flash-lite-pre
  (retrieved 2026-10-15; same figures on https://ai.google.dev/gemini-api/docs/pricing)
"""
from __future__ import annotations

# model id -> (input $/Mtok, output $/Mtok)
USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "jev-1.14.0": (0.042, 0.0),
    "jev-1.13.0": (0.042, 0.0),
    "gemini-3.8-flash-high": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
    "claude-opus-4-6-thinking": (5.0, 25.0),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}


def usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """List-price cost of a call, in USD. Unknown models price at $0."""
    in_price, out_price = USD_PER_MTOK.get(model, (0.0, 0.0))
    return (input_tokens * in_price + output_tokens * out_price) / 1e6


def is_priced(model: str) -> bool:
    """False when `usd()` would silently price this model at $0."""
    return model in USD_PER_MTOK


def unpriced_models(models: list[str]) -> list[str]:
    """Models in `models` (callers pass every model any arm may call) that
    have NO price row.  The bench fails loudly on these instead of letting a
    run look free."""
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m and m not in seen and not is_priced(m):
            seen.add(m)
            out.append(m)
    return out
