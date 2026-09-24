"""SwitchService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import os
from pathlib import Path
from .config import SwitchError, json_bytes
from .service import Service


class SwitchService(Service):
    def use(self, mode, app="all", dry_run=False, discard_changes=False):
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            apps = self.ctx.storage.apps(state, app)
            pending = {}
            notices = []
            for client in apps:
                original, config = self.ctx.clients.read_config(state, client)
                prior = self.ctx.profiles.profile(state["active"][client], client)
                fields, effort_only = self.ctx.profiles.drift(client, config, prior)
                if fields and not effort_only and not discard_changes:
                    raise SwitchError(
                        f"{client} 配置与已保存的 {state['active'][client]} 不一致，字段：{', '.join(fields)}。"
                        f"要保留变更，用 ai-switch capture --app {client}；"
                        f"要按已保存配置切换，用 ai-switch use {mode} --app {app} --discard-changes（会先备份）。"
                    )
                if fields:
                    kind = "推理强度变化" if effort_only else "未保存变更"
                    notices.append(
                        f"{client} {kind}：{', '.join(fields)}；备份当前配置后使用 {mode} 的已保存设置。"
                    )
                profile = self.ctx.profiles.profile(mode, client)
                content = self.ctx.clients.render(client, config, profile, mode)
                if original != content:
                    pending[Path(state["paths"][client])] = content
                state["active"][client] = mode
            if dry_run:
                for notice in notices:
                    print("计划：" + notice)
                print(
                    f"计划切换 {', '.join(apps)} → {mode}；会更新 {len(pending)} 个配置文件。"
                )
                return
            if not pending and state == self.ctx.storage.load():
                print(f"{', '.join(apps)} 已是 {mode}。")
                return
            self.ctx.storage.transaction(pending, state)
        for notice in notices:
            print(notice)
        print(f"已切换 {', '.join(apps)} → {mode}。新启动生效；现有进程不被终止。")

    def capture_current(self, app):
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            files = {}
            for client in self.ctx.storage.apps(state, app):
                mode = state["active"][client]
                _, config = self.ctx.clients.read_config(state, client)
                captured = self.ctx.clients.capture(client, config)
                prior = self.ctx.profiles.profile(mode, client)
                expected = (
                    prior["values"]["model_provider"]
                    if client == "codex"
                    else prior["env"]["ANTHROPIC_BASE_URL"]
                )
                actual = (
                    config.get("model_provider")
                    if client == "codex"
                    else config.get("env", {}).get("ANTHROPIC_BASE_URL")
                )
                if actual != expected:
                    raise SwitchError(
                        f"{client} 当前连接与记录的 {mode} 不符，拒绝覆盖配置。"
                    )
                captured["launch_env"] = prior.get("launch_env", {})
                captured["options"] = prior["options"]
                if client == "codex":
                    env_key = config["model_providers"][actual].get("env_key")
                    if env_key:
                        key = os.environ.get(env_key) or captured["launch_env"].get(
                            env_key
                        )
                        if not key:
                            raise SwitchError(f"缺少凭据环境变量 {env_key}。")
                        captured["launch_env"] = {env_key: key}
                    else:
                        captured["launch_env"] = {}
                self.ctx.profiles.validate_profile(client, captured)
                files[self.ctx.profiles.profile_path(mode, client)] = json_bytes(
                    captured
                )
            self.ctx.storage.transaction(files, state)
        print("已保存当前模式的通道设置；所有固定基准保持不变。")
