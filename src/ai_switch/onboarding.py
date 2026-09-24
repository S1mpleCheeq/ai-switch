"""OnboardingService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import copy
import getpass
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
from importlib.resources import files as resource_files
from .config import APPS, SwitchError, json_bytes
from .platforms import io as platform_io
from . import templates as template_profiles
from .service import Service


class OnboardingService(Service):
    def init(self, args):
        with self.ctx.storage.lock():
            if self.ctx.storage.state_path.exists():
                raise SwitchError("已经初始化，保留现有 Micu 基准；不会重复覆盖。")
            selected_app = getattr(args, "app", "all")
            apps = APPS if selected_app == "all" else (selected_app,)
            paths = {
                app: str(
                    Path(getattr(args, app + "_dir")).expanduser().resolve()
                    / ("settings.json" if app == "claude" else "config.toml")
                )
                for app in apps
            }
            state = {
                "version": 1,
                "paths": paths,
                "active": {a: "micu" for a in apps},
                "last_backup": None,
            }
            originals = {}
            configs = {}
            for app in apps:
                originals[app], configs[app] = self.ctx.clients.read_config(state, app)
            if "codex" in apps and configs["codex"].get("model_provider") != "micu":
                raise SwitchError(
                    "初始化需要 Codex 当前使用 micu，以免保存错误的恢复基准。"
                )
            if "claude" in apps and "micu" not in configs["claude"].get("env", {}).get(
                "ANTHROPIC_BASE_URL", ""
            ):
                raise SwitchError("初始化需要 Claude 当前使用 Micu。")
            catalog_bytes = None
            if "codex" in apps:
                if not args.catalog:
                    raise SwitchError(
                        "初始化 Codex 需要 --catalog 模型目录路径；仅 Claude 可省略。"
                    )
                catalog_bytes = Path(args.catalog).expanduser().read_bytes()
                catalog = json.loads(catalog_bytes)
                slugs = {m["slug"] for m in catalog["models"]}
                if not {"gpt-6-astra", "gemini-3.8-flash-high"} <= slugs:
                    raise SwitchError("模型目录缺少主模型或 Gemini 子代理。")
            ca_bytes = Path(args.ca).expanduser().read_bytes()
            ssl.create_default_context(cadata=ca_bytes.decode())
            assets = self.ctx.storage.root / "assets"
            profiles = {a: self.ctx.clients.capture(a, configs[a]) for a in apps}
            for app in apps:
                p = profiles[app]
                p["launch_env"] = {}
                p["options"] = {"read_guard": False} if app == "claude" else {}
                if app == "codex":
                    provider = configs[app]["model_providers"]["micu"]
                    env_key = provider.get("env_key")
                    if env_key:
                        if not os.environ.get(env_key):
                            raise SwitchError(f"缺少 Micu 凭据环境变量 {env_key}。")
                        p["launch_env"][env_key] = os.environ[env_key]
                self.ctx.profiles.validate_profile(app, p)
            if getattr(args, "ask_api_key", False):
                if not sys.stdin.isatty():
                    raise SwitchError(
                        "--ask-api-key 需要交互终端；自动化请使用 --aster-key-env。"
                    )
                key = getpass.getpass("AsterGate API key（输入不回显）: ")
            else:
                key_env = args.aster_key_env or "ASTERGATE_API_KEY"
                key = os.environ.get(key_env)
                if not key:
                    raise SwitchError(
                        f"缺少 AsterGate 凭据环境变量 {key_env}；也可使用 --ask-api-key。"
                    )
            if not key or not key.strip():
                raise SwitchError("没有读到 AsterGate API key；未初始化。")
            aster_profiles = {}
            for app in apps:
                profile = template_profiles.render(app, key, assets)
                if app == "codex":
                    profile["providers"] = dict(
                        copy.deepcopy(profiles[app]["providers"]),
                        **profile["providers"],
                    )
                else:
                    inherited = {
                        k: v
                        for k, v in profiles[app]["env"].items()
                        if "MODEL" not in k
                        and not k.startswith("CLAUDE_CODE_USE_")
                        and k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
                    }
                    profile["env"] = dict(inherited, **profile["env"])
                self.ctx.profiles.validate_profile(app, profile)
                aster_profiles[app] = profile
            if catalog_bytes is not None:
                platform_io.atomic_write(assets / "model-catalog.json", catalog_bytes)
            platform_io.atomic_write(assets / "astergate-ca.crt", ca_bytes)
            if "claude" in apps:
                platform_io.atomic_write(
                    assets / "read_guard.py",
                    resource_files("ai_switch.resources")
                    .joinpath("read_guard.py")
                    .read_bytes(),
                )
            for app, p in profiles.items():
                platform_io.atomic_write(
                    self.ctx.storage.root
                    / "baseline"
                    / (app + (".json" if app == "claude" else ".toml")),
                    originals[app],
                )
                platform_io.atomic_write(
                    self.ctx.storage.root / "profiles/micu" / f"{app}.json",
                    json_bytes(p),
                )
            for app, profile in aster_profiles.items():
                platform_io.atomic_write(
                    self.ctx.storage.root / "profiles/aster" / f"{app}.json",
                    json_bytes(profile),
                )
            # Auth is unchanged by use/run; keep a recovery snapshot only.
            if "codex" in apps:
                auth = Path(paths["codex"]).with_name("auth.json")
                if auth.exists():
                    platform_io.atomic_write(
                        self.ctx.storage.root / "baseline/codex-auth.json",
                        auth.read_bytes(),
                    )
            pinned = copy.deepcopy(profiles)
            for path, data in self.ctx.baselines.original_snapshot_files(
                pinned
            ).items():
                platform_io.atomic_write(path, data)
            for path, data in self.ctx.baselines.named_baseline_files(
                "aster", aster_profiles, configs, "init"
            ).items():
                platform_io.atomic_write(path, data)
            state["asset_hashes"] = {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in assets.iterdir()
            }
            platform_io.atomic_write(self.ctx.storage.state_path, json_bytes(state))
        print(
            f"已保存 Micu 基准并准备 {', '.join(apps)} 的 AsterGate 配置。当前仍为 Micu。"
        )
