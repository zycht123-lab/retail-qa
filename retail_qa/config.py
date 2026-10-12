"""Project and model configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_settings(path: Path | None = None) -> dict[str, Any]:
    config_path = (path or PROJECT_ROOT / "config.toml").resolve()
    with config_path.open("rb") as file:
        settings = tomllib.load(file)
    settings["database"] = (config_path.parent / settings["database"]).resolve()
    settings["model_config"] = (config_path.parent / settings["model_config"]).resolve()
    return settings


def make_model_client(settings: dict[str, Any], alias: str | None = None) -> tuple[OpenAI, dict[str, Any]]:
    with Path(settings["model_config"]).open("rb") as file:
        shared = tomllib.load(file)
    alias = alias or settings.get("models", {}).get("router") or settings.get("model")
    model = shared["models"].get(alias)
    if model is None:
        raise ValueError(f"未知模型 {alias}；可选：{', '.join(shared['models'])}")
    provider_name = model["provider"]
    provider = shared["providers"][provider_name]
    key = os.getenv(provider.get("env_key", "")) or provider.get("api_key", "")
    if not key:
        raise ValueError(f"缺少 {provider_name} 的 API Key；请填写相邻 agent-demo/config.toml")
    client = OpenAI(api_key=key, base_url=provider["base_url"], timeout=60, max_retries=0)
    return client, model
