"""ProfileService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import copy
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from .config import (
    APPS,
    CLAUDE_FIELDS,
    CODEX_EFFORT_FIELDS,
    CODEX_FIELDS,
    MODES,
    SwitchError,
    changed_fields,
    json_bytes,
    profile_name,
    redacted,
    selected,
    validate_url,
)
from .platforms import io as platform_io
from .platforms import runtime as platform_runtime
from . import templates as template_profiles
from .service import Service


class ProfileService(Service):
    def profile(self, mode, app):
        path = self.profile_path(mode, app)
        if not path.is_file():
            raise SwitchError(
                f"配置 {mode} 缺少 {app}；用 ai-switch profile list 查看已有配置。"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return self.normalize_profile(mode, app, data)

    def normalize_profile(self, mode, app, data):
        if not isinstance(data, dict):
            raise SwitchError("profile 根节点必须是 JSON 对象。")
        # Old profiles have no options; interpret them without rewriting anything.
        data.setdefault("launch_env", {})
        if "options" not in data:
            data["options"] = {"read_guard": mode == "aster"} if app == "claude" else {}
            if mode == "aster":
                data["options"]["ca_file"] = str(
                    self.ctx.storage.root / "assets/astergate-ca.crt"
                )
        self.validate_profile(app, data)
        return data

    def profile_path(self, name, app):
        profile_name(name)
        directory = self.ctx.storage.root / "profiles" / name
        path = directory / f"{app}.json"
        if directory.is_symlink() or path.is_symlink():
            raise SwitchError("profile 目录或文件不能是符号链接。")
        return path

    def profile_names(self):
        return sorted(
            p.name
            for p in (self.ctx.storage.root / "profiles").iterdir()
            if p.is_dir() and any((p / f"{a}.json").exists() for a in APPS)
        )

    def managed_provider_names(self):
        names = set(MODES)
        directory = self.ctx.storage.root / "profiles"
        if directory.exists():
            for mode in self.profile_names():
                path = self.profile_path(mode, "codex")
                if path.exists():
                    names.update(
                        json.loads(path.read_text(encoding="utf-8")).get(
                            "providers", {}
                        )
                    )
        return names

    def validate_profile(self, app, data):
        required = (
            {"values", "env"} if app == "claude" else {"values", "agents", "providers"}
        )
        if (
            not isinstance(data, dict)
            or not required <= data.keys()
            or data.keys() - required - {"launch_env", "options"}
        ):
            raise SwitchError("profile 字段不完整或包含未知字段。")
        if any(not isinstance(v, dict) for v in data.values()):
            raise SwitchError("profile 的各配置分组必须是 JSON 对象。")
        if "<redacted>" in json.dumps(data):
            raise SwitchError("不能导入脱敏占位符；请在编辑器或凭据参数中设置真实值。")
        if any(marker in json.dumps(data) for marker in template_profiles.PLACEHOLDERS):
            raise SwitchError(
                "不能使用未替换的公开模板占位符；请用 init 提供自己的凭据和资源。"
            )
        values = data["values"]
        allowed = CLAUDE_FIELDS if app == "claude" else CODEX_FIELDS
        if (
            values.keys() - set(allowed)
            or not isinstance(values.get("model"), str)
            or not values["model"].strip()
        ):
            raise SwitchError("主模型不能为空，values 只能包含受管理的客户端字段。")
        numeric_fields = ("model_context_window", "model_auto_compact_token_limit")
        for field, value in values.items():
            if field in numeric_fields:
                if type(value) is not int or value <= 0:
                    raise SwitchError(f"{field} 必须是正整数。")
            elif field == "modelSettings":
                if not isinstance(value, dict):
                    raise SwitchError("modelSettings 必须是 JSON 对象。")
            elif field not in (
                "ultracode",
                "enableArtifact",
                "disableArtifact",
                "enableWorkflows",
                "disableWorkflows",
                "workflowKeywordTriggerEnabled",
            ):
                if not isinstance(value, str) or not value.strip():
                    raise SwitchError(f"{field} 必须是非空字符串。")
        for section in ("env", "launch_env"):
            for key, value in data.get(section, {}).items():
                if (
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                    or not isinstance(value, str)
                    or "\0" in value
                ):
                    raise SwitchError("环境变量名称无效，或变量值不是字符串。")
        options = data.get("options", {})
        if options.keys() - (
            {"read_guard", "ca_file"} if app == "claude" else {"ca_file"}
        ):
            raise SwitchError("options 包含不支持的选项。")
        if "ca_file" in options and (
            not isinstance(options["ca_file"], str) or not options["ca_file"]
        ):
            raise SwitchError("ca_file 必须是证书文件路径。")
        self.ctx.clients.adapter(app).validate_profile(data)

    def expected_capture(self, app, profile):
        return selected(
            profile,
            ("values", "env") if app == "claude" else ("values", "agents", "providers"),
        )

    def drift(self, app, config, profile):
        fields = changed_fields(
            self.ctx.clients.capture(app, config), self.expected_capture(app, profile)
        )
        if app == "codex":
            fields = [
                f.removeprefix("values.").replace("providers.", "model_providers.", 1)
                for f in fields
            ]
        else:
            fields = [f.removeprefix("values.") for f in fields]
        effort_only = (
            app == "codex" and bool(fields) and set(fields) <= set(CODEX_EFFORT_FIELDS)
        )
        if effort_only:
            effort_only = all(
                config.get(f) is None
                or config[f]
                in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
                for f in fields
            )
        return fields, effort_only

    def list_profiles(self):
        state = self.ctx.storage.load()
        result = []
        for name in self.profile_names():
            apps = {}
            for app in self.ctx.storage.apps(state):
                p = self.profile(name, app)
                url = (
                    p["env"]["ANTHROPIC_BASE_URL"]
                    if app == "claude"
                    else p["providers"][p["values"]["model_provider"]]["base_url"]
                )
                apps[app] = dict(
                    active=state["active"][app] == name,
                    model=p["values"]["model"],
                    base_url=url,
                )
            result.append(dict(name=name, apps=apps))
        return result

    def show_profile(self, name, app="all"):
        state = self.ctx.storage.load()
        return {
            client: redacted(self.profile(name, client))
            for client in self.ctx.storage.apps(state, app)
        }

    def add_profile(self, name, source):
        profile_name(name)
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            if name in self.profile_names():
                raise SwitchError(f"配置 {name} 已存在，不会覆盖。")
            profiles = {
                app: self.profile(source, app) for app in self.ctx.storage.apps(state)
            }
            # Every copied account gets a distinct native provider ID. This also
            # keeps session metadata meaningful even when model/endpoint match.
            if "codex" in profiles:
                provider_id = "ai_switch_" + name
                _, current = self.ctx.clients.read_config(state, "codex")
                if (
                    provider_id in self.managed_provider_names()
                    or provider_id in current.get("model_providers", {})
                ):
                    raise SwitchError(
                        "新配置的 Codex provider ID 与已有配置冲突，请换一个名称。"
                    )
                codex = profiles["codex"]
                provider = copy.deepcopy(
                    codex["providers"][codex["values"]["model_provider"]]
                )
                provider["name"] = name
                codex["values"]["model_provider"] = provider_id
                codex["providers"] = {provider_id: provider}
            files = {}
            for app, p in profiles.items():
                self.validate_profile(app, p)
                files[self.profile_path(name, app)] = json_bytes(p)
            self.ctx.storage.transaction(files, state)
        print(
            f"已从 {source} 新增配置 {name}（含 {', '.join(profiles)}）；当前使用的配置不变。"
        )

    def delete_profile(self, name):
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            if name not in self.profile_names():
                raise SwitchError(f"配置 {name} 不存在。")
            active = [
                app
                for app in self.ctx.storage.apps(state)
                if state["active"][app] == name
            ]
            if active:
                raise SwitchError(
                    f"{name} 正被 {', '.join(active)} 使用；请先用 ai-switch use 切换后再删除。"
                )
            files = {
                self.profile_path(name, app): None
                for app in self.ctx.storage.apps(state)
            }
            runtime = self.ctx.storage.root / "runtime" / f"claude-{name}.json"
            if runtime.exists():
                files[runtime] = None
            self.ctx.storage.transaction(files, state)
        print(f"已删除配置 {name}；恢复备份和原始 Micu 快照保留。")

    def edit_profile(self, args):
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            name, app = args.name, args.app
            self.ctx.storage.apps(state, app)
            original = self.profile(name, app)
            profile = copy.deepcopy(original)
            changes = (
                any(
                    getattr(args, field, None) is not None
                    for field in (
                        "base_url",
                        "model",
                        "effort",
                        "subagent_model",
                        "api_key_env",
                        "read_guard",
                        "ultracode",
                        "catalog",
                    )
                )
                or args.clear_subagent
                or args.clear_catalog
                or args.ask_api_key
            )
            if args.file and (changes or args.editor):
                raise SwitchError("--file 不能和编辑器或其他修改参数一起使用。")
            if args.editor and changes:
                raise SwitchError("--editor 不能和其他修改参数一起使用。")
            if args.file:
                profile = json.loads(
                    Path(args.file).expanduser().read_text(encoding="utf-8")
                )
            elif args.editor or not changes:
                if not sys.stdin.isatty():
                    raise SwitchError(
                        "交互编辑需要终端；也可使用 --base-url / --model / --file 等参数。"
                    )
                editor = platform_runtime.editor_command()
                fd, temporary = tempfile.mkstemp(
                    prefix=f".edit-{name}-", suffix=".json", dir=self.ctx.storage.root
                )
                try:
                    with os.fdopen(fd, "wb") as stream:
                        platform_io.protect_file(temporary)
                        stream.write(json_bytes(profile))
                    command = editor
                    if not command or subprocess.run([*command, temporary]).returncode:
                        raise SwitchError("编辑器退出失败，配置未更新。")
                    profile = json.loads(Path(temporary).read_text(encoding="utf-8"))
                finally:
                    Path(temporary).unlink(missing_ok=True)
            else:
                self.apply_profile_edits(app, profile, args)
            self.validate_profile(app, profile)
            # Keep ownership stable so an editor cannot overwrite a different
            # profile's provider block or leave an untracked old provider behind.
            if app == "codex" and (
                profile["values"]["model_provider"]
                != original["values"]["model_provider"]
                or profile["providers"].keys() != original["providers"].keys()
            ):
                raise SwitchError(
                    "编辑不能更改 Codex provider ID；新增独立 provider 请使用 profile add。"
                )
            files = {self.profile_path(name, app): json_bytes(profile)}
            active = state["active"][app] == name
            if active:
                _, config = self.ctx.clients.read_config(state, app)
                if self.ctx.clients.capture(app, config) != self.expected_capture(
                    app, original
                ):
                    raise SwitchError(
                        "当前客户端配置已被外部修改；请先检查 status / capture。"
                    )
                files[Path(state["paths"][app])] = self.ctx.clients.render(
                    app, config, profile, name
                )
            if original == profile:
                print("配置未变化。")
                return
            self.ctx.storage.transaction(files, state)
        print(
            f"已修改 {name}/{app}；"
            + (
                "已同步当前客户端配置，新启动生效。"
                if active
                else "下次切换到该配置时生效。"
            )
        )

    def apply_profile_edits(self, app, profile, args):
        values = profile["values"]
        if app == "codex" and (
            args.read_guard is not None or args.ultracode is not None
        ):
            raise SwitchError("--read-guard / --ultracode 只适用于 Claude。")
        if app == "claude" and (args.catalog is not None or args.clear_catalog):
            raise SwitchError("--catalog / --clear-catalog 只适用于 Codex。")
        if args.base_url is not None:
            validate_url(args.base_url)
            if app == "claude":
                profile["env"]["ANTHROPIC_BASE_URL"] = args.base_url.rstrip("/")
            else:
                profile["providers"][values["model_provider"]]["base_url"] = (
                    args.base_url.rstrip("/")
                )
        if args.model is not None:
            values["model"] = args.model
            if app == "claude":
                profile["env"]["ANTHROPIC_MODEL"] = args.model
        if args.effort is not None:
            values["effortLevel" if app == "claude" else "model_reasoning_effort"] = (
                args.effort
            )
        if args.subagent_model is not None or args.clear_subagent:
            if app == "claude":
                if args.clear_subagent:
                    for key in list(profile["env"]):
                        if key.startswith("CLAUDE_CODE_SUBAGENT_"):
                            del profile["env"][key]
                else:
                    profile["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = args.subagent_model
            elif args.clear_subagent:
                profile["agents"] = {}
            else:
                profile["agents"]["default_subagent_model"] = args.subagent_model
        if args.read_guard is not None:
            profile["options"]["read_guard"] = args.read_guard == "on"
            if args.read_guard == "on":
                # The hook's guard marker is a feature flag, not the profile name.
                profile["env"]["AI_SWITCH_MODE"] = "aster"
            else:
                profile["env"].pop("AI_SWITCH_MODE", None)
        if args.ultracode is not None:
            values["ultracode"] = args.ultracode == "on"
        if args.catalog is not None:
            path = Path(args.catalog).expanduser().resolve()
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("models"), list):
                raise SwitchError("model catalog 须包含 models 数组。")
            values["model_catalog_json"] = str(path)
        if args.clear_catalog:
            values.pop("model_catalog_json", None)
        if args.ask_api_key or args.api_key_env:
            if args.api_key_env:
                key = os.environ.get(args.api_key_env)
            else:
                if not sys.stdin.isatty():
                    raise SwitchError("--ask-api-key 需要交互终端。")
                key = getpass.getpass("API key（输入不回显）: ")
            if not key or not key.strip():
                raise SwitchError("没有读到 API key；配置未更新。")
            if app == "claude":
                for field in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                    profile["env"].pop(field, None)
                profile["env"]["ANTHROPIC_AUTH_TOKEN"] = key
            else:
                provider = profile["providers"][values["model_provider"]]
                provider.pop("env_key", None)
                provider["experimental_bearer_token"] = key
                provider["requires_openai_auth"] = False
            profile["launch_env"] = {}
