"""Create self-contained Codex rollout copies without legacy response item IDs.

No original rollout or Codex database is written by this module. Registration
and subsequent conversation writes remain the native client's responsibility.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import re
import subprocess
import shutil
import platform_runtime
import uuid


class RepairError(Exception):
    pass


PREFIXES = {'message': 'msg_', 'reasoning': 'rs_', 'function_call': 'fc_',
            'function_call_output': 'fco_', 'custom_tool_call': 'ctc_',
            'custom_tool_call_output': 'ctco_'}


def native_history_mode():
    """Use the format required by 0.156's native prompt revert operation."""
    try:
        result = subprocess.run(platform_runtime.native_command([shutil.which('codex') or 'codex', '--version']), capture_output=True, text=True,
                                timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return 'legacy'
    match = re.search(r'\bcodex(?:-cli)?\s+(\d+)\.(\d+)\.(\d+)\b', result.stdout)
    return ('paginated' if match and tuple(map(int, match.groups())) >= (0, 156, 0)
            else 'legacy')


def legacy_message(item):
    """Project modern messages for the 0.155.1 legacy TUI, never API input."""
    kind = item.get('type')
    if kind not in ('UserMessage', 'AgentMessage'):
        return None
    parts = item.get('content', [])
    texts, elements = [], []
    offset = 0
    for part in parts:
        if part.get('type') not in ('text', 'Text'):
            continue
        text = part['text']
        for element in part.get('text_elements', []):
            element = copy.deepcopy(element)
            span = element.get('byte_range', {})
            for key in ('start', 'end'):
                if key in span:
                    span[key] += offset
            elements.append(element)
        texts.append(text)
        offset += len(text.encode()) + 1  # Newline between text blocks.
    message = '\n'.join(texts)
    if kind == 'AgentMessage':
        return dict(type='agent_message', message=message, phase=item.get('phase'))
    return dict(type='user_message', message=message, text_elements=elements,
                images=[p['url'] for p in parts if p.get('type') == 'image'],
                local_images=[p['path'] for p in parts if p.get('type') in ('local_image', 'localImage')],
                audio=[p['url'] for p in parts if p.get('type') == 'audio'],
                local_audio=[p['path'] for p in parts if p.get('type') in ('local_audio', 'localAudio')])


def add_display_events(records):
    """Retain modern events and add missing legacy message display projections.

    Retain the pre-0.156 compatibility path. Supplemental legacy events render
    messages without changing response_item/compacted records used for model
    context. Newer clients use a fresh paginated UUID instead of relabeling an
    existing legacy UUID whose native database still caches the old format.
    """
    existing = Counter()
    turn = None
    for record in records:
        payload = record.get('payload', {})
        if record.get('type') != 'event_msg':
            continue
        if payload.get('type') == 'task_started':
            turn = payload.get('turn_id')
        if payload.get('type') in ('user_message', 'agent_message'):
            event_turn = record.get('ai_switch_display', {}).get('turn_id', turn)
            existing[(event_turn, payload['type'], payload.get('message', ''))] += 1
    result, added, turn = [], Counter(), None
    for record in records:
        result.append(record)
        payload = record.get('payload', {})
        if record.get('type') != 'event_msg':
            continue
        if payload.get('type') == 'task_started':
            turn = payload.get('turn_id')
        if payload.get('type') != 'item_completed':
            continue
        message = legacy_message(payload.get('item', {}))
        if message is None:
            continue
        key = (payload.get('turn_id', turn), message['type'], message['message'])
        if existing[key]:
            existing[key] -= 1
            continue
        result.append(dict(type='event_msg', timestamp=record.get('timestamp'), payload=message,
                           ai_switch_display=dict(version=1, turn_id=key[0], item_id=payload.get('item', {}).get('id'))))
        added[message['type']] += 1
    return result, dict(added)


def add_paginated_messages(records, thread_id):
    """Project legacy display messages into native paginated transcript items.

    Keep all source records and API input untouched. Match modern/legacy display
    pairs per turn, including media and text spans, so mixed histories do not
    duplicate messages. These IDs belong only to local UI events.
    """
    def key(turn, message):
        fields = ('type', 'message', 'phase') if message['type'] == 'agent_message' else (
            'type', 'message', 'text_elements', 'images', 'local_images', 'audio', 'local_audio')
        value = {k: (message.get(k) or []) if k not in ('type', 'message', 'phase')
                 else message.get(k) for k in fields}
        return turn, json.dumps(value, sort_keys=True, ensure_ascii=False)

    existing, turn = Counter(), None
    for record in records:
        payload = record.get('payload', {})
        if record.get('type') != 'event_msg':
            continue
        if payload.get('type') == 'task_started':
            turn = payload.get('turn_id')
        if payload.get('type') == 'item_completed':
            message = legacy_message(payload.get('item', {}))
            if message is not None:
                existing[key(payload.get('turn_id', turn), message)] += 1

    result, added, turn = [], Counter(), None
    for record in records:
        result.append(record)
        payload = record.get('payload', {})
        if record.get('type') != 'event_msg':
            continue
        kind = payload.get('type')
        if kind == 'task_started':
            turn = payload.get('turn_id')
        if kind not in ('user_message', 'agent_message'):
            continue
        event_turn = record.get('ai_switch_display', {}).get('turn_id', turn)
        signature = key(event_turn, payload)
        if existing[signature]:
            existing[signature] -= 1
            continue
        if not event_turn:
            raise RepairError('旧消息缺少所属轮次，无法安全转换分页历史；未修改源会话。')
        user = kind == 'user_message'
        item = dict(type='UserMessage' if user else 'AgentMessage', id=str(uuid.uuid4()),
                    content=[dict(type='text' if user else 'Text', text=payload['message'])])
        if user:
            item['content'][0]['text_elements'] = copy.deepcopy(payload.get('text_elements', []))
            for field, media_type, value_key in (('images', 'image', 'url'),
                                                ('local_images', 'local_image', 'path'),
                                                ('audio', 'audio', 'url'),
                                                ('local_audio', 'local_audio', 'path')):
                for value in payload.get(field) or []:
                    if not isinstance(value, str):
                        raise RepairError('旧消息的媒体格式无法识别，未修改源会话。')
                    item['content'].append(dict(type=media_type, **{value_key: value}))
        else:
            item['phase'] = payload.get('phase')
            if 'memory_citation' in payload:
                item['memory_citation'] = copy.deepcopy(payload['memory_citation'])
        result.append(dict(type='event_msg', timestamp=record.get('timestamp'),
                           payload=dict(type='item_completed', thread_id=thread_id,
                                        turn_id=event_turn, item=item),
                           ai_switch_paginated=dict(version=1)))
        added[kind] += 1
    return result, dict(added)


def serialize(records):
    return b''.join((json.dumps(record, ensure_ascii=False, separators=(',', ':'))+'\n').encode()
                    for record in records)


def prepare_display_upgrade(codex_home, session_id, manifest):
    """Upgrade only a tool-owned copy, keeping UUID and all native appends."""
    home = Path(codex_home).resolve()
    if (manifest.get('id') != session_id or manifest.get('version') not in (1, 2)
            or not manifest.get('source_id') or manifest['source_id'] == session_id):
        raise RepairError('修复副本的来源记录无效，未修改会话。')
    path = Path(manifest['rollout_path'])
    if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
            or not any(path.resolve().is_relative_to(home/name) for name in ('sessions', 'archived_sessions'))):
        raise RepairError('修复副本路径无效或经过符号链接。')
    stat = path.stat()
    raw = path.read_bytes()
    if not raw.endswith(b'\n'):
        raise RepairError('会话仍在写入，请退出该会话后重试。')
    records = [json.loads(line) for line in raw.splitlines()]
    meta = records[0].get('payload', {}) if records else {}
    if (not records or records[0].get('type') != 'session_meta' or meta.get('id') != session_id
            or meta.get('history_base') or meta.get('history_mode') != 'legacy'):
        raise RepairError('副本格式已改变，不能自动补充旧版界面记录。')
    records, added = add_display_events(records)
    source = dict(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                  prefix_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    updated = copy.deepcopy(manifest)
    content = serialize(records)
    updated.update(version=2, display_events_version=1, records=len(records),
                   rollout_sha256=hashlib.sha256(content).hexdigest())
    updated['display_events_added'] = dict(Counter(updated.get('display_events_added', {})) + Counter(added))
    return dict(source_id=session_id, changed=sum(added.values()), upgrade=True, path=path,
                content=content, manifest=updated, source_nodes=[source], display_added=added)


def is_subagent(source, thread_source=None, agent_path=None):
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            pass
    return bool(isinstance(source, dict) and 'subagent' in source
                or source == 'subagent' or thread_source in ('subagent', 'guardian_review')
                or agent_path and agent_path.startswith('/root/'))


def response_items(record):
    payload = record.get('payload', {})
    if record.get('type') == 'response_item':
        yield payload
    elif record.get('type') == 'compacted':
        for key in ('replacement_history', 'guardian_history'):
            history = payload.get(key) or []
            if not isinstance(history, list):
                raise RepairError('压缩历史格式无法识别，未创建修复副本。')
            yield from history


def response_fingerprint(item):
    """Compare complete response bodies; call_id and encrypted data are included."""
    body = {key:value for key,value in item.items() if key != 'id'}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def checked_rollout_path(home, path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RepairError('会话路径无效或经过符号链接。')
    path = path.resolve()
    if not any(path.is_relative_to(home/name) for name in ('sessions', 'archived_sessions')):
        raise RepairError('会话文件不在当前 Codex 的历史目录中。')
    return path


def rollout_segment_id(path):
    match = re.search(r'[-_]([0-9a-fA-F-]{36})\.jsonl$', Path(path).name)
    try:
        return str(uuid.UUID(match.group(1))) if match else None
    except ValueError:
        return None


def resolve_base_path(home, conn, base, child_path):
    """Resolve a logical history cut to a physical rollout segment.

    Native 0.156 revert keeps the logical thread UUID and rotates the rollout.
    history_base.thread_id identifies the physical segment: the initial UUID or
    the extra UUID suffix on a later filename. Its header can still name the
    original logical thread. The database only names that thread's latest file.
    Never select a different segment merely because it has the same logical ID.
    """
    if not isinstance(base, dict) or not base.get('thread_id'):
        raise RepairError('无法识别分页祖先引用，未创建修复副本。')
    ident = str(uuid.UUID(base['thread_id']))
    limit = base.get('end_ordinal_exclusive')
    if type(limit) is not int or limit < 1:
        raise RepairError('祖先历史记录序号无效。')
    paths = set()
    row = conn.execute('SELECT rollout_path FROM threads WHERE id=?', (ident,)).fetchone()
    if row and rollout_segment_id(row[0]) == ident:
        paths.add(Path(row[0]))
    for directory in ('sessions', 'archived_sessions'):
        paths.update((home/directory).rglob(f'rollout-*{ident}.jsonl'))
    candidates = []
    for path in sorted(paths):
        path = checked_rollout_path(home, path)
        if path == child_path or not path.is_file():
            continue
        with path.open('rb') as stream:
            lines = stream.readlines()
        if not lines:
            continue
        header = json.loads(lines[0])
        meta = header.get('payload', {})
        if header.get('type') != 'session_meta' or rollout_segment_id(path) != ident:
            continue
        uuid.UUID(meta['id'])
        start = (meta.get('history_base') or {}).get('end_ordinal_exclusive', 0)
        if type(start) is not int or start < 0:
            raise RepairError('祖先历史记录序号无效。')
        count = limit-start
        if not 1 <= count <= len(lines):
            continue
        prefix = b''.join(lines[:count])
        if not prefix.endswith(b'\n'):
            continue
        candidates.append((path, len(prefix), hashlib.sha256(prefix).hexdigest()))
    exact = [c for c in candidates if c[1] == base.get('end_byte_offset')]
    if exact:
        candidates = exact
    if not candidates:
        raise RepairError(f'找不到覆盖指定历史边界的祖先分段：{ident}；未修改会话。')
    if len({c[2] for c in candidates}) != 1:
        raise RepairError(f'祖先分段存在多个不同候选，无法安全确定历史：{ident}；未修改会话。')
    return candidates[0][0]


def repaired_ancestor_fingerprints(home, session_id, repair_dir):
    """Find recorded repairs along native fork lineage, without replaying it.

    A native legacy fork copies all history, so history_base is absent even
    though forked_from_id still points to the parent. Codex assigns fresh IDs to
    missing response IDs when copying. Prefix checks alone cannot detect those.
    """
    if repair_dir is None:
        return set(), [], []
    repair_dir = Path(repair_dir)
    conn = sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro', uri=True)
    seen, pending, fingerprints, sources, references = set(), [(session_id, None)], set(), [], []

    def read_checked(path, full=False):
        path = Path(path)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise RepairError('修复来源路径经过符号链接。')
        stat = path.stat()
        with path.open('rb') as stream:
            raw = stream.read() if full else stream.readline()
        if not raw.endswith(b'\n'):
            raise RepairError('修复来源仍在写入或已截断。')
        sources.append(dict(path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns,
                            prefix_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
        return raw

    def rollout_path(path):
        path = Path(path)
        if not path.is_absolute() or not any(path.resolve().is_relative_to(home/name)
                                            for name in ('sessions','archived_sessions')):
            raise RepairError('修复祖先不在当前 Codex 历史目录内。')
        return path

    try:
        conn.execute('BEGIN')
        while pending:
            ident, explicit_path = pending.pop()
            ident = str(uuid.UUID(ident))
            key = (ident, str(explicit_path) if explicit_path else None)
            if key in seen:
                continue
            if len(seen) >= 256:
                raise RepairError('修复来源链过深。')
            seen.add(key)
            manifest_path = repair_dir/(ident+'.json')
            if manifest_path.exists():
                manifest = json.loads(read_checked(manifest_path, True))
                if manifest.get('id') != ident or manifest.get('version') not in (1, 2, 3):
                    raise RepairError('修复来源记录无效。')
                recorded = manifest.get('removed_response_fingerprints')
                if recorded is not None:
                    if (not isinstance(recorded, list) or
                            any(not isinstance(x,str) or len(x)!=64 or
                                any(c not in '0123456789abcdef' for c in x) for x in recorded)):
                        raise RepairError('修复来源的响应指纹无效。')
                    fingerprints.update(recorded)
                else:
                    raw = read_checked(rollout_path(manifest['rollout_path']), True)
                    records = [json.loads(x) for x in raw.splitlines()]
                    if records[0].get('payload',{}).get('id') != ident:
                        raise RepairError('修复来源的会话 UUID 不一致。')
                    for record in records:
                        for item in response_items(record):
                            if item.get('type') in PREFIXES and not item.get('id'):
                                fingerprints.add(response_fingerprint(item))
                references.append(ident)
                continue
            row = conn.execute('SELECT rollout_path FROM threads WHERE id=?',(ident,)).fetchone()
            if row is None and explicit_path is None:
                continue  # Deleted unrelated ancestry need not block legacy ID repair.
            path = rollout_path(explicit_path or row[0])
            header = json.loads(read_checked(path))
            meta = header.get('payload',{})
            if (header.get('type')!='session_meta' or
                    (meta.get('id')!=ident and (explicit_path is None or rollout_segment_id(path)!=ident))):
                raise RepairError('修复祖先的会话 UUID 不一致。')
            if meta.get('id') != ident and (repair_dir/(meta['id']+'.json')).is_file():
                pending.append((meta['id'], path))
                continue
            if meta.get('forked_from_id'):
                pending.append((meta['forked_from_id'], None))
            if meta.get('history_base'):
                base = meta['history_base']
                pending.append((base['thread_id'], resolve_base_path(home, conn, base, path)))
    finally:
        conn.close()
    return fingerprints, sources, references


def prepare(codex_home, session_id, repair_dir=None, source_path=None, history_mode='legacy'):
    if history_mode not in ('legacy', 'paginated'):
        raise RepairError('不支持的历史格式。')
    home = Path(codex_home).resolve()
    try:
        session_id = str(uuid.UUID(session_id))
    except ValueError:
        raise RepairError('请提供完整的 Codex 会话 UUID。') from None
    database = home/'state_5.sqlite'
    if not database.is_file():
        raise RepairError('找不到 Codex 会话数据库。')
    conn = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    nodes, visited, visited_ids = [], set(), set()
    source_row = None
    try:
        conn.execute('BEGIN')
        ident, limit, explicit_path = session_id, None, None
        while True:
            row = conn.execute('SELECT * FROM threads WHERE id=?', (ident,)).fetchone()
            if row is None and ident == session_id and source_path is not None:
                # A newly created tool-owned copy may not yet be registered by
                # native Codex. Its manifest supplies the path; validation below
                # still checks directory containment, symlinks and UUID.
                row = dict(rollout_path=str(source_path), cwd='', model_provider='')
            if row is None and explicit_path is not None:
                row = dict(rollout_path=str(explicit_path), cwd='', model_provider='')
            if row is None:
                raise RepairError(f'找不到会话或祖先：{ident}')
            if source_row is None:
                source_row = dict(row)
            path = checked_rollout_path(home, explicit_path or row['rollout_path'])
            if path in visited or len(visited) >= 256:
                raise RepairError('会话历史分段循环或过深，未创建修复副本。')
            visited.add(path)
            visited_ids.add(ident)
            stat = path.stat()
            with path.open('rb') as stream:
                lines = stream.readlines()
            if not lines:
                raise RepairError('会话文件为空。')
            header = json.loads(lines[0])
            start_ordinal = (header.get('payload', {}).get('history_base') or {}).get('end_ordinal_exclusive', 0)
            if limit is not None:
                if type(limit) is not int or type(start_ordinal) is not int:
                    raise RepairError('祖先历史记录序号无效。')
                count = limit - start_ordinal
                if count < 1 or count > len(lines):
                    raise RepairError('祖先历史记录边界无效，未创建修复副本。')
                lines = lines[:count]
            raw = b''.join(lines)
            if not raw.endswith(b'\n'):
                raise RepairError('会话仍在写入或历史截断位置不是完整记录边界，请退出该会话后重试。')
            records = [json.loads(line) for line in raw.splitlines()]
            if not records or records[0].get('type') != 'session_meta':
                raise RepairError('无法识别会话元数据。')
            meta = records[0]['payload']
            if meta.get('id') != ident and (explicit_path is None or rollout_segment_id(path) != ident):
                raise RepairError('会话数据库与文件 UUID 不一致。')
            visited_ids.add(str(uuid.UUID(meta['id'])))
            nodes.append(dict(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                              prefix_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), records=records))
            base = meta.get('history_base')
            if not base:
                break
            if not isinstance(base, dict) or not base.get('thread_id') or 'end_ordinal_exclusive' not in base:
                raise RepairError('无法识别分页祖先引用，未创建修复副本。')
            # Byte offsets are indexing hints and can be stale after native
            # migrations. Logical rollout ordinals define the actual fork cut.
            ident, limit = str(uuid.UUID(base['thread_id'])), base['end_ordinal_exclusive']
            explicit_path = resolve_base_path(home, conn, base, path)
    finally:
        conn.close()
    fingerprints, reference_nodes, reference_ids = repaired_ancestor_fingerprints(home, session_id, repair_dir)
    # Resolve ancestor boundaries before dropping their metadata. This preserves
    # the chosen fork's history, not later turns appended to any ancestor.
    metadata = nodes[0]['records'][0]
    records = [metadata]
    for node in reversed(nodes):
        records.extend(record for record in node['records'][1:] if record.get('type') != 'session_meta')
    mapping, kinds, removed_fingerprints, regenerated = {}, {}, set(), set()
    for record in records[1:]:
        for item in response_items(record):
            if not isinstance(item, dict):
                raise RepairError('历史响应项格式无效。')
            old, kind = item.get('id'), item.get('type')
            signature = response_fingerprint(item)
            if isinstance(old, str) and kind in PREFIXES and (old.startswith('item_') or signature in fingerprints):
                if old in mapping and mapping[old] != kind:
                    raise RepairError('同一旧 ID 对应不同响应类型，未创建修复副本。')
                mapping[old], kinds[old] = kind, kind
                removed_fingerprints.add(signature)
                if not old.startswith('item_'):
                    regenerated.add(old)
    source_nodes = [{k:v for k,v in n.items() if k != 'records'} for n in nodes] + reference_nodes
    migrate = history_mode == 'paginated' and metadata['payload'].get('history_mode') != 'paginated'
    if not mapping and not migrate:
        return dict(source_id=session_id, changed=0, source_nodes=source_nodes,
                    history_mode=metadata['payload'].get('history_mode', 'legacy'))
    for record in records[1:]:
        for item in response_items(record):
            if item.get('id') in mapping:
                item.pop('id')
        # Transcript UI events are local records and retain their original IDs.
        # Do not invent replacement API IDs: removing only the optional response
        # ID is the behavior verified with Aster. Never alter call_id, user text,
        # tool arguments/results, or encrypted reasoning payloads.
    created = dt.datetime.now(dt.timezone.utc)
    new_id = str(uuid.uuid4())
    meta = metadata['payload']
    meta['id'] = new_id
    if 'session_id' in meta:
        meta['session_id'] = new_id
    meta['timestamp'] = created.isoformat().replace('+00:00', 'Z')
    meta['history_mode'] = history_mode
    for key in ('history_base', 'forked_from_id', 'forked_from_ordinal_exclusive'):
        meta.pop(key, None)
    metadata['timestamp'] = meta['timestamp']
    for record in records[1:]:
        payload = record.get('payload', {})
        if record.get('type') == 'event_msg' and payload.get('thread_id') in visited_ids:
            payload['thread_id'] = new_id
    display_added, paginated_added = {}, {}
    if history_mode == 'paginated':
        records, paginated_added = add_paginated_messages(records, new_id)
        for ordinal, record in enumerate(records):
            record['ordinal'] = ordinal
    else:
        records, display_added = add_display_events(records)
    raw = serialize(records)
    filename = 'rollout-' + created.strftime('%Y-%m-%dT%H-%M-%S') + '-' + new_id + '.jsonl'
    path = home/'sessions'/created.strftime('%Y/%m/%d')/filename
    counts = {kind: list(kinds.values()).count(kind) for kind in sorted(set(kinds.values()))}
    manifest = dict(version=3 if history_mode == 'paginated' else 2,
                    history_mode=history_mode, format_migrated=migrate,
                    paginated_events_added=paginated_added,
                    display_events_version=1, display_events_added=display_added,
                    regenerated_ids=len(regenerated), repair_reference_ids=reference_ids,
                    removed_response_fingerprints=sorted(removed_fingerprints | fingerprints),
                    source_id=session_id, id=new_id, cwd=meta.get('cwd', source_row['cwd']),
                    provider=meta.get('model_provider', source_row['model_provider']),
                    name=('[兼容副本] ' if migrate else '[ID 修复] ') + (source_row.get('name') or source_row.get('title') or session_id),
                    created_at_ms=int(created.timestamp()*1000), rollout_path=str(path),
                    rollout_sha256=hashlib.sha256(raw).hexdigest(), changed=len(mapping), changed_types=counts,
                    records=len(records), source_nodes=source_nodes)
    manifest['subagent'] = is_subagent(source_row.get('source'), source_row.get('thread_source'), source_row.get('agent_path'))
    return dict(source_id=session_id, changed=len(mapping), format_migrated=migrate,
                path=path, content=raw, manifest=manifest)


def verify_sources(plan):
    for node in plan.get('source_nodes', plan.get('manifest', plan)['source_nodes']):
        path = Path(node['path'])
        stat = path.stat()
        if stat.st_size != node['size'] or stat.st_mtime_ns != node['mtime_ns']:
            raise RepairError('源会话在修复期间发生变化，请退出该会话后重试。')
        with path.open('rb') as stream:
            digest = hashlib.sha256(stream.read(node['prefix_bytes'])).hexdigest()
        if digest != node['sha256']:
            raise RepairError('源会话在修复期间发生变化，未创建修复副本。')
