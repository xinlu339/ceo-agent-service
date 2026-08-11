from __future__ import annotations

import json
from pathlib import Path

from app.pi_runner import (
    SUPPORTED_PI_APIS,
    validate_pi_model,
    validate_pi_provider,
)


_MAX_CATALOG_FILE_BYTES = 2 * 1024 * 1024
_MAX_CATALOG_MODELS = 5_000


def pi_builtin_model_catalog(cli_path: Path) -> dict[str, list[dict[str, object]]]:
    """Load credential-free built-in model metadata from the sibling Pi build."""

    data_dir = _pi_model_data_dir(cli_path)
    if data_dir is None:
        return {}

    catalog: dict[str, dict[str, dict[str, object]]] = {}
    model_count = 0
    for path in sorted(data_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            if path.stat().st_size > _MAX_CATALOG_FILE_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for api, models in payload.items():
            if api not in SUPPORTED_PI_APIS or not isinstance(models, dict):
                continue
            for raw_model in models.values():
                if model_count >= _MAX_CATALOG_MODELS:
                    break
                model = _normalized_model(raw_model, fallback_provider=path.stem)
                if model is None or model["api"] != api:
                    continue
                provider = str(model["provider"])
                model_id = str(model["id"])
                catalog.setdefault(provider, {})[model_id] = model
                model_count += 1

    return {
        provider: sorted(
            models.values(),
            key=lambda item: (
                str(item["name"]).casefold(),
                str(item["id"]).casefold(),
            ),
        )
        for provider, models in sorted(catalog.items())
    }


def _pi_model_data_dir(cli_path: Path) -> Path | None:
    try:
        resolved = cli_path.expanduser().resolve()
    except OSError:
        return None
    for parent in resolved.parents:
        for relative in (
            Path("packages/ai/dist/providers/data"),
            Path("packages/ai/src/providers/data"),
        ):
            candidate = parent / relative
            if candidate.is_dir():
                return candidate
    return None


def _normalized_model(
    raw_model: object,
    *,
    fallback_provider: str,
) -> dict[str, object] | None:
    if not isinstance(raw_model, dict):
        return None
    try:
        provider = validate_pi_provider(
            str(raw_model.get("provider") or fallback_provider)
        )
        model_id = validate_pi_model(str(raw_model.get("id") or ""))
    except ValueError:
        return None
    api = str(raw_model.get("api") or "")
    if api not in SUPPORTED_PI_APIS:
        return None
    name = str(raw_model.get("name") or model_id).strip()[:200] or model_id
    base_url = str(raw_model.get("baseUrl") or "").strip()[:2_000]
    inputs = raw_model.get("input")
    input_types = (
        [str(value) for value in inputs if isinstance(value, str)]
        if isinstance(inputs, list)
        else []
    )
    return {
        "provider": provider,
        "id": model_id,
        "name": name,
        "api": api,
        "baseUrl": base_url,
        "reasoning": bool(raw_model.get("reasoning")),
        "images": "image" in input_types,
        "contextWindow": _positive_int(raw_model.get("contextWindow")),
        "maxTokens": _positive_int(raw_model.get("maxTokens")),
    }


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
