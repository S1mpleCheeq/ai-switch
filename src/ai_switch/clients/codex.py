"""Codex native configuration: capture, render and client-specific validation."""

import copy
import tomlkit
from ..config import (
    AGENT_FIELDS,
    CODEX_FIELDS,
    SwitchError,
    replace_fields,
    selected,
    validate_url,
)


def validate_profile(data):
    values = data["values"]
    options = data.get("options", {})
    if data["agents"].keys() - set(AGENT_FIELDS) or any(
        not isinstance(v, str) for v in data["agents"].values()
    ):
        raise SwitchError("agents 只能包含子代理模型及其推理强度。")
    provider_id = values.get("model_provider")
    if not isinstance(provider_id, str) or provider_id not in data["providers"]:
        raise SwitchError("model_provider 必须指向本 profile 中的 provider。")
    for provider in data["providers"].values():
        if not isinstance(provider, dict):
            raise SwitchError("provider 必须是 JSON 对象。")
        if "base_url" in provider:
            validate_url(provider["base_url"])
    provider = data["providers"][provider_id]
    validate_url(provider.get("base_url"))
    if provider.get("wire_api") != "responses" or not isinstance(
        provider.get("name"), str
    ):
        raise SwitchError('Codex provider 需要 name 和 wire_api="responses"。')
    for field in ("env_key", "experimental_bearer_token"):
        if field in provider and (
            not isinstance(provider[field], str) or not provider[field]
        ):
            raise SwitchError("Codex 凭据字段必须是非空字符串。")
    for field in ("model_reasoning_effort", "plan_mode_reasoning_effort"):
        if field in values and values[field] not in (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        ):
            raise SwitchError("Codex reasoning effort 值无效。")
    # Validate representability before committing any JSON profile or live TOML.
    tomlkit.dumps(
        dict(values, agents=data["agents"], model_providers=data["providers"])
    )


def decode(data):
    return tomlkit.parse(data.decode())


def capture(config, provider_names):
    return {
        "values": selected(config, CODEX_FIELDS),
        "agents": selected(config.get("agents", {}), AGENT_FIELDS),
        "providers": selected(config.get("model_providers", {}), provider_names),
    }


def render(config, profile, mode, *, provider_names):
    result = copy.deepcopy(config)
    replace_fields(result, CODEX_FIELDS, profile["values"])
    if "agents" not in result and profile["agents"]:
        result["agents"] = tomlkit.table()
    if "agents" in result:
        replace_fields(result["agents"], AGENT_FIELDS, profile["agents"])
        if not result["agents"]:
            del result["agents"]
    if "model_providers" not in result:
        result["model_providers"] = tomlkit.table()
    replace_fields(
        result["model_providers"],
        provider_names | set(profile["providers"]),
        profile["providers"],
    )
    data = tomlkit.dumps(result).encode()
    tomlkit.parse(data.decode())
    return result, data
