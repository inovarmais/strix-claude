"""LiteLLM model-name resolution for local cost estimates."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, cast


def _pick_ambiguous_match(
    matches: list[str], prices: set[tuple[Any, Any]], name: str
) -> str | None:
    """Disambiguate multiple provider-prefixed matches for one bare model name."""
    if len(matches) == 1 or len(prices) == 1:
        return matches[0]
    # Multiple third-party resellers price this model differently -- prefer
    # the provider whose own name the model is published under (e.g.
    # "minimax/MiniMax-M3") over unrelated rehosts.
    canonical = [key for key in matches if name.lower().startswith(key.split("/", 1)[0].lower())]
    return canonical[0] if len(canonical) == 1 else None


@lru_cache(maxsize=512)
def resolve_litellm_model(model: str) -> str | None:
    """Return a provider-qualified model name that LiteLLM can price."""
    try:
        import litellm

        normalized = model.strip()
        for prefix in ("litellm/", "any-llm/", "openai/"):
            if normalized.startswith(prefix):
                normalized = normalized.removeprefix(prefix)
                break
        if not normalized:
            return None

        model_cost = cast(
            "dict[str, dict[str, Any]]",
            getattr(litellm, "model_cost"),  # noqa: B009
        )
        bare_entry = model_cost.get(normalized)
        if "/" not in normalized and isinstance(bare_entry, dict):
            provider = bare_entry.get("litellm_provider")
            if isinstance(provider, str) and provider:
                return f"{provider}/{normalized}"
        if "/" in normalized and isinstance(bare_entry, dict):
            return normalized

        names = [normalized]
        if "/" in normalized:
            names.append(normalized.rsplit("/", 1)[-1])
        for name in names:
            matches = sorted(key for key in model_cost if key.endswith(f"/{name}"))
            if not matches:
                continue
            prices = {
                (
                    model_cost[key].get("input_cost_per_token"),
                    model_cost[key].get("output_cost_per_token"),
                )
                for key in matches
                if isinstance(model_cost.get(key), dict)
            }
            picked = _pick_ambiguous_match(matches, prices, name)
            if picked is not None:
                return picked
        return None  # noqa: TRY300
    except Exception:  # noqa: BLE001
        return None
