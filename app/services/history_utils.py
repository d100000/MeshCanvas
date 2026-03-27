"""Pure functions for building and manipulating LLM conversation histories."""

from __future__ import annotations

from app.core.prompts import BASE_SYSTEM_PROMPT, THINK_PROMPT
from app.models.search import SearchBundle


def build_initial_history(
    user_message: str,
    think_enabled: bool,
    search_bundle: SearchBundle | None,
    model: str,
) -> list[dict[str, str]]:
    history: list[dict[str, str]] = [
        {
            "role": "system",
            "content": f"{BASE_SYSTEM_PROMPT}\n当前模型标识：{model}。",
        }
    ]
    if think_enabled:
        history.append({"role": "system", "content": THINK_PROMPT})
    if search_bundle:
        history.append({"role": "system", "content": search_bundle.as_prompt_block()})
    history.append({"role": "user", "content": user_message})
    return history


def inject_search_bundle(
    histories: dict[str, list[dict[str, str]]],
    search_bundle: SearchBundle,
) -> None:
    prompt_block = search_bundle.as_prompt_block()
    for history in histories.values():
        insert_at = len(history)
        if history and history[-1]["role"] == "user":
            insert_at = len(history) - 1
        history.insert(insert_at, {"role": "system", "content": prompt_block})


def clone_history_to_round(
    history: list[dict[str, str]], source_round: int, *, include_assistant: bool = True
) -> list[dict[str, str]] | None:
    cloned: list[dict[str, str]] = []
    assistant_rounds = 0
    for item in history:
        if item["role"] == "assistant":
            assistant_rounds += 1
            if assistant_rounds >= source_round:
                if include_assistant:
                    cloned.append({"role": item["role"], "content": item["content"]})
                return cloned
        cloned.append({"role": item["role"], "content": item["content"]})
    return None if not include_assistant else cloned


def clone_history_before_assistant_round(
    history: list[dict[str, str]], source_round: int
) -> list[dict[str, str]] | None:
    return clone_history_to_round(history, source_round, include_assistant=False)


def clone_history_until_round(history: list[dict[str, str]], source_round: int) -> list[dict[str, str]]:
    result = clone_history_to_round(history, source_round, include_assistant=True)
    return result if result is not None else []


def parse_discussion_rounds(value: object) -> int:
    try:
        rounds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        rounds = 2
    return max(1, min(rounds, 4))


def parse_source_round(value: object) -> int:
    try:
        round_number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        round_number = 1
    return max(1, round_number)


def parse_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
