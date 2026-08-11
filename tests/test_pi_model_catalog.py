import json
from pathlib import Path

from app.pi_model_catalog import pi_builtin_model_catalog


def test_pi_builtin_model_catalog_loads_supported_models_from_sibling_build(
    tmp_path: Path,
):
    pi_root = tmp_path / "pi"
    cli_path = pi_root / "packages" / "coding-agent" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("", encoding="utf-8")
    data_dir = pi_root / "packages" / "ai" / "dist" / "providers" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "deepseek.json").write_text(
        json.dumps(
            {
                "openai-completions": {
                    "deepseek-v4-pro": {
                        "id": "deepseek-v4-pro",
                        "name": "DeepSeek V4 Pro",
                        "api": "openai-completions",
                        "provider": "deepseek",
                        "baseUrl": "https://api.deepseek.com",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": 1_000_000,
                        "maxTokens": 384_000,
                    }
                },
                "unsupported-api": {
                    "ignored": {
                        "id": "ignored",
                        "api": "unsupported-api",
                        "provider": "deepseek",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    catalog = pi_builtin_model_catalog(cli_path)

    assert catalog == {
        "deepseek": [
            {
                "provider": "deepseek",
                "id": "deepseek-v4-pro",
                "name": "DeepSeek V4 Pro",
                "api": "openai-completions",
                "baseUrl": "https://api.deepseek.com",
                "reasoning": True,
                "images": False,
                "contextWindow": 1_000_000,
                "maxTokens": 384_000,
            }
        ]
    }


def test_pi_builtin_model_catalog_returns_empty_when_pi_catalog_is_missing(
    tmp_path: Path,
):
    assert pi_builtin_model_catalog(tmp_path / "pi" / "cli.js") == {}
