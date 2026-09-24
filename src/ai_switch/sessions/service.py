"""SessionService: extracted behavior with the version-1 state format preserved."""

from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import uuid
from ..config import SwitchError, json_bytes
from ..sessions import repair as session_repair
from ..service import Service


class SessionService(Service):
    def reusable_repair(self, session):
        """Reuse existing branches only while their recorded sources are intact.

        Changes made inside the compatible branch are deliberately allowed:
        repeatedly launching the original UUID should keep that continuation.
        """
        manifests = []
        for path in (self.ctx.storage.root / "repairs").glob("*.json"):
            if path.is_symlink():
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if path.stem != value.get("id"):
                continue
            manifests.append(value)
        visited = set()
        while session not in visited:
            visited.add(session)
            # Native revert rotates the rollout path without changing its UUID
            # or the old file. A byte-identical old source is then a stale branch.
            database = (
                Path(self.ctx.storage.load()["paths"]["codex"]).parent
                / "state_5.sqlite"
            )
            current_path = None
            if database.is_file():
                conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                try:
                    row = conn.execute(
                        "SELECT rollout_path FROM threads WHERE id=?", (session,)
                    ).fetchone()
                    if row:
                        current_path = Path(row[0]).resolve()
                finally:
                    conn.close()
            candidates = sorted(
                (m for m in manifests if m.get("source_id") == session),
                key=lambda m: m.get("created_at_ms", 0),
                reverse=True,
            )
            for manifest in candidates:
                if (
                    manifest["id"] in visited
                    or not Path(manifest["rollout_path"]).is_file()
                ):
                    continue
                if not manifest.get("source_nodes"):
                    continue
                if (
                    current_path
                    and current_path
                    != Path(manifest["source_nodes"][0]["path"]).resolve()
                ):
                    continue
                try:
                    session_repair.verify_sources(manifest)
                except (OSError, session_repair.RepairError):
                    continue
                session = str(uuid.UUID(manifest["id"]))
                break
            else:
                return session
        raise SwitchError("修复副本的来源关系循环，未启动客户端。")

    def repair_session(self, session, app="codex", dry_run=False, automatic=False):
        if app != "codex":
            raise SwitchError("旧 item_ ID 修复目前仅适用于 Codex。")
        try:
            session = str(uuid.UUID(session))
        except ValueError:
            raise SwitchError("请提供完整的 Codex 会话 UUID。") from None
        with self.ctx.storage.lock():
            state = self.ctx.storage.load()
            self.ctx.storage.apps(state, "codex")
            if automatic:
                requested = session
                session = self.reusable_repair(session)
                if session != requested:
                    print(
                        f"自动复用兼容副本：{requested} → {session}；副本中的后续对话保留。"
                    )
            manifest_path = self.ctx.storage.root / "repairs" / (session + ".json")
            upgrade = manifest_path.exists()
            try:
                home = Path(state["paths"]["codex"]).parent
                history_mode = session_repair.native_history_mode()
                if upgrade:
                    if manifest_path.is_symlink():
                        raise SwitchError("副本来源记录经过符号链接，未修改会话。")
                    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if saved.get("id") != session or saved.get("version") not in (
                        1,
                        2,
                        3,
                    ):
                        raise SwitchError("修复副本的来源记录无效，未修改会话。")
                    if (
                        automatic
                        or history_mode == "paginated"
                        or saved.get("history_mode") == "paginated"
                    ):
                        plan = session_repair.prepare(
                            home,
                            session,
                            repair_dir=self.ctx.storage.root / "repairs",
                            source_path=saved["rollout_path"],
                            history_mode=history_mode,
                        )
                        if plan["changed"] or plan.get("format_migrated"):
                            upgrade = False
                        elif plan.get("history_mode") != "paginated":
                            plan = session_repair.prepare_display_upgrade(
                                home, session, saved
                            )
                    else:
                        plan = session_repair.prepare_display_upgrade(
                            home, session, saved
                        )
                else:
                    plan = session_repair.prepare(
                        home,
                        session,
                        repair_dir=self.ctx.storage.root / "repairs",
                        history_mode=history_mode,
                    )
                session_repair.verify_sources(plan)
            except session_repair.RepairError as exc:
                raise SwitchError(str(exc)) from None
            if not plan["changed"] and not plan.get("format_migrated"):
                if automatic:
                    print("启动前检查通过，无需修复。")
                    return session
                print(
                    "副本的历史格式与消息显示记录已完整；未修改会话。"
                    if upgrade
                    else "没有发现需要处理的历史格式或已知记录 ID 问题；未创建副本。"
                )
                return None
            manifest = plan["manifest"]
            if upgrade:
                print(
                    f"为现有副本补充 {plan['changed']} 条消息显示记录；UUID、原历史和后续追加记录保持不变。"
                )
            else:
                if plan["changed"]:
                    print(
                        f"发现 {plan['changed']} 个不兼容记录 ID（其中 {manifest.get('regenerated_ids', 0)} 个由原生分支重新生成），完整历史 {manifest['records']} 条记录；正文、工具参数和结果保持不变。"
                    )
                else:
                    print(
                        f"需要升级旧版历史格式，完整历史 {manifest['records']} 条记录；模型上下文保持不变。"
                    )
                if manifest.get("history_mode") == "paginated":
                    print(
                        "使用 Codex 0.156+ 分页历史；以新 UUID 登记，支持原生回退编辑，原会话保留。"
                    )
            if dry_run:
                print("仅预览，未写入任何会话；不会切换配置或发送模型请求。")
                if automatic:
                    if not upgrade:
                        print("以下副本 UUID 仅为预览，实际启动时才会创建兼容副本。")
                    return manifest["id"]
                return None
            destination = plan["path"]
            if (destination.exists() and not upgrade) or any(
                p.is_symlink() for p in (destination, *destination.parents)
            ):
                raise SwitchError("修复副本的目标路径已存在或经过符号链接。")
            self.ctx.storage.transaction(
                {
                    destination: plan["content"],
                    self.ctx.storage.root
                    / "repairs"
                    / (manifest["id"] + ".json"): json_bytes(manifest),
                },
                state,
                before_commit=lambda: session_repair.verify_sources(plan),
            )
        print(
            f"已补全现有副本的消息显示：{manifest['id']}"
            if upgrade
            else f"已创建修复副本：{manifest['id']}\n原会话保留：{manifest['source_id']}"
        )
        print(f"续接：ai-switch run codex --mode aster --session {manifest['id']}")
        print(
            "以后通过 ai-switch run 续接 Aster 会话会自动检查；原生客户端内新建分支需下次启动时再检查。"
        )
        return manifest["id"]

    def pending_repaired_sessions(self, known_ids, include_subagents=False):
        result = []
        for path in (self.ctx.storage.root / "repairs").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                data["id"] not in known_ids
                and Path(data["rollout_path"]).is_file()
                and (include_subagents or not data.get("subagent"))
            ):
                result.append(
                    dict(
                        id=data["id"],
                        cwd=data["cwd"],
                        provider=data["provider"],
                        name=data["name"],
                        subagent=data.get("subagent", False),
                        _sort_time=data["created_at_ms"],
                    )
                )
        return result

    def sessions(self, app, limit=20, include_subagents=False):
        if limit < 1:
            raise SwitchError("--limit 必须为正整数。")
        state = self.ctx.storage.load()
        self.ctx.storage.apps(state, app)
        if app == "codex":
            db_path = Path(state["paths"][app]).parent / "state_5.sqlite"
            if not db_path.exists():
                return []
            conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
            try:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
                title = (
                    "COALESCE(NULLIF(name,''),title)" if "name" in columns else "title"
                )
                order = "updated_at_ms" if "updated_at_ms" in columns else "updated_at"
                extra = ",".join(
                    key if key in columns else "NULL"
                    for key in ("source", "thread_source", "agent_path")
                )
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                children = (
                    {
                        r[0]
                        for r in conn.execute(
                            "SELECT child_thread_id FROM thread_spawn_edges"
                        )
                    }
                    if "thread_spawn_edges" in tables
                    else set()
                )
                rows = conn.execute(
                    f"SELECT id,cwd,model_provider,{title},{extra} FROM threads WHERE archived=0 ORDER BY {order} DESC"
                )
                result = []
                for row in rows:
                    subagent = (
                        session_repair.is_subagent(row[4], row[5], row[6])
                        or row[0] in children
                    )
                    if subagent and not include_subagents:
                        continue
                    result.append(
                        dict(
                            id=row[0],
                            cwd=row[1],
                            provider=row[2],
                            name=row[3] or "",
                            subagent=bool(subagent),
                        )
                    )
                    if len(result) == limit:
                        break
                pending = self.pending_repaired_sessions(
                    {r[0] for r in conn.execute("SELECT id FROM threads")},
                    include_subagents,
                )
                if pending:
                    times = dict(conn.execute(f"SELECT id,{order} FROM threads"))
                    for item in result:
                        item["_sort_time"] = (times[item["id"]] or 0) * (
                            1 if order.endswith("_ms") else 1000
                        )
                    result = sorted(
                        result + pending,
                        key=lambda item: item["_sort_time"],
                        reverse=True,
                    )[:limit]
                    for item in result:
                        item.pop("_sort_time", None)
                return result
            finally:
                conn.close()
        projects = Path(state["paths"][app]).parent / "projects"
        files = sorted(
            projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        result = []
        for path in files:
            try:
                uuid.UUID(path.stem)
                cwd = ""
                subagent = False
                with path.open(encoding="utf-8") as stream:
                    for _, line in zip(range(30), stream):
                        item = json.loads(line)
                        subagent = subagent or bool(item.get("isSidechain"))
                        if item.get("cwd"):
                            cwd = item["cwd"]
                if subagent and not include_subagents:
                    continue
                result.append(
                    dict(
                        id=path.stem,
                        cwd=cwd,
                        provider="Claude 会话",
                        name="",
                        subagent=subagent,
                    )
                )
                if len(result) == limit:
                    break
            except (ValueError, OSError):
                continue
        return result
