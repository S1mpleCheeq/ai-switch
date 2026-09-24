"""Claude native configuration: capture, render and client-specific validation."""

import copy
import json
from ..config import (
    CLAUDE_FIELDS,
    SwitchError,
    claude_env_key,
    json_bytes,
    replace_fields,
    selected,
    validate_url,
)


def validate_profile(data):
    values = data["values"]
    options = data.get("options", {})
    if any(not claude_env_key(k) for k in data["env"]):
        raise SwitchError("env 只能包含受管理的 Claude 通道变量。")
    if "read_guard" in options and type(options["read_guard"]) is not bool:
        raise SwitchError("read_guard 必须是布尔值。")
    for field in (
        "ultracode",
        "enableArtifact",
        "disableArtifact",
        "enableWorkflows",
        "disableWorkflows",
        "workflowKeywordTriggerEnabled",
    ):
        if field in values and type(values[field]) is not bool:
            raise SwitchError(f"{field} 必须是布尔值。")
    if "effortLevel" in values and values["effortLevel"] not in (
        "low",
        "medium",
        "high",
        "xhigh",
    ):
        raise SwitchError("Claude effort 仅支持 low/medium/high/xhigh。")
    validate_url(data["env"].get("ANTHROPIC_BASE_URL"))
    if options.get("read_guard") and data["env"].get("AI_SWITCH_MODE") not in (
        None,
        "aster",
    ):
        raise SwitchError("Read guard 启用时 AI_SWITCH_MODE 须为空或 aster。")


def decode(data):
    return json.loads(data)


def capture(config, provider_names=()):
    return {
        "values": selected(config, CLAUDE_FIELDS),
        "env": {k: v for k, v in config.get("env", {}).items() if claude_env_key(k)},
    }


def render(config, profile, mode, *, hooks, hook_command):
    result = copy.deepcopy(config)
    replace_fields(result, CLAUDE_FIELDS, profile["values"])
    env = result.setdefault("env", {})
    replace_fields(
        env,
        set(k for k in env if claude_env_key(k)) | set(profile["env"]),
        profile["env"],
    )
    if profile.get("options", {}).get("read_guard", mode == "aster"):
        for event in ("PreToolUse", "PostToolUse"):
            hooks.setdefault(event, []).append(
                {
                    "matcher": "Read|View",
                    "hooks": [{"type": "command", "command": hook_command}],
                }
            )
    if hooks:
        result["hooks"] = hooks
    elif "hooks" in result:
        del result["hooks"]
    return result, json_bytes(result)
