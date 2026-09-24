"""StateStore: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import base64
import contextlib
import datetime as dt
import json
from pathlib import Path
import uuid
from .config import APPS, SwitchError, json_bytes
from .platforms import io as platform_io


class StateStore:
    def __init__(self, root=None):
        self.root = (
            Path(root or Path.home() / ".config/ai-switch").expanduser().resolve()
        )
        self.state_path = self.root / "state.json"

    def load(self):
        if not self.state_path.exists():
            raise SwitchError("尚未初始化；请先运行 ai-switch init。")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("version") != 1:
            raise SwitchError("不支持的 ai-switch 状态版本。")
        pending = self.root / "pending.json"
        if pending.exists():
            raise SwitchError("发现未完成的切换。请先运行 ai-switch recover。")
        return state

    @contextlib.contextmanager
    def lock(self):
        try:
            with platform_io.configuration_lock(self.root / "lock") as handle:
                yield handle
        except BlockingIOError:
            raise SwitchError("另一个 ai-switch 正在切换配置。") from None

    def apps(self, state=None, requested="all"):
        state = self.load() if state is None else state
        enabled = tuple(app for app in APPS if app in state["active"])
        if (
            not enabled
            or set(state["active"]) != set(enabled)
            or set(state["paths"]) != set(enabled)
        ):
            raise SwitchError("已初始化客户端记录无效。")
        if requested == "all":
            return enabled
        if requested not in enabled:
            raise SwitchError(
                f"{requested} 未在此管理目录初始化；请使用已初始化客户端，或为它指定独立 --state-dir。"
            )
        return (requested,)

    def transaction(self, files, state, before_commit=None):
        backup = (
            self.root
            / "backups"
            / (
                dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8]
            )
        )
        snapshots = []
        for path in [*files, self.state_path]:
            snapshots.append(
                dict(
                    path=str(path),
                    exists=path.exists(),
                    mode=(path.stat().st_mode & 0o777) if path.exists() else 0o600,
                    data=base64.b64encode(path.read_bytes()).decode()
                    if path.exists()
                    else "",
                )
            )
        platform_io.atomic_write(backup / "snapshot.json", json_bytes(snapshots))
        if before_commit is not None:
            before_commit()
        platform_io.atomic_write(
            self.root / "pending.json", json_bytes({"backup": str(backup)})
        )
        try:
            for path, content in files.items():
                if content is None:
                    path.unlink(missing_ok=True)
                    platform_io.sync_directory(path.parent)
                else:
                    platform_io.atomic_write(path, content)
            state["last_backup"] = str(backup)
            platform_io.atomic_write(self.state_path, json_bytes(state))
            (self.root / "pending.json").unlink()
        except BaseException:
            self.restore_snapshot(snapshots)
            (self.root / "pending.json").unlink(missing_ok=True)
            raise

    def restore_snapshot(self, snapshots):
        for item in snapshots:
            path = Path(item["path"])
            if item["exists"]:
                platform_io.atomic_write(
                    path, base64.b64decode(item["data"]), item["mode"]
                )
            else:
                path.unlink(missing_ok=True)

    def recover(self):
        with self.lock():
            journal = self.root / "pending.json"
            if not journal.exists():
                print("没有待恢复的切换。")
                return
            backup = Path(json.loads(journal.read_text(encoding="utf-8"))["backup"])
            self.restore_snapshot(
                json.loads((backup / "snapshot.json").read_text(encoding="utf-8"))
            )
            journal.unlink()
        print("已恢复到未完成切换之前。")
