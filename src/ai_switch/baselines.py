"""BaselineService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import base64
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tomlkit
from .config import SwitchError, json_bytes, profile_name
from .service import Service


class BaselineService(Service):
    def original_snapshot_files(self, profiles):
        directory = self.ctx.storage.root / "baseline"
        snapshot = directory / "original-profiles.json"
        manifest = directory / "original-manifest.json"
        if snapshot.exists() or manifest.exists():
            raise SwitchError(
                "原始基准已固定或不完整；拒绝覆盖。请用 baseline status 检查。"
            )
        payload = json_bytes(profiles)
        hashes = {snapshot.name: hashlib.sha256(payload).hexdigest()}
        for name in ("claude.json", "codex.toml", "codex-auth.json"):
            path = directory / name
            if path.exists():
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            snapshot: payload,
            manifest: json_bytes(
                {
                    "version": 1,
                    "files": hashes,
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            ),
        }

    def original_profiles(self):
        directory = self.ctx.storage.root / "baseline"
        manifest = directory / "original-manifest.json"
        if not manifest.exists():
            raise SwitchError(
                "旧版原始快照尚未固定凭据与校验值；请先运行 ai-switch baseline protect。"
            )
        document = json.loads(manifest.read_text(encoding="utf-8"))
        hashes = document.get("files", {})
        apps = self.ctx.storage.apps()
        required = {"original-profiles.json"} | {
            app + (".json" if app == "claude" else ".toml") for app in apps
        }
        if (
            document.get("version") != 1
            or not required <= hashes.keys()
            or hashes.keys() - required - {"codex-auth.json"}
        ):
            raise SwitchError("原始基准校验清单无效，拒绝恢复。")
        if (directory / "codex-auth.json").exists() != ("codex-auth.json" in hashes):
            raise SwitchError("原始 Codex 登录快照与校验清单不一致，拒绝恢复。")
        for name, digest in hashes.items():
            path = directory / name
            if (
                path.is_symlink()
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                raise SwitchError(f"原始基准文件缺失或已被修改：{name}；拒绝恢复。")
        profiles = json.loads(
            (directory / "original-profiles.json").read_text(encoding="utf-8")
        )
        if set(profiles) != set(apps):
            raise SwitchError("原始基准必须包含全部已初始化客户端。")
        for app, profile in profiles.items():
            self.ctx.profiles.validate_profile(app, profile)
        return profiles

    def protect_original(self):
        """One-time migration: never use a changed working profile as baseline."""
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            if (self.ctx.storage.root / "baseline/original-manifest.json").exists():
                self.original_profiles()
                print("原始 Micu 基准已固定且校验通过，不会重复覆盖。")
                return
            profiles = {}
            for app in self.ctx.storage.apps(state):
                path = (
                    self.ctx.storage.root
                    / "baseline"
                    / (app + (".json" if app == "claude" else ".toml"))
                )
                if path.is_symlink():
                    raise SwitchError("原始快照不能是符号链接。")
                config = (
                    json.loads(path.read_bytes())
                    if app == "claude"
                    else tomlkit.parse(path.read_text(encoding="utf-8"))
                )
                original = self.ctx.clients.capture(app, config)
                saved = self.ctx.profiles.profile("micu", app)
                if original != self.ctx.profiles.expected_capture(app, saved):
                    raise SwitchError(
                        f"{app} 的 Micu profile 已与原始快照不同，无法安全固定旧版凭据；未修改基准。"
                    )
                original["launch_env"] = copy.deepcopy(saved.get("launch_env", {}))
                original["options"] = {"read_guard": False} if app == "claude" else {}
                self.ctx.profiles.validate_profile(app, original)
                profiles[app] = original
            self.ctx.storage.transaction(self.original_snapshot_files(profiles), state)
        print(
            "已固定初始化时的 Micu 配置及已保存凭据；edit/capture/delete 不会覆盖此基准。"
        )

    def baseline_status(self, name="micu"):
        self.ctx.storage.load()
        profiles = (
            self.original_profiles()
            if name == "micu"
            else self.named_baseline(name)["profiles"]
        )
        for app, profile in profiles.items():
            print(
                f"{name}/{app}: 固定基准校验通过 | 主模型 {profile['values']['model']}"
            )

    def restore_original(self, app="all", full_config=False, dry_run=False):
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            profiles = self.original_profiles()
            apps = self.ctx.storage.apps(state, app)
            files = {}
            for client in apps:
                profile = profiles[client]
                live = Path(state["paths"][client])
                if live.is_symlink():
                    raise SwitchError(f"配置是符号链接，请先明确其管理位置：{live}")
                if full_config:
                    original = (
                        self.ctx.storage.root
                        / "baseline"
                        / (client + (".json" if client == "claude" else ".toml"))
                    )
                    content = original.read_bytes()
                    if (
                        client == "codex"
                        and (
                            self.ctx.storage.root / "baseline/codex-auth.json"
                        ).exists()
                    ):
                        auth = live.with_name("auth.json")
                        if auth.is_symlink():
                            raise SwitchError("Codex auth.json 是符号链接，拒绝覆盖。")
                        files[auth] = (
                            self.ctx.storage.root / "baseline/codex-auth.json"
                        ).read_bytes()
                else:
                    _, config = self.ctx.clients.read_config(state, client)
                    content = self.ctx.clients.render(client, config, profile, "micu")
                files[live] = content
                files[self.ctx.profiles.profile_path("micu", client)] = json_bytes(
                    profile
                )
                state["active"][client] = "micu"
            scope = (
                "完整配置（含当时的公共设置，以及存在的 Codex 登录快照）"
                if full_config
                else "通道、模型、凭据及专属设置（保留当前公共设置）"
            )
            if dry_run:
                print(
                    f"计划从固定基准恢复 {', '.join(apps)} 的{scope}；会先备份，并重置对应 micu profile。"
                )
                return
            self.ctx.storage.transaction(files, state)
        print(
            f"已从固定基准恢复 {', '.join(apps)} 的{scope}，并重置对应 micu profile。新启动生效。"
        )

    def named_baseline_dir(self, name):
        profile_name(name)
        directory = self.ctx.storage.root / "baseline/profiles" / name
        if any(
            p.is_symlink()
            for p in (self.ctx.storage.root / "baseline", directory.parent, directory)
        ):
            raise SwitchError("基准目录不能是符号链接。")
        return directory

    def baseline_resource_paths(self, app, profile):
        paths = set()
        if profile.get("options", {}).get("ca_file"):
            paths.add(profile["options"]["ca_file"])
        if app == "claude":
            if profile["env"].get("NODE_EXTRA_CA_CERTS"):
                paths.add(profile["env"]["NODE_EXTRA_CA_CERTS"])
            if profile.get("options", {}).get("read_guard"):
                paths.add(str(self.ctx.storage.root / "assets/read_guard.py"))
        else:
            for key in ("model_catalog_json", "model_instructions_file"):
                if profile["values"].get(key):
                    paths.add(profile["values"][key])
        result = []
        for value in sorted(paths):
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise SwitchError(
                    "固定基准时，模型目录、指令文件和证书须使用绝对路径。"
                )
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise SwitchError(f"基准资源不能经过符号链接：{path.name}")
            path = path.resolve()
            if any(
                path.is_relative_to(self.ctx.storage.root / part)
                for part in ("baseline", "profiles", "backups")
            ):
                raise SwitchError("基准资源不能指向基准、profile 或备份目录。")
            result.append(path)
        return result

    def named_baseline_files(self, name, profiles, configs, source):
        directory = self.named_baseline_dir(name)
        if directory.exists() and any(directory.iterdir()):
            raise SwitchError(f"{name} 基准已存在或不完整，拒绝覆盖。")
        bundle = {
            "version": 1,
            "name": name,
            "source": source,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "profiles": profiles,
            "configs": {},
            "resources": {},
        }
        for app in profiles:
            self.ctx.profiles.validate_profile(app, profiles[app])
            content = self.ctx.clients.render(app, configs[app], profiles[app], name)
            bundle["configs"][app] = base64.b64encode(content).decode()
            bundle["resources"][app] = [
                dict(path=str(path), data=base64.b64encode(path.read_bytes()).decode())
                for path in self.baseline_resource_paths(app, profiles[app])
            ]
        payload = json_bytes(bundle)
        return {
            directory / "snapshot.json": payload,
            directory / "manifest.json": json_bytes(
                {
                    "version": 1,
                    "name": name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ),
        }

    def named_baseline(self, name):
        directory = self.named_baseline_dir(name)
        snapshot, manifest = directory / "snapshot.json", directory / "manifest.json"
        if not snapshot.is_file() or not manifest.is_file():
            raise SwitchError(
                f"{name} 尚无完整固定基准；用 ai-switch baseline protect {name} 创建。"
            )
        if snapshot.is_symlink() or manifest.is_symlink():
            raise SwitchError("基准文件不能是符号链接。")
        document = json.loads(manifest.read_text(encoding="utf-8"))
        payload = snapshot.read_bytes()
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or document.get("name") != name
            or document.get("sha256") != hashlib.sha256(payload).hexdigest()
        ):
            raise SwitchError(f"{name} 固定基准校验失败，拒绝恢复。")
        bundle = json.loads(payload)
        if (
            not isinstance(bundle, dict)
            or bundle.get("version") != 1
            or bundle.get("name") != name
        ):
            raise SwitchError("固定基准格式无效。")
        for field in ("profiles", "configs", "resources"):
            if not isinstance(bundle.get(field), dict) or set(bundle[field]) != set(
                self.ctx.storage.apps()
            ):
                raise SwitchError("固定基准必须包含全部已初始化客户端的配置及资源。")
        for app in self.ctx.storage.apps():
            self.ctx.profiles.validate_profile(app, bundle["profiles"][app])
            content = base64.b64decode(bundle["configs"][app], validate=True)
            config = (
                json.loads(content)
                if app == "claude"
                else tomlkit.parse(content.decode())
            )
            if not isinstance(config, dict):
                raise SwitchError("基准中的完整配置格式无效。")
            resources = bundle["resources"][app]
            expected = {
                str(p)
                for p in self.baseline_resource_paths(app, bundle["profiles"][app])
            }
            if (
                not isinstance(resources, list)
                or any(not isinstance(r, dict) for r in resources)
                or len(resources) != len(expected)
                or {r.get("path") for r in resources} != expected
            ):
                raise SwitchError("基准资源与 profile 引用不一致。")
            for resource in resources:
                base64.b64decode(resource["data"], validate=True)
        return bundle

    def protect_baseline(self, name="micu", from_backup=None):
        if name == "micu":
            if from_backup:
                raise SwitchError("Micu 使用初始化时的原始基准，不接受其他备份来源。")
            return self.protect_original()
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            directory = self.named_baseline_dir(name)
            if (directory / "manifest.json").exists():
                self.named_baseline(name)
                print(f"{name} 基准已固定且校验通过，不会重复覆盖。")
                return
            if from_backup:
                backup = Path(from_backup).expanduser().resolve()
                if (
                    not backup.is_relative_to(self.ctx.storage.root / "backups")
                    or not backup.is_dir()
                ):
                    raise SwitchError("--from-backup 须指向本管理目录下的备份子目录。")
                items = json.loads(
                    (backup / "snapshot.json").read_text(encoding="utf-8")
                )
                profiles = {}
                for app in self.ctx.storage.apps(state):
                    matches = [
                        item
                        for item in items
                        if item["path"]
                        == str(self.ctx.profiles.profile_path(name, app))
                        and item["exists"]
                    ]
                    if len(matches) != 1:
                        raise SwitchError(
                            f"备份未包含完整的 {name}/{app} profile；拒绝混用当前配置。"
                        )
                    data = json.loads(
                        base64.b64decode(matches[0]["data"], validate=True)
                    )
                    profiles[app] = self.ctx.profiles.normalize_profile(name, app, data)
                source = "backup:" + backup.name
                # Historical profiles do not embed resources. Only freeze managed
                # tutorial assets if they still match the recorded original hashes.
                for app in self.ctx.storage.apps(state):
                    for path in self.baseline_resource_paths(app, profiles[app]):
                        if path.parent == self.ctx.storage.root / "assets":
                            expected = state.get("asset_hashes", {}).get(path.name)
                            if (
                                not expected
                                or hashlib.sha256(path.read_bytes()).hexdigest()
                                != expected
                            ):
                                raise SwitchError(
                                    f"历史配置引用的教程资源无法核实：{path.name}"
                                )
            else:
                profiles = {
                    app: self.ctx.profiles.profile(name, app)
                    for app in self.ctx.storage.apps(state)
                }
                source = "saved-profile"
            configs = {
                app: self.ctx.clients.read_config(state, app)[1]
                for app in self.ctx.storage.apps(state)
            }
            self.ctx.storage.transaction(
                self.named_baseline_files(name, profiles, configs, source), state
            )
        print(
            f"已固定 {name} 的已初始化客户端 profile、引用资源及公共设置快照；当前配置和可编辑 profile 保持不变。"
        )

    def list_baselines(self):
        self.ctx.storage.load()
        names = ["micu"]
        directory = self.ctx.storage.root / "baseline/profiles"
        if directory.is_symlink():
            raise SwitchError("基准目录不能是符号链接。")
        if directory.exists():
            names += sorted(
                p.name
                for p in directory.iterdir()
                if p.is_dir()
                and any(
                    (p / filename).exists()
                    for filename in ("snapshot.json", "manifest.json")
                )
            )
        for name in names:
            if name == "micu":
                profiles, source = self.original_profiles(), "初始化原始配置"
            else:
                bundle = self.named_baseline(name)
                profiles, source = bundle["profiles"], bundle["source"]
            print(
                f"{name} | 校验通过 | "
                + " | ".join(
                    f"{app}: {profiles[app]['values']['model']}" for app in profiles
                )
                + f" | 来源: {source}"
            )

    def restore_baseline(
        self, name="micu", app="all", full_config=False, dry_run=False
    ):
        if name == "micu":
            return self.restore_original(app, full_config, dry_run)
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            bundle = self.named_baseline(name)
            apps = self.ctx.storage.apps(state, app)
            files = {}
            reserved = {
                self.ctx.storage.state_path,
                self.ctx.storage.root / "lock",
                self.ctx.storage.root / "pending.json",
            }
            reserved.update(Path(p) for p in state["paths"].values())
            for client in apps:
                profile = bundle["profiles"][client]
                live = Path(state["paths"][client])
                if live.is_symlink():
                    raise SwitchError(f"配置是符号链接，请先明确其管理位置：{live}")
                if full_config:
                    content = base64.b64decode(bundle["configs"][client], validate=True)
                else:
                    content = self.ctx.clients.render(
                        client,
                        self.ctx.clients.read_config(state, client)[1],
                        profile,
                        name,
                    )
                files[live] = content
                files[self.ctx.profiles.profile_path(name, client)] = json_bytes(
                    profile
                )
                for resource in bundle["resources"][client]:
                    target = Path(resource["path"])
                    if target in reserved:
                        raise SwitchError("基准资源路径与管理文件冲突，拒绝恢复。")
                    value = base64.b64decode(resource["data"], validate=True)
                    if target in files and files[target] != value:
                        raise SwitchError(
                            "两客户端引用了内容不同的同名资源，拒绝恢复。"
                        )
                    if not target.is_file() or target.read_bytes() != value:
                        files[target] = value
                    if (
                        target.parent == self.ctx.storage.root / "assets"
                        and target.name in state.get("asset_hashes", {})
                    ):
                        state["asset_hashes"][target.name] = hashlib.sha256(
                            value
                        ).hexdigest()
                state["active"][client] = name
            scope = (
                "完整配置（含固定时的公共设置）"
                if full_config
                else "通道设置（保留当前公共设置）"
            )
            action = "计划" if dry_run else "已"
            if not dry_run:
                self.ctx.storage.transaction(files, state)
            print(
                f"{action}从 {name} 固定基准恢复 {', '.join(apps)} 的{scope}和引用资源，并重置对应 profile；"
                + ("会先备份。" if dry_run else "已备份，新启动生效。")
            )
