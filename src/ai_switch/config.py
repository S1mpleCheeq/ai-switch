"""Shared profile schema, validation helpers and redaction; no filesystem writes."""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit

MODES = (
    "micu",
    "aster",
)  # Bootstrap templates; runtime profiles are discovered on disk.
APPS = ("claude", "codex")
CLAUDE_FIELDS = (
    "model",
    "effortLevel",
    "modelSettings",
    "ultracode",
    "enableArtifact",
    "disableArtifact",
    "enableWorkflows",
    "disableWorkflows",
    "workflowKeywordTriggerEnabled",
)
CODEX_FIELDS = (
    "model",
    "model_provider",
    "model_reasoning_effort",
    "model_catalog_json",
    "model_instructions_file",
    "model_context_window",
    "model_auto_compact_token_limit",
    "model_auto_compact_token_limit_scope",
    "service_tier",
    "plan_mode_reasoning_effort",
)
AGENT_FIELDS = ("default_subagent_model", "default_subagent_reasoning_effort")
CODEX_EFFORT_FIELDS = ("model_reasoning_effort", "plan_mode_reasoning_effort")
CLAUDE_EXTRA_ENV = (
    "NODE_EXTRA_CA_CERTS",
    "AI_SWITCH_MODE",
    "FORCE_READ_GUARD",
    "READ_GUARD_MODELS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


class SwitchError(Exception):
    pass


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def claude_env_key(key):
    return (
        key.startswith("ANTHROPIC_")
        or key.startswith("CLAUDE_CODE_SUBAGENT_")
        or key in CLAUDE_EXTRA_ENV
    )


def plain(value):
    return value.unwrap() if hasattr(value, "unwrap") else copy.deepcopy(value)


def selected(source, keys):
    return {k: plain(source[k]) for k in keys if k in source}


def replace_fields(target, keys, values):
    for key in keys:
        if key in values:
            target[key] = copy.deepcopy(values[key])
        elif key in target:
            del target[key]


def changed_fields(current, saved, prefix=""):
    """Return field paths only: diagnostics must never include credentials."""
    if isinstance(current, dict) and isinstance(saved, dict):
        paths = []
        for key in sorted(set(current) | set(saved)):
            path = prefix + key
            if key not in current or key not in saved:
                paths.append(path)
            else:
                paths.extend(changed_fields(current[key], saved[key], path + "."))
        return paths
    return [] if current == saved else [prefix.rstrip(".")]


def profile_name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value
    ):
        raise SwitchError(
            "配置名须为 1～64 位字母、数字、下划线或短横线，并以字母或数字开头。"
        )
    return value


def validate_url(value):
    if not isinstance(value, str):
        raise SwitchError("base_url 必须是 HTTP(S) 地址。")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ("https", "http") and parsed.hostname and parsed.port != 0
        )
    except ValueError:
        valid = False
    if (
        not valid
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SwitchError(
            "base_url 必须是 HTTP(S) 地址，不能包含用户名、密码、查询参数或片段。"
        )


def redacted(value, parent=""):
    if isinstance(value, dict):
        return {
            k: (
                "<redacted>"
                if parent in ("launch_env", "http_headers", "env_http_headers")
                or re.search(
                    r"key|(?:^|_)token$|secret|password|authorization|credential",
                    k,
                    re.I,
                )
                and k not in ("env_key", "workflowKeywordTriggerEnabled")
                else redacted(v, k)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redacted(v, parent) for v in value]
    return value
