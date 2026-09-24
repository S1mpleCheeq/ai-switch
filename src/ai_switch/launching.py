"""LaunchService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
import uuid
from .config import SwitchError, claude_env_key, json_bytes
from .platforms import io as platform_io
from .platforms import runtime as platform_runtime
from .platforms import processes as session_process
from .service import Service


class LaunchService(Service):
    def launch(self, args):
        tail = args.client_args
        if tail and tail[0] == "--":
            tail = tail[1:]
        # Validate before any automatic repair can write a compatible copy.
        forbidden = (
            "-c",
            "--config",
            "--settings",
            "--setting-sources",
            "--model",
            "-m",
            "--profile",
            "--remote",
            "--resume",
            "-r",
            "--continue",
            "--ignore-user-config",
            "--fork-session",
            "--session-id",
            "--effort",
            "--last",
        )
        if any(
            v.split("=")[0] in forbidden
            or (
                v.startswith(("-c", "-m", "-r"))
                and not v.startswith("--")
                and len(v) > 2
            )
            for v in tail
        ):
            raise SwitchError(
                "透传参数不能覆盖模式、模型、配置或会话；请使用 ai-switch 参数。"
            )
        takeover = getattr(args, "takeover", False)
        if takeover and (args.app != "codex" or not args.session):
            raise SwitchError("--takeover 仅用于 run codex --session UUID。")
        session = args.session
        if session:
            try:
                session = str(uuid.UUID(session))
            except ValueError:
                raise SwitchError(
                    "续接请提供明确的会话 UUID（用 ai-switch sessions 查找）。"
                ) from None
        state = self.ctx.storage.load()
        self.ctx.storage.apps(state, args.app)
        mode = args.mode or state["active"][args.app]
        profile = self.ctx.profiles.profile(mode, args.app)
        _, live = self.ctx.clients.read_config(state, args.app)
        differences, effort_only = self.ctx.profiles.drift(
            args.app,
            live,
            self.ctx.profiles.profile(state["active"][args.app], args.app),
        )
        if differences and not effort_only:
            raise SwitchError("配置已被外部修改，请先检查 status / capture。")
        needs_use = bool(
            (args.mode and mode != state["active"][args.app]) or effort_only
        )
        if needs_use:
            self.ctx.clients.render(
                args.app, live, profile, mode
            )  # Validate before signalling a process.
        env = dict(os.environ)
        for k in list(env):
            if args.app == "codex" and k == "NODE_EXTRA_CA_CERTS":
                continue  # Shared Node-based MCPs may need the user's existing CA.
            if claude_env_key(k) or k in (
                "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN",
            ):
                del env[k]
        env.update(profile.get("launch_env", {}))
        cwd = Path(args.cwd or os.getcwd()).resolve()
        auto_repair = False
        if (
            args.app == "codex"
            and session
            and not getattr(args, "no_auto_repair", False)
        ):
            provider = profile["providers"][profile["values"]["model_provider"]]
            auto_repair = (
                urlsplit(provider["base_url"]).hostname == "aster.empeirion.cn"
            )
            if auto_repair:
                actual = self.ctx.session_service.reusable_repair(session)
                if actual != session:
                    print(
                        f"自动复用兼容副本：{session} → {actual}；副本中的后续对话保留。"
                    )
                    session = actual
        if session:
            for item in self.ctx.session_service.sessions(
                args.app, 100000, include_subagents=True
            ):
                if item["id"] == session and item["cwd"] and not args.cwd:
                    cwd = Path(item["cwd"])
                    break
        if not cwd.is_dir():
            raise SwitchError(f"工作目录不存在：{cwd}")
        executable = shutil.which(args.app)
        if not executable:
            raise SwitchError(f"找不到客户端：{args.app}")
        # Resolve npm launchers before terminating an old session or switching
        # live configuration. Unsupported shims must leave both untouched.
        command_prefix = platform_runtime.native_command([executable])
        planned_takeover = False
        if args.app == "codex" and session:
            planned_takeover = session_process.ensure_available(
                Path(state["paths"]["codex"]).parent,
                session,
                takeover=takeover,
                dry_run=args.dry_run,
            )
        if needs_use:
            self.ctx.switching.use(mode, args.app, args.dry_run)
            if not args.dry_run:
                state = self.ctx.storage.load()
        if (
            not args.dry_run
            and not self.ctx.diagnostics.report()["apps"][args.app]["matches_profile"]
        ):
            raise SwitchError("配置已被外部修改，请先检查 status / capture。")
        if auto_repair:
            if planned_takeover:
                print(
                    "仅预览：旧进程退出后才检查历史；实际续接 UUID 以届时检查结果为准。"
                )
            else:
                session = self.ctx.session_service.repair_session(
                    session, dry_run=args.dry_run, automatic=True
                )
        cmd = list(command_prefix)
        if args.app == "claude":
            claude_dir = Path(state["paths"]["claude"]).parent.resolve()
            if claude_dir == (Path.home() / ".claude").resolve():
                # Even setting the default directory moves Claude's user MCP
                # config from ~/.claude.json to ~/.claude/.claude.json.
                env.pop("CLAUDE_CONFIG_DIR", None)
            else:
                env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            env.update(profile["env"])
            # Explicit overrides defeat the saved model/effort when resuming.
            overlay = dict(profile["values"], env=profile["env"])
            overlay.setdefault("ultracode", False)
            runtime = self.ctx.storage.root / "runtime" / f"claude-{mode}.json"
            if not args.dry_run:
                platform_io.atomic_write(runtime, json_bytes(overlay))
            cmd += ["--settings", str(runtime), "--model", profile["values"]["model"]]
            if profile["values"].get("effortLevel"):
                cmd += ["--effort", profile["values"]["effortLevel"]]
            if session:
                cmd += ["--resume", session]
        else:
            env["CODEX_HOME"] = str(Path(state["paths"]["codex"]).parent)
            # Explicit overrides pin this launch to the selected profile. Native
            # resume also honors the current user config in Codex 0.155.1.
            cmd += [
                "-c",
                "model_provider=" + json.dumps(profile["values"]["model_provider"]),
                "-m",
                profile["values"]["model"],
            ]
            if profile["values"].get("model_reasoning_effort"):
                cmd += [
                    "-c",
                    "model_reasoning_effort="
                    + json.dumps(profile["values"]["model_reasoning_effort"]),
                ]
            for key, value in profile.get("agents", {}).items():
                cmd += ["-c", f"agents.{key}=" + json.dumps(value)]
            if session:
                cmd += ["resume", session]
        cmd += tail
        print(
            f"{args.app} / {mode} / {profile['values']['model']}，目录 {cwd}",
            flush=True,
        )
        if args.dry_run:
            print(platform_runtime.display_command(cmd))
            return
        if args.app == "codex" and session:
            # If someone else claimed it meanwhile, stop rather than terminating
            # an additional process. Native Codex also enforces its writer lock.
            session_process.ensure_available(
                Path(state["paths"]["codex"]).parent, session
            )
        os.chdir(cwd)
        return platform_runtime.launch(cmd, env)
