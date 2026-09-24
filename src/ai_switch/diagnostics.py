"""DiagnosticsService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import hashlib
import json
import os
import shutil
import ssl
import urllib.error
import urllib.request
from .config import SwitchError
from ._version import VERSION
from .service import Service


class DiagnosticsService(Service):
    def report(self):
        state = self.ctx.storage.load()
        result = {
            "version": VERSION,
            "apps": {},
            "last_backup": state.get("last_backup"),
        }
        for app in self.ctx.storage.apps(state):
            _, config = self.ctx.clients.read_config(state, app)
            mode = state["active"][app]
            p = self.ctx.profiles.profile(mode, app)
            capture = self.ctx.clients.capture(app, config)
            expected = self.ctx.profiles.expected_capture(app, p)
            if app == "codex":
                provider = config.get("model_providers", {}).get(
                    config.get("model_provider"), {}
                )
                url = provider.get("base_url")
                sub = config.get("agents", {}).get(
                    "default_subagent_model", "客户端默认"
                )
            else:
                url = config.get("env", {}).get("ANTHROPIC_BASE_URL")
                sub = config.get("env", {}).get(
                    "CLAUDE_CODE_SUBAGENT_MODEL", "客户端默认"
                )
            result["apps"][app] = dict(
                mode=mode,
                matches_profile=capture == expected,
                model=config.get("model"),
                base_url=url,
                subagent=sub,
                config=state["paths"][app],
            )
            fields, effort_only = self.ctx.profiles.drift(app, config, p)
            result["apps"][app].update(changed_fields=fields, effort_only=effort_only)
        return result

    def check(self, network=False):
        report = self.report()
        errors = []
        for name, digest in self.ctx.storage.load().get("asset_hashes", {}).items():
            path = self.ctx.storage.root / "assets" / name
            if (
                not path.exists()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                errors.append(f"资源缺失或已变更：{name}")
        for app, data in report["apps"].items():
            if not data["matches_profile"]:
                if data["effort_only"]:
                    print(
                        f"{app}: 推理强度与已保存配置不同，use/run 时会先备份再应用已保存设置。"
                    )
                else:
                    errors.append(
                        f"{app}: 配置不一致（{', '.join(data['changed_fields'])}）"
                    )
            if not shutil.which(app):
                errors.append(f"{app}: 未安装客户端")
        for mode in self.ctx.profiles.profile_names():
            for app in self.ctx.storage.apps():
                p = self.ctx.profiles.profile(mode, app)
                if app == "codex":
                    provider = p["providers"][p["values"]["model_provider"]]
                    key = (
                        provider.get("experimental_bearer_token")
                        or p.get("launch_env", {}).get(provider.get("env_key"))
                        or os.environ.get(provider.get("env_key", ""))
                    )
                    url = provider["base_url"].rstrip("/") + "/models"
                else:
                    key = p["env"].get("ANTHROPIC_AUTH_TOKEN") or p["env"].get(
                        "ANTHROPIC_API_KEY"
                    )
                    url = p["env"]["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1/models"
                if not key:
                    errors.append(f"{app}/{mode}: 没有可用凭据")
                if network and key:
                    ctx = ssl.create_default_context()
                    if p["options"].get("ca_file"):
                        ctx.load_verify_locations(p["options"]["ca_file"])
                    headers = {
                        "Authorization": "Bearer " + key,
                        "User-Agent": "ai-switch/" + VERSION,
                    }
                    if app == "claude":
                        headers.update(
                            {"anthropic-version": "2023-06-01", "x-api-key": key}
                        )
                    req = urllib.request.Request(url, headers=headers)
                    try:
                        with urllib.request.urlopen(
                            req, context=ctx, timeout=15
                        ) as response:
                            content = json.load(response)
                            print(
                                f"{app}/{mode}: HTTPS + 认证通过，{len(content.get('data', []))} 个模型"
                            )
                    except urllib.error.HTTPError as exc:
                        errors.append(
                            f"{app}/{mode}: HTTP {exc.code}（/models 检查，不代表推理接口结果）"
                        )
                    except (OSError, ValueError) as exc:
                        errors.append(f"{app}/{mode}: {type(exc).__name__}")
        for error in errors:
            print(error)
        if errors:
            raise SwitchError("检查未全部通过。")
        print("配置、凭据引用和客户端检查通过。")
