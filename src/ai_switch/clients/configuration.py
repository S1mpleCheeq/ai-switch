"""ConfigurationService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import copy
import os
from pathlib import Path
import shlex
from ..config import SwitchError, plain
from . import claude, codex
from ..platforms import runtime as platform_runtime
from ..service import Service


class ConfigurationService(Service):
    @staticmethod
    def adapter(app):
        return {"claude": claude, "codex": codex}[app]

    def read_config(self, state, app):
        path = Path(state["paths"][app])
        if path.is_symlink():
            raise SwitchError(f"配置是符号链接，请先明确其管理位置：{path}")
        data = path.read_bytes()
        value = self.adapter(app).decode(data)
        if not isinstance(value, dict):
            raise SwitchError(f"配置根节点必须是对象：{path}")
        return data, value

    def hook_command(self):
        return (
            platform_runtime.hook_command(
                self.ctx.storage.root / "assets/read_guard.py"
            )
            if os.name == "nt"
            else "python3 "
            + shlex.quote(str(self.ctx.storage.root / "assets/read_guard.py"))
        )

    def clean_hooks(self, hooks):
        result = copy.deepcopy(hooks)
        for event in list(result):
            groups = []
            for group in result[event]:
                g = copy.deepcopy(group)
                g["hooks"] = [
                    h
                    for h in g.get("hooks", [])
                    if h.get("command") != self.hook_command()
                    and not platform_runtime.is_managed_hook(
                        h.get("command", ""),
                        self.ctx.storage.root / "assets/read_guard.py",
                    )
                ]
                if g["hooks"]:
                    groups.append(g)
            if groups:
                result[event] = groups
            else:
                del result[event]
        return result

    def capture(self, app, config):
        adapter = self.adapter(app)
        providers = self.ctx.profiles.managed_provider_names() if app == "codex" else ()
        return adapter.capture(config, providers)

    def render(self, app, config, profile, mode):
        if app == "claude":
            result, data = claude.render(
                config,
                profile,
                mode,
                hooks=self.clean_hooks(config.get("hooks", {})),
                hook_command=self.hook_command(),
            )
        else:
            result, data = codex.render(
                config,
                profile,
                mode,
                provider_names=self.ctx.profiles.managed_provider_names(),
            )
        return self.original_if_equal(app, result, data, mode)

    def original_if_equal(self, app, result, data, mode):
        # Restore original bytes when common settings have not changed meanwhile.
        baseline = (
            self.ctx.storage.root
            / "baseline"
            / (app + (".json" if app == "claude" else ".toml"))
        )
        if mode == "micu" and baseline.exists():
            raw = baseline.read_bytes()
            original = self.adapter(app).decode(raw)
            if plain(original) == plain(result):
                return raw
        return data
