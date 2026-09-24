#!/usr/bin/env python3
"""Switch complete Claude/Codex provider settings without rewriting conversations."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import datetime as dt
try:
    import fcntl
except ImportError:  # Allow help and public templates on native Windows.
    fcntl = None
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
import session_repair
import session_process
import template_profiles
import platform_io
import platform_runtime

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
import tomlkit

VERSION = "1.9.1"
MODES = ("micu", "aster")  # Bootstrap templates; runtime profiles are discovered on disk.
APPS = ("claude", "codex")
CLAUDE_FIELDS = ("model", "effortLevel", "modelSettings", "ultracode", "enableArtifact",
                 "disableArtifact", "enableWorkflows", "disableWorkflows",
                 "workflowKeywordTriggerEnabled")
CODEX_FIELDS = ("model", "model_provider", "model_reasoning_effort", "model_catalog_json",
                "model_instructions_file", "model_context_window", "model_auto_compact_token_limit",
                "model_auto_compact_token_limit_scope", "service_tier", "plan_mode_reasoning_effort")
AGENT_FIELDS = ("default_subagent_model", "default_subagent_reasoning_effort")
CODEX_EFFORT_FIELDS = ('model_reasoning_effort', 'plan_mode_reasoning_effort')
CLAUDE_EXTRA_ENV = ("NODE_EXTRA_CA_CERTS", "AI_SWITCH_MODE", "FORCE_READ_GUARD", "READ_GUARD_MODELS",
                    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY")


class SwitchError(Exception):
    pass


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


private_dir = platform_io.private_dir
atomic_write = platform_io.atomic_write


def claude_env_key(key):
    return (key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_CODE_SUBAGENT_")
            or key in CLAUDE_EXTRA_ENV)


def plain(value):
    return value.unwrap() if hasattr(value, "unwrap") else copy.deepcopy(value)


def selected(source, keys):
    return {k: plain(source[k]) for k in keys if k in source}


def replace_fields(target, keys, values):
    for key in keys:
        if key in values:
            target[key] = copy.deepcopy(values[key])
        elif key in target:
            del target[key]


def changed_fields(current, saved, prefix=''):
    """Return field paths only: diagnostics must never include credentials."""
    if isinstance(current, dict) and isinstance(saved, dict):
        paths = []
        for key in sorted(set(current) | set(saved)):
            path = prefix + key
            if key not in current or key not in saved:
                paths.append(path)
            else:
                paths.extend(changed_fields(current[key], saved[key], path + '.'))
        return paths
    return [] if current == saved else [prefix.rstrip('.')]


def profile_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', value):
        raise SwitchError('配置名须为 1～64 位字母、数字、下划线或短横线，并以字母或数字开头。')
    return value


def validate_url(value):
    if not isinstance(value, str):
        raise SwitchError('base_url 必须是 HTTP(S) 地址。')
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in ('https', 'http') and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SwitchError('base_url 必须是 HTTP(S) 地址，不能包含用户名、密码、查询参数或片段。')


def redacted(value, parent=''):
    if isinstance(value, dict):
        return {k: ('<redacted>' if parent in ('launch_env', 'http_headers', 'env_http_headers')
                    or re.search(r'key|(?:^|_)token$|secret|password|authorization|credential', k, re.I)
                    and k not in ('env_key', 'workflowKeywordTriggerEnabled')
                    else redacted(v, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v, parent) for v in value]
    return value


class Manager:
    def __init__(self, root=None):
        self.root = Path(root or Path.home() / ".config/ai-switch").expanduser().resolve()
        self.state_path = self.root / "state.json"

    def load(self):
        if not self.state_path.exists():
            raise SwitchError("尚未初始化；请先运行 ai-switch init。")
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        if state.get("version") != 1:
            raise SwitchError("不支持的 ai-switch 状态版本。")
        pending = self.root / "pending.json"
        if pending.exists():
            raise SwitchError("发现未完成的切换。请先运行 ai-switch recover。")
        return state

    @contextlib.contextmanager
    def lock(self):
        try:
            with platform_io.configuration_lock(self.root / 'lock') as handle:
                yield handle
        except BlockingIOError:
            raise SwitchError("另一个 ai-switch 正在切换配置。") from None

    def apps(self, state=None, requested='all'):
        state = self.load() if state is None else state
        enabled = tuple(app for app in APPS if app in state['active'])
        if not enabled or set(state['active']) != set(enabled) or set(state['paths']) != set(enabled):
            raise SwitchError('已初始化客户端记录无效。')
        if requested == 'all':
            return enabled
        if requested not in enabled:
            raise SwitchError(f'{requested} 未在此管理目录初始化；请使用已初始化客户端，或为它指定独立 --state-dir。')
        return (requested,)

    def profile(self, mode, app):
        path = self.profile_path(mode, app)
        if not path.is_file():
            raise SwitchError(f'配置 {mode} 缺少 {app}；用 ai-switch profile list 查看已有配置。')
        data = json.loads(path.read_text(encoding='utf-8'))
        return self.normalize_profile(mode, app, data)

    def normalize_profile(self, mode, app, data):
        if not isinstance(data, dict):
            raise SwitchError('profile 根节点必须是 JSON 对象。')
        # Old profiles have no options; interpret them without rewriting anything.
        data.setdefault('launch_env', {})
        if 'options' not in data:
            data['options'] = {'read_guard': mode == 'aster'} if app == 'claude' else {}
            if mode == 'aster':
                data['options']['ca_file'] = str(self.root/'assets/astergate-ca.crt')
        self.validate_profile(app, data)
        return data

    def profile_path(self, name, app):
        profile_name(name)
        directory = self.root/'profiles'/name
        path = directory/f'{app}.json'
        if directory.is_symlink() or path.is_symlink():
            raise SwitchError('profile 目录或文件不能是符号链接。')
        return path

    def profile_names(self):
        return sorted(p.name for p in (self.root/'profiles').iterdir()
                      if p.is_dir() and any((p/f'{a}.json').exists() for a in APPS))

    def managed_provider_names(self):
        names = set(MODES)
        directory = self.root/'profiles'
        if directory.exists():
            for mode in self.profile_names():
                path = self.profile_path(mode, 'codex')
                if path.exists():
                    names.update(json.loads(path.read_text(encoding='utf-8')).get('providers', {}))
        return names

    def validate_profile(self, app, data):
        required = {'values', 'env'} if app == 'claude' else {'values', 'agents', 'providers'}
        if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - {'launch_env', 'options'}:
            raise SwitchError('profile 字段不完整或包含未知字段。')
        if any(not isinstance(v, dict) for v in data.values()):
            raise SwitchError('profile 的各配置分组必须是 JSON 对象。')
        if '<redacted>' in json.dumps(data):
            raise SwitchError('不能导入脱敏占位符；请在编辑器或凭据参数中设置真实值。')
        if any(marker in json.dumps(data) for marker in template_profiles.PLACEHOLDERS):
            raise SwitchError('不能使用未替换的公开模板占位符；请用 init 提供自己的凭据和资源。')
        values = data['values']
        allowed = CLAUDE_FIELDS if app == 'claude' else CODEX_FIELDS
        if values.keys() - set(allowed) or not isinstance(values.get('model'), str) or not values['model'].strip():
            raise SwitchError('主模型不能为空，values 只能包含受管理的客户端字段。')
        numeric_fields = ('model_context_window', 'model_auto_compact_token_limit')
        for field, value in values.items():
            if field in numeric_fields:
                if type(value) is not int or value <= 0:
                    raise SwitchError(f'{field} 必须是正整数。')
            elif field == 'modelSettings':
                if not isinstance(value, dict):
                    raise SwitchError('modelSettings 必须是 JSON 对象。')
            elif field not in ('ultracode', 'enableArtifact', 'disableArtifact', 'enableWorkflows', 'disableWorkflows', 'workflowKeywordTriggerEnabled'):
                if not isinstance(value, str) or not value.strip():
                    raise SwitchError(f'{field} 必须是非空字符串。')
        for section in ('env', 'launch_env'):
            for key, value in data.get(section, {}).items():
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(value, str) or '\0' in value:
                    raise SwitchError('环境变量名称无效，或变量值不是字符串。')
        options = data.get('options', {})
        if options.keys() - ({'read_guard', 'ca_file'} if app == 'claude' else {'ca_file'}):
            raise SwitchError('options 包含不支持的选项。')
        if 'ca_file' in options and (not isinstance(options['ca_file'], str) or not options['ca_file']):
            raise SwitchError('ca_file 必须是证书文件路径。')
        if app == 'claude':
            if any(not claude_env_key(k) for k in data['env']):
                raise SwitchError('env 只能包含受管理的 Claude 通道变量。')
            if 'read_guard' in options and type(options['read_guard']) is not bool:
                raise SwitchError('read_guard 必须是布尔值。')
            for field in ('ultracode', 'enableArtifact', 'disableArtifact', 'enableWorkflows', 'disableWorkflows', 'workflowKeywordTriggerEnabled'):
                if field in values and type(values[field]) is not bool:
                    raise SwitchError(f'{field} 必须是布尔值。')
            if 'effortLevel' in values and values['effortLevel'] not in ('low', 'medium', 'high', 'xhigh'):
                raise SwitchError('Claude effort 仅支持 low/medium/high/xhigh。')
            validate_url(data['env'].get('ANTHROPIC_BASE_URL'))
            if options.get('read_guard') and data['env'].get('AI_SWITCH_MODE') not in (None, 'aster'):
                raise SwitchError('Read guard 启用时 AI_SWITCH_MODE 须为空或 aster。')
        else:
            if data['agents'].keys() - set(AGENT_FIELDS) or any(not isinstance(v, str) for v in data['agents'].values()):
                raise SwitchError('agents 只能包含子代理模型及其推理强度。')
            provider_id = values.get('model_provider')
            if not isinstance(provider_id, str) or provider_id not in data['providers']:
                raise SwitchError('model_provider 必须指向本 profile 中的 provider。')
            for provider in data['providers'].values():
                if not isinstance(provider, dict):
                    raise SwitchError('provider 必须是 JSON 对象。')
                if 'base_url' in provider:
                    validate_url(provider['base_url'])
            provider = data['providers'][provider_id]
            validate_url(provider.get('base_url'))
            if provider.get('wire_api') != 'responses' or not isinstance(provider.get('name'), str):
                raise SwitchError('Codex provider 需要 name 和 wire_api="responses"。')
            for field in ('env_key', 'experimental_bearer_token'):
                if field in provider and (not isinstance(provider[field], str) or not provider[field]):
                    raise SwitchError('Codex 凭据字段必须是非空字符串。')
            for field in ('model_reasoning_effort', 'plan_mode_reasoning_effort'):
                if field in values and values[field] not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
                    raise SwitchError('Codex reasoning effort 值无效。')
            # Validate representability before committing any JSON profile or live TOML.
            tomlkit.dumps(dict(values, agents=data['agents'], model_providers=data['providers']))

    def expected_capture(self, app, profile):
        return selected(profile, ('values', 'env') if app == 'claude' else ('values', 'agents', 'providers'))

    def drift(self, app, config, profile):
        fields = changed_fields(self.capture(app, config), self.expected_capture(app, profile))
        if app == 'codex':
            fields = [f.removeprefix('values.').replace('providers.', 'model_providers.', 1) for f in fields]
        else:
            fields = [f.removeprefix('values.') for f in fields]
        effort_only = app == 'codex' and bool(fields) and set(fields) <= set(CODEX_EFFORT_FIELDS)
        if effort_only:
            effort_only = all(config.get(f) is None or config[f] in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
                              for f in fields)
        return fields, effort_only

    def original_snapshot_files(self, profiles):
        directory = self.root/'baseline'
        snapshot = directory/'original-profiles.json'
        manifest = directory/'original-manifest.json'
        if snapshot.exists() or manifest.exists():
            raise SwitchError('原始基准已固定或不完整；拒绝覆盖。请用 baseline status 检查。')
        payload = json_bytes(profiles)
        hashes = {snapshot.name: hashlib.sha256(payload).hexdigest()}
        for name in ('claude.json', 'codex.toml', 'codex-auth.json'):
            path = directory/name
            if path.exists():
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {snapshot: payload,
                manifest: json_bytes({'version': 1, 'files': hashes,
                                      'created_at': dt.datetime.now(dt.timezone.utc).isoformat()})}

    def original_profiles(self):
        directory = self.root/'baseline'
        manifest = directory/'original-manifest.json'
        if not manifest.exists():
            raise SwitchError('旧版原始快照尚未固定凭据与校验值；请先运行 ai-switch baseline protect。')
        document = json.loads(manifest.read_text(encoding='utf-8'))
        hashes = document.get('files', {})
        apps = self.apps()
        required = {'original-profiles.json'} | {app + ('.json' if app == 'claude' else '.toml') for app in apps}
        if document.get('version') != 1 or not required <= hashes.keys() or hashes.keys() - required - {'codex-auth.json'}:
            raise SwitchError('原始基准校验清单无效，拒绝恢复。')
        if (directory/'codex-auth.json').exists() != ('codex-auth.json' in hashes):
            raise SwitchError('原始 Codex 登录快照与校验清单不一致，拒绝恢复。')
        for name, digest in hashes.items():
            path = directory/name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise SwitchError(f'原始基准文件缺失或已被修改：{name}；拒绝恢复。')
        profiles = json.loads((directory/'original-profiles.json').read_text(encoding='utf-8'))
        if set(profiles) != set(apps):
            raise SwitchError('原始基准必须包含全部已初始化客户端。')
        for app, profile in profiles.items():
            self.validate_profile(app, profile)
        return profiles

    def protect_original(self):
        """One-time migration: never use a changed working profile as baseline."""
        with self.lock():
            state = self.load()
            if (self.root/'baseline/original-manifest.json').exists():
                self.original_profiles()
                print('原始 Micu 基准已固定且校验通过，不会重复覆盖。')
                return
            profiles = {}
            for app in self.apps(state):
                path = self.root/'baseline'/(app + ('.json' if app == 'claude' else '.toml'))
                if path.is_symlink():
                    raise SwitchError('原始快照不能是符号链接。')
                config = json.loads(path.read_bytes()) if app == 'claude' else tomlkit.parse(path.read_text(encoding='utf-8'))
                original = self.capture(app, config)
                saved = self.profile('micu', app)
                if original != self.expected_capture(app, saved):
                    raise SwitchError(f'{app} 的 Micu profile 已与原始快照不同，无法安全固定旧版凭据；未修改基准。')
                original['launch_env'] = copy.deepcopy(saved.get('launch_env', {}))
                original['options'] = {'read_guard': False} if app == 'claude' else {}
                self.validate_profile(app, original)
                profiles[app] = original
            self.transaction(self.original_snapshot_files(profiles), state)
        print('已固定初始化时的 Micu 配置及已保存凭据；edit/capture/delete 不会覆盖此基准。')

    def baseline_status(self, name='micu'):
        self.load()
        profiles = self.original_profiles() if name == 'micu' else self.named_baseline(name)['profiles']
        for app, profile in profiles.items():
            print(f'{name}/{app}: 固定基准校验通过 | 主模型 {profile["values"]["model"]}')

    def restore_original(self, app='all', full_config=False, dry_run=False):
        with self.lock():
            state = self.load()
            profiles = self.original_profiles()
            apps = self.apps(state, app)
            files = {}
            for client in apps:
                profile = profiles[client]
                live = Path(state['paths'][client])
                if live.is_symlink():
                    raise SwitchError(f'配置是符号链接，请先明确其管理位置：{live}')
                if full_config:
                    original = self.root/'baseline'/(client + ('.json' if client == 'claude' else '.toml'))
                    content = original.read_bytes()
                    if client == 'codex' and (self.root/'baseline/codex-auth.json').exists():
                        auth = live.with_name('auth.json')
                        if auth.is_symlink():
                            raise SwitchError('Codex auth.json 是符号链接，拒绝覆盖。')
                        files[auth] = (self.root/'baseline/codex-auth.json').read_bytes()
                else:
                    _, config = self.read_config(state, client)
                    content = self.render(client, config, profile, 'micu')
                files[live] = content
                files[self.profile_path('micu', client)] = json_bytes(profile)
                state['active'][client] = 'micu'
            scope = '完整配置（含当时的公共设置，以及存在的 Codex 登录快照）' if full_config else '通道、模型、凭据及专属设置（保留当前公共设置）'
            if dry_run:
                print(f'计划从固定基准恢复 {", ".join(apps)} 的{scope}；会先备份，并重置对应 micu profile。')
                return
            self.transaction(files, state)
        print(f'已从固定基准恢复 {", ".join(apps)} 的{scope}，并重置对应 micu profile。新启动生效。')

    def named_baseline_dir(self, name):
        profile_name(name)
        directory = self.root/'baseline/profiles'/name
        if any(p.is_symlink() for p in (self.root/'baseline', directory.parent, directory)):
            raise SwitchError('基准目录不能是符号链接。')
        return directory

    def baseline_resource_paths(self, app, profile):
        paths = set()
        if profile.get('options', {}).get('ca_file'):
            paths.add(profile['options']['ca_file'])
        if app == 'claude':
            if profile['env'].get('NODE_EXTRA_CA_CERTS'):
                paths.add(profile['env']['NODE_EXTRA_CA_CERTS'])
            if profile.get('options', {}).get('read_guard'):
                paths.add(str(self.root/'assets/read_guard.py'))
        else:
            for key in ('model_catalog_json', 'model_instructions_file'):
                if profile['values'].get(key):
                    paths.add(profile['values'][key])
        result = []
        for value in sorted(paths):
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise SwitchError('固定基准时，模型目录、指令文件和证书须使用绝对路径。')
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise SwitchError(f'基准资源不能经过符号链接：{path.name}')
            path = path.resolve()
            if any(path.is_relative_to(self.root/part) for part in ('baseline', 'profiles', 'backups')):
                raise SwitchError('基准资源不能指向基准、profile 或备份目录。')
            result.append(path)
        return result

    def named_baseline_files(self, name, profiles, configs, source):
        directory = self.named_baseline_dir(name)
        if directory.exists() and any(directory.iterdir()):
            raise SwitchError(f'{name} 基准已存在或不完整，拒绝覆盖。')
        bundle = {'version': 1, 'name': name, 'source': source,
                  'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'profiles': profiles, 'configs': {}, 'resources': {}}
        for app in profiles:
            self.validate_profile(app, profiles[app])
            content = self.render(app, configs[app], profiles[app], name)
            bundle['configs'][app] = base64.b64encode(content).decode()
            bundle['resources'][app] = [dict(path=str(path), data=base64.b64encode(path.read_bytes()).decode())
                                        for path in self.baseline_resource_paths(app, profiles[app])]
        payload = json_bytes(bundle)
        return {directory/'snapshot.json': payload,
                directory/'manifest.json': json_bytes({'version': 1, 'name': name,
                                                        'sha256': hashlib.sha256(payload).hexdigest()})}

    def named_baseline(self, name):
        directory = self.named_baseline_dir(name)
        snapshot, manifest = directory/'snapshot.json', directory/'manifest.json'
        if not snapshot.is_file() or not manifest.is_file():
            raise SwitchError(f'{name} 尚无完整固定基准；用 ai-switch baseline protect {name} 创建。')
        if snapshot.is_symlink() or manifest.is_symlink():
            raise SwitchError('基准文件不能是符号链接。')
        document = json.loads(manifest.read_text(encoding='utf-8'))
        payload = snapshot.read_bytes()
        if (not isinstance(document, dict) or document.get('version') != 1 or document.get('name') != name
                or document.get('sha256') != hashlib.sha256(payload).hexdigest()):
            raise SwitchError(f'{name} 固定基准校验失败，拒绝恢复。')
        bundle = json.loads(payload)
        if not isinstance(bundle, dict) or bundle.get('version') != 1 or bundle.get('name') != name:
            raise SwitchError('固定基准格式无效。')
        for field in ('profiles', 'configs', 'resources'):
            if not isinstance(bundle.get(field), dict) or set(bundle[field]) != set(self.apps()):
                raise SwitchError('固定基准必须包含全部已初始化客户端的配置及资源。')
        for app in self.apps():
            self.validate_profile(app, bundle['profiles'][app])
            content = base64.b64decode(bundle['configs'][app], validate=True)
            config = json.loads(content) if app == 'claude' else tomlkit.parse(content.decode())
            if not isinstance(config, dict):
                raise SwitchError('基准中的完整配置格式无效。')
            resources = bundle['resources'][app]
            expected = {str(p) for p in self.baseline_resource_paths(app, bundle['profiles'][app])}
            if (not isinstance(resources, list) or any(not isinstance(r, dict) for r in resources)
                    or len(resources) != len(expected) or {r.get('path') for r in resources} != expected):
                raise SwitchError('基准资源与 profile 引用不一致。')
            for resource in resources:
                base64.b64decode(resource['data'], validate=True)
        return bundle

    def protect_baseline(self, name='micu', from_backup=None):
        if name == 'micu':
            if from_backup:
                raise SwitchError('Micu 使用初始化时的原始基准，不接受其他备份来源。')
            return self.protect_original()
        with self.lock():
            state = self.load()
            directory = self.named_baseline_dir(name)
            if (directory/'manifest.json').exists():
                self.named_baseline(name)
                print(f'{name} 基准已固定且校验通过，不会重复覆盖。')
                return
            if from_backup:
                backup = Path(from_backup).expanduser().resolve()
                if not backup.is_relative_to(self.root/'backups') or not backup.is_dir():
                    raise SwitchError('--from-backup 须指向本管理目录下的备份子目录。')
                items = json.loads((backup/'snapshot.json').read_text(encoding='utf-8'))
                profiles = {}
                for app in self.apps(state):
                    matches = [item for item in items if item['path'] == str(self.profile_path(name, app)) and item['exists']]
                    if len(matches) != 1:
                        raise SwitchError(f'备份未包含完整的 {name}/{app} profile；拒绝混用当前配置。')
                    data = json.loads(base64.b64decode(matches[0]['data'], validate=True))
                    profiles[app] = self.normalize_profile(name, app, data)
                source = 'backup:' + backup.name
                # Historical profiles do not embed resources. Only freeze managed
                # tutorial assets if they still match the recorded original hashes.
                for app in self.apps(state):
                    for path in self.baseline_resource_paths(app, profiles[app]):
                        if path.parent == self.root/'assets':
                            expected = state.get('asset_hashes', {}).get(path.name)
                            if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                                raise SwitchError(f'历史配置引用的教程资源无法核实：{path.name}')
            else:
                profiles = {app: self.profile(name, app) for app in self.apps(state)}
                source = 'saved-profile'
            configs = {app: self.read_config(state, app)[1] for app in self.apps(state)}
            self.transaction(self.named_baseline_files(name, profiles, configs, source), state)
        print(f'已固定 {name} 的已初始化客户端 profile、引用资源及公共设置快照；当前配置和可编辑 profile 保持不变。')

    def list_baselines(self):
        self.load()
        names = ['micu']
        directory = self.root/'baseline/profiles'
        if directory.is_symlink():
            raise SwitchError('基准目录不能是符号链接。')
        if directory.exists():
            names += sorted(p.name for p in directory.iterdir() if p.is_dir()
                            and any((p/filename).exists() for filename in ('snapshot.json', 'manifest.json')))
        for name in names:
            if name == 'micu':
                profiles, source = self.original_profiles(), '初始化原始配置'
            else:
                bundle = self.named_baseline(name)
                profiles, source = bundle['profiles'], bundle['source']
            print(f'{name} | 校验通过 | ' + ' | '.join(f'{app}: {profiles[app]["values"]["model"]}' for app in profiles)
                  + f' | 来源: {source}')

    def restore_baseline(self, name='micu', app='all', full_config=False, dry_run=False):
        if name == 'micu':
            return self.restore_original(app, full_config, dry_run)
        with self.lock():
            state = self.load()
            bundle = self.named_baseline(name)
            apps = self.apps(state, app)
            files = {}
            reserved = {self.state_path, self.root/'lock', self.root/'pending.json'}
            reserved.update(Path(p) for p in state['paths'].values())
            for client in apps:
                profile = bundle['profiles'][client]
                live = Path(state['paths'][client])
                if live.is_symlink():
                    raise SwitchError(f'配置是符号链接，请先明确其管理位置：{live}')
                if full_config:
                    content = base64.b64decode(bundle['configs'][client], validate=True)
                else:
                    content = self.render(client, self.read_config(state, client)[1], profile, name)
                files[live] = content
                files[self.profile_path(name, client)] = json_bytes(profile)
                for resource in bundle['resources'][client]:
                    target = Path(resource['path'])
                    if target in reserved:
                        raise SwitchError('基准资源路径与管理文件冲突，拒绝恢复。')
                    value = base64.b64decode(resource['data'], validate=True)
                    if target in files and files[target] != value:
                        raise SwitchError('两客户端引用了内容不同的同名资源，拒绝恢复。')
                    if not target.is_file() or target.read_bytes() != value:
                        files[target] = value
                    if target.parent == self.root/'assets' and target.name in state.get('asset_hashes', {}):
                        state['asset_hashes'][target.name] = hashlib.sha256(value).hexdigest()
                state['active'][client] = name
            scope = '完整配置（含固定时的公共设置）' if full_config else '通道设置（保留当前公共设置）'
            action = '计划' if dry_run else '已'
            if not dry_run:
                self.transaction(files, state)
            print(f'{action}从 {name} 固定基准恢复 {", ".join(apps)} 的{scope}和引用资源，并重置对应 profile；'
                  + ('会先备份。' if dry_run else '已备份，新启动生效。'))

    def list_profiles(self):
        state = self.load()
        result = []
        for name in self.profile_names():
            apps = {}
            for app in self.apps(state):
                p = self.profile(name, app)
                url = (p['env']['ANTHROPIC_BASE_URL'] if app == 'claude' else
                       p['providers'][p['values']['model_provider']]['base_url'])
                apps[app] = dict(active=state['active'][app] == name,
                                 model=p['values']['model'], base_url=url)
            result.append(dict(name=name, apps=apps))
        return result

    def show_profile(self, name, app='all'):
        state = self.load()
        return {client: redacted(self.profile(name, client))
                for client in self.apps(state, app)}

    def add_profile(self, name, source):
        profile_name(name)
        with self.lock():
            state = self.load()
            if name in self.profile_names():
                raise SwitchError(f'配置 {name} 已存在，不会覆盖。')
            profiles = {app: self.profile(source, app) for app in self.apps(state)}
            # Every copied account gets a distinct native provider ID. This also
            # keeps session metadata meaningful even when model/endpoint match.
            if 'codex' in profiles:
                provider_id = 'ai_switch_' + name
                _, current = self.read_config(state, 'codex')
                if provider_id in self.managed_provider_names() or provider_id in current.get('model_providers', {}):
                    raise SwitchError('新配置的 Codex provider ID 与已有配置冲突，请换一个名称。')
                codex = profiles['codex']
                provider = copy.deepcopy(codex['providers'][codex['values']['model_provider']])
                provider['name'] = name
                codex['values']['model_provider'] = provider_id
                codex['providers'] = {provider_id: provider}
            files = {}
            for app, p in profiles.items():
                self.validate_profile(app, p)
                files[self.profile_path(name, app)] = json_bytes(p)
            self.transaction(files, state)
        print(f'已从 {source} 新增配置 {name}（含 {", ".join(profiles)}）；当前使用的配置不变。')

    def delete_profile(self, name):
        with self.lock():
            state = self.load()
            if name not in self.profile_names():
                raise SwitchError(f'配置 {name} 不存在。')
            active = [app for app in self.apps(state) if state['active'][app] == name]
            if active:
                raise SwitchError(f'{name} 正被 {", ".join(active)} 使用；请先用 ai-switch use 切换后再删除。')
            files = {self.profile_path(name, app): None for app in self.apps(state)}
            runtime = self.root/'runtime'/f'claude-{name}.json'
            if runtime.exists():
                files[runtime] = None
            self.transaction(files, state)
        print(f'已删除配置 {name}；恢复备份和原始 Micu 快照保留。')

    def edit_profile(self, args):
        with self.lock():
            state = self.load()
            name, app = args.name, args.app
            self.apps(state, app)
            original = self.profile(name, app)
            profile = copy.deepcopy(original)
            changes = any(getattr(args, field, None) is not None for field in
                          ('base_url', 'model', 'effort', 'subagent_model', 'api_key_env',
                           'read_guard', 'ultracode', 'catalog')) or args.clear_subagent or args.clear_catalog or args.ask_api_key
            if args.file and (changes or args.editor):
                raise SwitchError('--file 不能和编辑器或其他修改参数一起使用。')
            if args.editor and changes:
                raise SwitchError('--editor 不能和其他修改参数一起使用。')
            if args.file:
                profile = json.loads(Path(args.file).expanduser().read_text(encoding='utf-8'))
            elif args.editor or not changes:
                if not sys.stdin.isatty():
                    raise SwitchError('交互编辑需要终端；也可使用 --base-url / --model / --file 等参数。')
                editor = platform_runtime.editor_command()
                fd, temporary = tempfile.mkstemp(prefix=f'.edit-{name}-', suffix='.json', dir=self.root)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        platform_io.protect_file(temporary)
                        stream.write(json_bytes(profile))
                    command = editor
                    if not command or subprocess.run([*command, temporary]).returncode:
                        raise SwitchError('编辑器退出失败，配置未更新。')
                    profile = json.loads(Path(temporary).read_text(encoding='utf-8'))
                finally:
                    Path(temporary).unlink(missing_ok=True)
            else:
                self.apply_profile_edits(app, profile, args)
            self.validate_profile(app, profile)
            # Keep ownership stable so an editor cannot overwrite a different
            # profile's provider block or leave an untracked old provider behind.
            if app == 'codex' and (profile['values']['model_provider'] != original['values']['model_provider']
                                  or profile['providers'].keys() != original['providers'].keys()):
                raise SwitchError('编辑不能更改 Codex provider ID；新增独立 provider 请使用 profile add。')
            files = {self.profile_path(name, app): json_bytes(profile)}
            active = state['active'][app] == name
            if active:
                _, config = self.read_config(state, app)
                if self.capture(app, config) != self.expected_capture(app, original):
                    raise SwitchError('当前客户端配置已被外部修改；请先检查 status / capture。')
                files[Path(state['paths'][app])] = self.render(app, config, profile, name)
            if original == profile:
                print('配置未变化。')
                return
            self.transaction(files, state)
        print(f'已修改 {name}/{app}；' + ('已同步当前客户端配置，新启动生效。' if active else '下次切换到该配置时生效。'))

    def apply_profile_edits(self, app, profile, args):
        values = profile['values']
        if app == 'codex' and (args.read_guard is not None or args.ultracode is not None):
            raise SwitchError('--read-guard / --ultracode 只适用于 Claude。')
        if app == 'claude' and (args.catalog is not None or args.clear_catalog):
            raise SwitchError('--catalog / --clear-catalog 只适用于 Codex。')
        if args.base_url is not None:
            validate_url(args.base_url)
            if app == 'claude':
                profile['env']['ANTHROPIC_BASE_URL'] = args.base_url.rstrip('/')
            else:
                profile['providers'][values['model_provider']]['base_url'] = args.base_url.rstrip('/')
        if args.model is not None:
            values['model'] = args.model
            if app == 'claude':
                profile['env']['ANTHROPIC_MODEL'] = args.model
        if args.effort is not None:
            values['effortLevel' if app == 'claude' else 'model_reasoning_effort'] = args.effort
        if args.subagent_model is not None or args.clear_subagent:
            if app == 'claude':
                if args.clear_subagent:
                    for key in list(profile['env']):
                        if key.startswith('CLAUDE_CODE_SUBAGENT_'):
                            del profile['env'][key]
                else:
                    profile['env']['CLAUDE_CODE_SUBAGENT_MODEL'] = args.subagent_model
            elif args.clear_subagent:
                profile['agents'] = {}
            else:
                profile['agents']['default_subagent_model'] = args.subagent_model
        if args.read_guard is not None:
            profile['options']['read_guard'] = args.read_guard == 'on'
            if args.read_guard == 'on':
                # The hook's guard marker is a feature flag, not the profile name.
                profile['env']['AI_SWITCH_MODE'] = 'aster'
            else:
                profile['env'].pop('AI_SWITCH_MODE', None)
        if args.ultracode is not None:
            values['ultracode'] = args.ultracode == 'on'
        if args.catalog is not None:
            path = Path(args.catalog).expanduser().resolve()
            doc = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(doc, dict) or not isinstance(doc.get('models'), list):
                raise SwitchError('model catalog 须包含 models 数组。')
            values['model_catalog_json'] = str(path)
        if args.clear_catalog:
            values.pop('model_catalog_json', None)
        if args.ask_api_key or args.api_key_env:
            if args.api_key_env:
                key = os.environ.get(args.api_key_env)
            else:
                if not sys.stdin.isatty():
                    raise SwitchError('--ask-api-key 需要交互终端。')
                key = getpass.getpass('API key（输入不回显）: ')
            if not key or not key.strip():
                raise SwitchError('没有读到 API key；配置未更新。')
            if app == 'claude':
                for field in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
                    profile['env'].pop(field, None)
                profile['env']['ANTHROPIC_AUTH_TOKEN'] = key
            else:
                provider = profile['providers'][values['model_provider']]
                provider.pop('env_key', None)
                provider['experimental_bearer_token'] = key
                provider['requires_openai_auth'] = False
            profile['launch_env'] = {}

    def read_config(self, state, app):
        path = Path(state["paths"][app])
        if path.is_symlink():
            raise SwitchError(f"配置是符号链接，请先明确其管理位置：{path}")
        data = path.read_bytes()
        value = json.loads(data) if app == "claude" else tomlkit.parse(data.decode())
        if not isinstance(value, dict):
            raise SwitchError(f"配置根节点必须是对象：{path}")
        return data, value

    def hook_command(self):
        return (platform_runtime.hook_command(self.root / "assets/read_guard.py") if os.name == "nt"
                else "python3 " + shlex.quote(str(self.root / "assets/read_guard.py")))

    def clean_hooks(self, hooks):
        result = copy.deepcopy(hooks)
        for event in list(result):
            groups = []
            for group in result[event]:
                g = copy.deepcopy(group)
                g["hooks"] = [h for h in g.get("hooks", [])
                              if h.get("command") != self.hook_command()
                              and not platform_runtime.is_managed_hook(
                                  h.get("command", ""), self.root / "assets/read_guard.py")]
                if g["hooks"]:
                    groups.append(g)
            if groups:
                result[event] = groups
            else:
                del result[event]
        return result

    def capture(self, app, config):
        if app == "claude":
            return {"values": selected(config, CLAUDE_FIELDS),
                    "env": {k: v for k, v in config.get("env", {}).items() if claude_env_key(k)}}
        return {"values": selected(config, CODEX_FIELDS),
                "agents": selected(config.get("agents", {}), AGENT_FIELDS),
                "providers": selected(config.get("model_providers", {}), self.managed_provider_names())}

    def render(self, app, config, profile, mode):
        if app == "claude":
            result = copy.deepcopy(config)
            replace_fields(result, CLAUDE_FIELDS, profile["values"])
            env = result.setdefault("env", {})
            replace_fields(env, set(k for k in env if claude_env_key(k)) | set(profile["env"]), profile["env"])
            hooks = self.clean_hooks(result.get("hooks", {}))
            if profile.get('options', {}).get('read_guard', mode == 'aster'):
                for event in ("PreToolUse", "PostToolUse"):
                    hooks.setdefault(event, []).append({"matcher": "Read|View", "hooks": [
                        {"type": "command", "command": self.hook_command()}]})
            if hooks:
                result["hooks"] = hooks
            elif "hooks" in result:
                del result["hooks"]
            return self.original_if_equal(app, result, json_bytes(result), mode)
        result = copy.deepcopy(config)
        replace_fields(result, CODEX_FIELDS, profile["values"])
        if "agents" not in result and profile["agents"]:
            result["agents"] = tomlkit.table()
        if "agents" in result:
            replace_fields(result["agents"], AGENT_FIELDS, profile["agents"])
            if not result["agents"]:
                del result["agents"]
        if "model_providers" not in result:
            result["model_providers"] = tomlkit.table()
        replace_fields(result["model_providers"], self.managed_provider_names() | set(profile['providers']), profile["providers"])
        data = tomlkit.dumps(result).encode()
        tomlkit.parse(data.decode())
        return self.original_if_equal(app, result, data, mode)

    def original_if_equal(self, app, result, data, mode):
        # Restore original bytes when common settings have not changed meanwhile.
        baseline = self.root / 'baseline' / (app + ('.json' if app == 'claude' else '.toml'))
        if mode == 'micu' and baseline.exists():
            raw = baseline.read_bytes()
            original = json.loads(raw) if app == 'claude' else tomlkit.parse(raw.decode())
            if plain(original) == plain(result):
                return raw
        return data

    def init(self, args):
        with self.lock():
            if self.state_path.exists():
                raise SwitchError("已经初始化，保留现有 Micu 基准；不会重复覆盖。")
            selected_app = getattr(args, 'app', 'all')
            apps = APPS if selected_app == 'all' else (selected_app,)
            paths = {app: str(Path(getattr(args, app+'_dir')).expanduser().resolve() /
                              ('settings.json' if app == 'claude' else 'config.toml')) for app in apps}
            state = {"version": 1, "paths": paths, "active": {a: "micu" for a in apps}, "last_backup": None}
            originals = {}
            configs = {}
            for app in apps:
                originals[app], configs[app] = self.read_config(state, app)
            if 'codex' in apps and configs['codex'].get('model_provider') != 'micu':
                raise SwitchError("初始化需要 Codex 当前使用 micu，以免保存错误的恢复基准。")
            if 'claude' in apps and 'micu' not in configs['claude'].get('env', {}).get('ANTHROPIC_BASE_URL', ''):
                raise SwitchError("初始化需要 Claude 当前使用 Micu。")
            catalog_bytes = None
            if 'codex' in apps:
                if not args.catalog:
                    raise SwitchError('初始化 Codex 需要 --catalog 模型目录路径；仅 Claude 可省略。')
                catalog_bytes = Path(args.catalog).expanduser().read_bytes()
                catalog = json.loads(catalog_bytes)
                slugs = {m['slug'] for m in catalog['models']}
                if not {'gpt-6-astra', 'gemini-3.8-flash-high'} <= slugs:
                    raise SwitchError("模型目录缺少主模型或 Gemini 子代理。")
            ca_bytes = Path(args.ca).expanduser().read_bytes()
            ssl.create_default_context(cadata=ca_bytes.decode())
            assets = self.root / 'assets'
            profiles = {a: self.capture(a, configs[a]) for a in apps}
            for app in apps:
                p = profiles[app]
                p['launch_env'] = {}
                p['options'] = {'read_guard': False} if app == 'claude' else {}
                if app == 'codex':
                    provider = configs[app]['model_providers']['micu']
                    env_key = provider.get('env_key')
                    if env_key:
                        if not os.environ.get(env_key):
                            raise SwitchError(f"缺少 Micu 凭据环境变量 {env_key}。")
                        p['launch_env'][env_key] = os.environ[env_key]
                self.validate_profile(app, p)
            if getattr(args, 'ask_api_key', False):
                if not sys.stdin.isatty():
                    raise SwitchError('--ask-api-key 需要交互终端；自动化请使用 --aster-key-env。')
                key = getpass.getpass('AsterGate API key（输入不回显）: ')
            else:
                key_env = args.aster_key_env or 'ASTERGATE_API_KEY'
                key = os.environ.get(key_env)
                if not key:
                    raise SwitchError(f'缺少 AsterGate 凭据环境变量 {key_env}；也可使用 --ask-api-key。')
            if not key or not key.strip():
                raise SwitchError('没有读到 AsterGate API key；未初始化。')
            aster_profiles = {}
            for app in apps:
                profile = template_profiles.render(app, key, assets)
                if app == 'codex':
                    profile['providers'] = dict(copy.deepcopy(profiles[app]['providers']), **profile['providers'])
                else:
                    inherited = {k: v for k, v in profiles[app]['env'].items()
                                 if 'MODEL' not in k and not k.startswith('CLAUDE_CODE_USE_')
                                 and k not in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY')}
                    profile['env'] = dict(inherited, **profile['env'])
                self.validate_profile(app, profile)
                aster_profiles[app] = profile
            if catalog_bytes is not None:
                atomic_write(assets / 'model-catalog.json', catalog_bytes)
            atomic_write(assets / 'astergate-ca.crt', ca_bytes)
            if 'claude' in apps:
                atomic_write(assets / 'read_guard.py', Path(__file__).with_name('read_guard.py').read_bytes())
            for app, p in profiles.items():
                atomic_write(self.root / 'baseline' / (app + ('.json' if app == 'claude' else '.toml')), originals[app])
                atomic_write(self.root / 'profiles/micu' / f'{app}.json', json_bytes(p))
            for app, profile in aster_profiles.items():
                atomic_write(self.root / 'profiles/aster' / f'{app}.json', json_bytes(profile))
            # Auth is unchanged by use/run; keep a recovery snapshot only.
            if 'codex' in apps:
                auth = Path(paths['codex']).with_name('auth.json')
                if auth.exists():
                    atomic_write(self.root / 'baseline/codex-auth.json', auth.read_bytes())
            pinned = copy.deepcopy(profiles)
            for path, data in self.original_snapshot_files(pinned).items():
                atomic_write(path, data)
            for path, data in self.named_baseline_files('aster', aster_profiles, configs, 'init').items():
                atomic_write(path, data)
            state['asset_hashes'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in assets.iterdir()}
            atomic_write(self.state_path, json_bytes(state))
        print(f'已保存 Micu 基准并准备 {", ".join(apps)} 的 AsterGate 配置。当前仍为 Micu。')

    def use(self, mode, app='all', dry_run=False, discard_changes=False):
        with self.lock():
            state = self.load()
            apps = self.apps(state, app)
            pending = {}
            notices = []
            for client in apps:
                original, config = self.read_config(state, client)
                prior = self.profile(state['active'][client], client)
                fields, effort_only = self.drift(client, config, prior)
                if fields and not effort_only and not discard_changes:
                    raise SwitchError(f'{client} 配置与已保存的 {state["active"][client]} 不一致，字段：{", ".join(fields)}。'
                                      f'要保留变更，用 ai-switch capture --app {client}；'
                                      f'要按已保存配置切换，用 ai-switch use {mode} --app {app} --discard-changes（会先备份）。')
                if fields:
                    kind = '推理强度变化' if effort_only else '未保存变更'
                    notices.append(f'{client} {kind}：{", ".join(fields)}；备份当前配置后使用 {mode} 的已保存设置。')
                profile = self.profile(mode, client)
                content = self.render(client, config, profile, mode)
                if original != content:
                    pending[Path(state['paths'][client])] = content
                state['active'][client] = mode
            if dry_run:
                for notice in notices:print('计划：'+notice)
                print(f'计划切换 {", ".join(apps)} → {mode}；会更新 {len(pending)} 个配置文件。')
                return
            if not pending and state == self.load():
                print(f'{", ".join(apps)} 已是 {mode}。')
                return
            self.transaction(pending, state)
        for notice in notices:print(notice)
        print(f'已切换 {", ".join(apps)} → {mode}。新启动生效；现有进程不被终止。')

    def transaction(self, files, state, before_commit=None):
        backup = self.root / 'backups' / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
        snapshots = []
        for path in [*files, self.state_path]:
            snapshots.append(dict(path=str(path), exists=path.exists(), mode=(path.stat().st_mode & 0o777) if path.exists() else 0o600,
                                  data=base64.b64encode(path.read_bytes()).decode() if path.exists() else ''))
        atomic_write(backup / 'snapshot.json', json_bytes(snapshots))
        if before_commit is not None:
            before_commit()
        atomic_write(self.root / 'pending.json', json_bytes({'backup': str(backup)}))
        try:
            for path, content in files.items():
                if content is None:
                    path.unlink(missing_ok=True)
                    platform_io.sync_directory(path.parent)
                else:
                    atomic_write(path, content)
            state['last_backup'] = str(backup)
            atomic_write(self.state_path, json_bytes(state))
            (self.root / 'pending.json').unlink()
        except BaseException:
            self.restore_snapshot(snapshots)
            (self.root / 'pending.json').unlink(missing_ok=True)
            raise

    def restore_snapshot(self, snapshots):
        for item in snapshots:
            path = Path(item['path'])
            if item['exists']:
                atomic_write(path, base64.b64decode(item['data']), item['mode'])
            else:
                path.unlink(missing_ok=True)

    def recover(self):
        with self.lock():
            journal = self.root / 'pending.json'
            if not journal.exists():
                print('没有待恢复的切换。')
                return
            backup = Path(json.loads(journal.read_text(encoding='utf-8'))['backup'])
            self.restore_snapshot(json.loads((backup/'snapshot.json').read_text(encoding='utf-8')))
            journal.unlink()
        print('已恢复到未完成切换之前。')

    def capture_current(self, app):
        with self.lock():
            state = self.load()
            files = {}
            for client in self.apps(state, app):
                mode = state['active'][client]
                _, config = self.read_config(state, client)
                captured = self.capture(client, config)
                prior = self.profile(mode, client)
                expected = prior['values']['model_provider'] if client == 'codex' else prior['env']['ANTHROPIC_BASE_URL']
                actual = config.get('model_provider') if client == 'codex' else config.get('env', {}).get('ANTHROPIC_BASE_URL')
                if actual != expected:
                    raise SwitchError(f'{client} 当前连接与记录的 {mode} 不符，拒绝覆盖配置。')
                captured['launch_env'] = prior.get('launch_env', {})
                captured['options'] = prior['options']
                if client == 'codex':
                    env_key = config['model_providers'][actual].get('env_key')
                    if env_key:
                        key = os.environ.get(env_key) or captured['launch_env'].get(env_key)
                        if not key:
                            raise SwitchError(f'缺少凭据环境变量 {env_key}。')
                        captured['launch_env'] = {env_key: key}
                    else:
                        captured['launch_env'] = {}
                self.validate_profile(client, captured)
                files[self.profile_path(mode, client)] = json_bytes(captured)
            self.transaction(files, state)
        print('已保存当前模式的通道设置；所有固定基准保持不变。')

    def report(self):
        state = self.load()
        result = {'version': VERSION, 'apps': {}, 'last_backup': state.get('last_backup')}
        for app in self.apps(state):
            _, config = self.read_config(state, app)
            mode = state['active'][app]
            p = self.profile(mode, app)
            capture = self.capture(app, config)
            expected = self.expected_capture(app, p)
            if app == 'codex':
                provider = config.get('model_providers', {}).get(config.get('model_provider'), {})
                url = provider.get('base_url')
                sub = config.get('agents', {}).get('default_subagent_model', '客户端默认')
            else:
                url = config.get('env', {}).get('ANTHROPIC_BASE_URL')
                sub = config.get('env', {}).get('CLAUDE_CODE_SUBAGENT_MODEL', '客户端默认')
            result['apps'][app] = dict(mode=mode, matches_profile=capture==expected, model=config.get('model'),
                                       base_url=url, subagent=sub, config=state['paths'][app])
            fields, effort_only = self.drift(app, config, p)
            result['apps'][app].update(changed_fields=fields, effort_only=effort_only)
        return result

    def reusable_repair(self, session):
        """Reuse existing branches only while their recorded sources are intact.

        Changes made inside the compatible branch are deliberately allowed:
        repeatedly launching the original UUID should keep that continuation.
        """
        manifests = []
        for path in (self.root/'repairs').glob('*.json'):
            if path.is_symlink():
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
            if path.stem != value.get('id'):
                continue
            manifests.append(value)
        visited = set()
        while session not in visited:
            visited.add(session)
            # Native revert rotates the rollout path without changing its UUID
            # or the old file. A byte-identical old source is then a stale branch.
            database = Path(self.load()['paths']['codex']).parent/'state_5.sqlite'
            current_path = None
            if database.is_file():
                conn = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
                try:
                    row = conn.execute('SELECT rollout_path FROM threads WHERE id=?', (session,)).fetchone()
                    if row:
                        current_path = Path(row[0]).resolve()
                finally:
                    conn.close()
            candidates = sorted((m for m in manifests if m.get('source_id') == session),
                                key=lambda m:m.get('created_at_ms',0), reverse=True)
            for manifest in candidates:
                if manifest['id'] in visited or not Path(manifest['rollout_path']).is_file():
                    continue
                if not manifest.get('source_nodes'):
                    continue
                if current_path and current_path != Path(manifest['source_nodes'][0]['path']).resolve():
                    continue
                try:
                    session_repair.verify_sources(manifest)
                except (OSError, session_repair.RepairError):
                    continue
                session = str(uuid.UUID(manifest['id']))
                break
            else:
                return session
        raise SwitchError('修复副本的来源关系循环，未启动客户端。')

    def repair_session(self, session, app='codex', dry_run=False, automatic=False):
        if app != 'codex':
            raise SwitchError('旧 item_ ID 修复目前仅适用于 Codex。')
        try:
            session = str(uuid.UUID(session))
        except ValueError:
            raise SwitchError('请提供完整的 Codex 会话 UUID。') from None
        with self.lock():
            state = self.load()
            self.apps(state, 'codex')
            if automatic:
                requested = session
                session = self.reusable_repair(session)
                if session != requested:
                    print(f'自动复用兼容副本：{requested} → {session}；副本中的后续对话保留。')
            manifest_path = self.root/'repairs'/(session+'.json')
            upgrade = manifest_path.exists()
            try:
                home = Path(state['paths']['codex']).parent
                history_mode = session_repair.native_history_mode()
                if upgrade:
                    if manifest_path.is_symlink():
                        raise SwitchError('副本来源记录经过符号链接，未修改会话。')
                    saved = json.loads(manifest_path.read_text(encoding='utf-8'))
                    if saved.get('id') != session or saved.get('version') not in (1, 2, 3):
                        raise SwitchError('修复副本的来源记录无效，未修改会话。')
                    if automatic or history_mode == 'paginated' or saved.get('history_mode') == 'paginated':
                        plan = session_repair.prepare(home, session, repair_dir=self.root/'repairs',
                                                      source_path=saved['rollout_path'], history_mode=history_mode)
                        if plan['changed'] or plan.get('format_migrated'):
                            upgrade = False
                        elif plan.get('history_mode') != 'paginated':
                            plan = session_repair.prepare_display_upgrade(home, session, saved)
                    else:
                        plan = session_repair.prepare_display_upgrade(home, session, saved)
                else:
                    plan = session_repair.prepare(home, session, repair_dir=self.root/'repairs',
                                                  history_mode=history_mode)
                session_repair.verify_sources(plan)
            except session_repair.RepairError as exc:
                raise SwitchError(str(exc)) from None
            if not plan['changed'] and not plan.get('format_migrated'):
                if automatic:
                    print('启动前检查通过，无需修复。')
                    return session
                print('副本的历史格式与消息显示记录已完整；未修改会话。' if upgrade else '没有发现需要处理的历史格式或已知记录 ID 问题；未创建副本。')
                return None
            manifest = plan['manifest']
            if upgrade:
                print(f'为现有副本补充 {plan["changed"]} 条消息显示记录；UUID、原历史和后续追加记录保持不变。')
            else:
                if plan['changed']:
                    print(f'发现 {plan["changed"]} 个不兼容记录 ID（其中 {manifest.get("regenerated_ids",0)} 个由原生分支重新生成），完整历史 {manifest["records"]} 条记录；正文、工具参数和结果保持不变。')
                else:
                    print(f'需要升级旧版历史格式，完整历史 {manifest["records"]} 条记录；模型上下文保持不变。')
                if manifest.get('history_mode') == 'paginated':
                    print('使用 Codex 0.156+ 分页历史；以新 UUID 登记，支持原生回退编辑，原会话保留。')
            if dry_run:
                print('仅预览，未写入任何会话；不会切换配置或发送模型请求。')
                if automatic:
                    if not upgrade:
                        print('以下副本 UUID 仅为预览，实际启动时才会创建兼容副本。')
                    return manifest['id']
                return None
            destination = plan['path']
            if (destination.exists() and not upgrade) or any(p.is_symlink() for p in (destination, *destination.parents)):
                raise SwitchError('修复副本的目标路径已存在或经过符号链接。')
            self.transaction({destination: plan['content'],
                              self.root/'repairs'/(manifest['id']+'.json'): json_bytes(manifest)}, state,
                             before_commit=lambda: session_repair.verify_sources(plan))
        print(f'已补全现有副本的消息显示：{manifest["id"]}' if upgrade else
              f'已创建修复副本：{manifest["id"]}\n原会话保留：{manifest["source_id"]}')
        print(f'续接：ai-switch run codex --mode aster --session {manifest["id"]}')
        print('以后通过 ai-switch run 续接 Aster 会话会自动检查；原生客户端内新建分支需下次启动时再检查。')
        return manifest['id']

    def pending_repaired_sessions(self, known_ids, include_subagents=False):
        result = []
        for path in (self.root/'repairs').glob('*.json'):
            data = json.loads(path.read_text(encoding='utf-8'))
            if (data['id'] not in known_ids and Path(data['rollout_path']).is_file()
                    and (include_subagents or not data.get('subagent'))):
                result.append(dict(id=data['id'],cwd=data['cwd'],provider=data['provider'],name=data['name'],
                                   subagent=data.get('subagent',False),_sort_time=data['created_at_ms']))
        return result

    def sessions(self, app, limit=20, include_subagents=False):
        if limit < 1:
            raise SwitchError('--limit 必须为正整数。')
        state = self.load()
        self.apps(state, app)
        if app == 'codex':
            db_path = Path(state['paths'][app]).parent / 'state_5.sqlite'
            if not db_path.exists():
                return []
            conn = sqlite3.connect(db_path.as_uri()+'?mode=ro', uri=True)
            try:
                columns = {r[1] for r in conn.execute('PRAGMA table_info(threads)')}
                title = "COALESCE(NULLIF(name,''),title)" if 'name' in columns else 'title'
                order = 'updated_at_ms' if 'updated_at_ms' in columns else 'updated_at'
                extra = ','.join(key if key in columns else 'NULL' for key in ('source', 'thread_source', 'agent_path'))
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                children = ({r[0] for r in conn.execute('SELECT child_thread_id FROM thread_spawn_edges')}
                            if 'thread_spawn_edges' in tables else set())
                rows = conn.execute(f'SELECT id,cwd,model_provider,{title},{extra} FROM threads WHERE archived=0 ORDER BY {order} DESC')
                result = []
                for row in rows:
                    subagent = session_repair.is_subagent(row[4],row[5],row[6]) or row[0] in children
                    if subagent and not include_subagents:
                        continue
                    result.append(dict(id=row[0],cwd=row[1],provider=row[2],name=row[3] or '',subagent=bool(subagent)))
                    if len(result) == limit:
                        break
                pending = self.pending_repaired_sessions({r[0] for r in conn.execute('SELECT id FROM threads')}, include_subagents)
                if pending:
                    times = dict(conn.execute(f'SELECT id,{order} FROM threads'))
                    for item in result:
                        item['_sort_time'] = (times[item['id']] or 0) * (1 if order.endswith('_ms') else 1000)
                    result = sorted(result + pending, key=lambda item:item['_sort_time'], reverse=True)[:limit]
                    for item in result:
                        item.pop('_sort_time', None)
                return result
            finally:
                conn.close()
        projects = Path(state['paths'][app]).parent / 'projects'
        files = sorted(projects.glob('*/*.jsonl'), key=lambda p:p.stat().st_mtime, reverse=True)
        result=[]
        for path in files:
            try:
                uuid.UUID(path.stem)
                cwd='';subagent=False
                with path.open(encoding='utf-8') as stream:
                    for _, line in zip(range(30),stream):
                        item=json.loads(line)
                        subagent = subagent or bool(item.get('isSidechain'))
                        if item.get('cwd'):
                            cwd=item['cwd']
                if subagent and not include_subagents:
                    continue
                result.append(dict(id=path.stem,cwd=cwd,provider='Claude 会话',name='',subagent=subagent))
                if len(result) == limit:
                    break
            except (ValueError,OSError):
                continue
        return result

    def launch(self, args):
        tail = args.client_args
        if tail and tail[0]=='--':tail=tail[1:]
        # Validate before any automatic repair can write a compatible copy.
        forbidden=('-c','--config','--settings','--setting-sources','--model','-m','--profile','--remote','--resume','-r','--continue','--ignore-user-config','--fork-session','--session-id','--effort','--last')
        if any(v.split('=')[0] in forbidden or (v.startswith(('-c','-m','-r')) and not v.startswith('--') and len(v)>2) for v in tail):
            raise SwitchError('透传参数不能覆盖模式、模型、配置或会话；请使用 ai-switch 参数。')
        takeover = getattr(args, 'takeover', False)
        if takeover and (args.app != 'codex' or not args.session):
            raise SwitchError('--takeover 仅用于 run codex --session UUID。')
        session = args.session
        if session:
            try:
                session = str(uuid.UUID(session))
            except ValueError:
                raise SwitchError('续接请提供明确的会话 UUID（用 ai-switch sessions 查找）。') from None
        state = self.load()
        self.apps(state, args.app)
        mode = args.mode or state['active'][args.app]
        profile = self.profile(mode, args.app)
        _, live = self.read_config(state, args.app)
        differences, effort_only = self.drift(args.app, live, self.profile(state['active'][args.app], args.app))
        if differences and not effort_only:
            raise SwitchError('配置已被外部修改，请先检查 status / capture。')
        needs_use = bool((args.mode and mode != state['active'][args.app]) or effort_only)
        if needs_use:
            self.render(args.app, live, profile, mode)  # Validate before signalling a process.
        env = dict(os.environ)
        for k in list(env):
            if args.app == 'codex' and k == 'NODE_EXTRA_CA_CERTS':
                continue  # Shared Node-based MCPs may need the user's existing CA.
            if claude_env_key(k) or k in ('ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN'):
                del env[k]
        env.update(profile.get('launch_env', {}))
        cwd = Path(args.cwd or os.getcwd()).resolve()
        auto_repair = False
        if args.app == 'codex' and session and not getattr(args, 'no_auto_repair', False):
            provider = profile['providers'][profile['values']['model_provider']]
            auto_repair = urlsplit(provider['base_url']).hostname == 'aster.empeirion.cn'
            if auto_repair:
                actual = self.reusable_repair(session)
                if actual != session:
                    print(f'自动复用兼容副本：{session} → {actual}；副本中的后续对话保留。')
                    session = actual
        if session:
            for item in self.sessions(args.app, 100000, include_subagents=True):
                if item['id'] == session and item['cwd'] and not args.cwd:
                    cwd=Path(item['cwd']); break
        if not cwd.is_dir():
            raise SwitchError(f'工作目录不存在：{cwd}')
        executable = shutil.which(args.app)
        if not executable:
            raise SwitchError(f'找不到客户端：{args.app}')
        # Resolve npm launchers before terminating an old session or switching
        # live configuration. Unsupported shims must leave both untouched.
        command_prefix = platform_runtime.native_command([executable])
        planned_takeover = False
        if args.app == 'codex' and session:
            planned_takeover = session_process.ensure_available(
                Path(state['paths']['codex']).parent, session,
                takeover=takeover, dry_run=args.dry_run)
        if needs_use:
            self.use(mode, args.app, args.dry_run)
            if not args.dry_run:
                state = self.load()
        if not args.dry_run and not self.report()['apps'][args.app]['matches_profile']:
            raise SwitchError('配置已被外部修改，请先检查 status / capture。')
        if auto_repair:
            if planned_takeover:
                print('仅预览：旧进程退出后才检查历史；实际续接 UUID 以届时检查结果为准。')
            else:
                session = self.repair_session(session, dry_run=args.dry_run, automatic=True)
        cmd=list(command_prefix)
        if args.app == 'claude':
            claude_dir = Path(state['paths']['claude']).parent.resolve()
            if claude_dir == (Path.home() / '.claude').resolve():
                # Even setting the default directory moves Claude's user MCP
                # config from ~/.claude.json to ~/.claude/.claude.json.
                env.pop('CLAUDE_CONFIG_DIR', None)
            else:
                env['CLAUDE_CONFIG_DIR'] = str(claude_dir)
            env.update(profile['env'])
            # Explicit overrides defeat the saved model/effort when resuming.
            overlay=dict(profile['values'],env=profile['env'])
            overlay.setdefault('ultracode',False)
            runtime=self.root/'runtime'/f'claude-{mode}.json'
            if not args.dry_run:
                atomic_write(runtime,json_bytes(overlay))
            cmd += ['--settings',str(runtime),'--model',profile['values']['model']]
            if profile['values'].get('effortLevel'):
                cmd += ['--effort',profile['values']['effortLevel']]
            if session:
                cmd += ['--resume',session]
        else:
            env['CODEX_HOME']=str(Path(state['paths']['codex']).parent)
            # Explicit overrides pin this launch to the selected profile. Native
            # resume also honors the current user config in Codex 0.155.1.
            cmd += ['-c','model_provider='+json.dumps(profile['values']['model_provider']),
                    '-m',profile['values']['model']]
            if profile['values'].get('model_reasoning_effort'):
                cmd += ['-c','model_reasoning_effort='+json.dumps(profile['values']['model_reasoning_effort'])]
            for key,value in profile.get('agents',{}).items():
                cmd += ['-c',f'agents.{key}='+json.dumps(value)]
            if session:
                cmd += ['resume',session]
        cmd+=tail
        print(f'{args.app} / {mode} / {profile["values"]["model"]}，目录 {cwd}',flush=True)
        if args.dry_run:
            print(platform_runtime.display_command(cmd)); return
        if args.app == 'codex' and session:
            # If someone else claimed it meanwhile, stop rather than terminating
            # an additional process. Native Codex also enforces its writer lock.
            session_process.ensure_available(Path(state['paths']['codex']).parent, session)
        os.chdir(cwd)
        return platform_runtime.launch(cmd, env)

    def check(self, network=False):
        report=self.report()
        errors=[]
        for name, digest in self.load().get('asset_hashes', {}).items():
            path = self.root/'assets'/name
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                errors.append(f'资源缺失或已变更：{name}')
        for app,data in report['apps'].items():
            if not data['matches_profile']:
                if data['effort_only']:
                    print(f'{app}: 推理强度与已保存配置不同，use/run 时会先备份再应用已保存设置。')
                else:
                    errors.append(f'{app}: 配置不一致（{", ".join(data["changed_fields"])}）')
            if not shutil.which(app):errors.append(f'{app}: 未安装客户端')
        for mode in self.profile_names():
            for app in self.apps():
                p=self.profile(mode,app)
                if app=='codex':
                    provider=p['providers'][p['values']['model_provider']]
                    key=(provider.get('experimental_bearer_token') or p.get('launch_env',{}).get(provider.get('env_key')) or os.environ.get(provider.get('env_key','')))
                    url=provider['base_url'].rstrip('/')+'/models'
                else:
                    key=p['env'].get('ANTHROPIC_AUTH_TOKEN') or p['env'].get('ANTHROPIC_API_KEY')
                    url=p['env']['ANTHROPIC_BASE_URL'].rstrip('/')+'/v1/models'
                if not key: errors.append(f'{app}/{mode}: 没有可用凭据')
                if network and key:
                    ctx=ssl.create_default_context()
                    if p['options'].get('ca_file'):ctx.load_verify_locations(p['options']['ca_file'])
                    headers = {'Authorization': 'Bearer '+key, 'User-Agent': 'ai-switch/'+VERSION}
                    if app == 'claude':
                        headers.update({'anthropic-version': '2023-06-01', 'x-api-key': key})
                    req=urllib.request.Request(url,headers=headers)
                    try:
                        with urllib.request.urlopen(req,context=ctx,timeout=15) as response:
                            content=json.load(response)
                            print(f'{app}/{mode}: HTTPS + 认证通过，{len(content.get("data",[]))} 个模型')
                    except urllib.error.HTTPError as exc:
                        errors.append(f'{app}/{mode}: HTTP {exc.code}（/models 检查，不代表推理接口结果）')
                    except (OSError,ValueError) as exc:
                        errors.append(f'{app}/{mode}: {type(exc).__name__}')
        for error in errors:print(error)
        if errors:raise SwitchError('检查未全部通过。')
        print('配置、凭据引用和客户端检查通过。')


class HelpParser(argparse.ArgumentParser):
    """Use the installed command name and Chinese help at every command level."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('formatter_class', argparse.RawDescriptionHelpFormatter)
        kwargs['add_help'] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = '位置参数'
        self._optionals.title = '选项'
        self.add_argument('-h', '--help', action='help', help='显示本页帮助并退出')

    def format_help(self):
        return super().format_help().replace('usage: ', '用法：', 1)


def parser():
    ap = HelpParser(
        prog='ai-switch',
        description='统一管理 Claude Code 和 Codex 的多套接入配置，支持切换及续接已有会话。\n'
                    '切换对当前用户全局生效，不限当前目录；新启动的客户端生效。\n'
                    '本页列出全部一级命令和常用示例；完整参数见 ai-switch 命令 --help。\n'
                    '支持 Linux、macOS 和原生 Windows；平台包共享命令和 profile 格式。',
        epilog='''常用命令：
  ai-switch status                          查看当前使用的配置
  ai-switch profile list                    列出全部配置
  ai-switch profile template aster --app codex
                                             查看内置参考模板，无需初始化或密钥
  ai-switch use aster                       已初始化客户端切到 AsterGate
  ai-switch use micu --app codex             只把 Codex 切回 Micu
  ai-switch baseline list                   列出并校验所有固定基准
  ai-switch baseline restore micu           恢复原始 Micu 通道设置
  ai-switch baseline restore aster          恢复固定的 AsterGate 通道设置
  ai-switch repair-session UUID --dry-run   检查记录 ID 和分页兼容性，预览修复

新增、修改、使用、删除（backup 是你给新配置起的示例名称）：
  ai-switch profile add backup --from micu   先复制已有配置，创建 backup
  ai-switch profile edit backup --app codex  再用编辑器修改 Codex 配置
  ai-switch profile edit backup --app claude 再用编辑器修改 Claude 配置
  ai-switch use backup                      启用新配置
  ai-switch use micu                         删除前先切到其他配置
  ai-switch profile delete backup           删除 backup，保留恢复备份

查看会话并续接：
  ai-switch sessions --app codex --limit 100  列出跨通道的会话；不会打开会话
  ai-switch run codex --mode aster --session UUID
                                             自动检查 Aster 历史兼容性后续接
  ai-switch run codex --session UUID --takeover --dry-run
                                             预览旧进程接管，不发送退出信号
  ai-switch run codex --session UUID --takeover
                                             结束持锁旧进程，释放后再续接
  接管最多等待 15 秒：Linux/macOS 发 SIGTERM；Windows 结束核实过的单个进程。未完成请求可能中断。

其他常用选项（完整说明见对应子命令 --help）：
  run --no-auto-repair                      跳过修复及副本复用，直接续接指定 UUID
  sessions --include-subagents              会话列表也显示子代理和 guardian
  use --discard-changes                    备份未保存改动，再应用目标 profile
  baseline restore --full-config            连同基准里的公共设置一起恢复
  baseline protect PROFILE                  一次性固定自定义配置，不切换通道

详细参数与示例：
  ai-switch 命令 --help                     查看任意一级命令的完整参数
  ai-switch profile 操作 --help             list / show / template / add / edit / delete
  ai-switch baseline 操作 --help            list / status / protect / restore
  ai-switch use --help
  ai-switch baseline restore --help
  ai-switch profile edit --help
  ai-switch sessions --help
  ai-switch repair-session --help
  ai-switch run --help
''')
    ap.add_argument('--version', action='version', version=VERSION, help='显示版本并退出')
    ap.add_argument('--state-dir', metavar='目录', help='指定配置管理目录；默认 ~/.config/ai-switch')
    sp = ap.add_subparsers(dest='command', required=True, title='子命令', metavar='命令')
    p = sp.add_parser('init', help='首次初始化：选择客户端并固定 Micu / AsterGate 基准',
                      description='仅在首次安装后运行；所选客户端须已正常使用自己的 Micu 配置。\n'
                                  '--app codex 或 claude 只管理该客户端；默认 all 管理两者。\n'
                                  '从内置公开模板生成 Aster 配置，使用自己的密钥、CA 和模型目录。\n'
                                  '已有配置时不要重复 init；新增配置请用 profile add。',
                      epilog='示例：\n  ai-switch init --app codex --catalog /path/to/model-catalog.json --ca /path/to/ca.crt --ask-api-key\n'
                             '  ai-switch init --app claude --ca /path/to/ca.crt --ask-api-key\n'
                             '  ai-switch init --catalog /path/to/model-catalog.json --ca /path/to/ca.crt\n\n'
                             '默认从 ASTERGATE_API_KEY 读取凭据；--ask-api-key 需要交互终端。\n'
                             '完整配置步骤见随包 docs/setup.md。')
    p.add_argument('--app', choices=(*APPS, 'all'), default='all', help='初始化哪个客户端；默认 all（两者），不会触碰未选择的客户端')
    p.add_argument('--claude-dir', default=str(Path.home()/'.claude'), help='Claude 配置目录；默认 ~/.claude')
    p.add_argument('--codex-dir', default=str(Path.home()/'.codex'), help='Codex 配置目录；默认 ~/.codex')
    p.add_argument('--catalog', help='教程提供的完整模型目录 JSON；初始化 Codex 时必填，仅 Claude 可省略')
    p.add_argument('--ca', required=True, help='AsterGate CA 证书文件')
    credentials = p.add_mutually_exclusive_group()
    credentials.add_argument('--aster-key-env', default='ASTERGATE_API_KEY', help='存放自己的 AsterGate 密钥的环境变量名；默认 ASTERGATE_API_KEY')
    credentials.add_argument('--ask-api-key', action='store_true', help='在交互终端隐藏输入自己的 AsterGate API key，不放在命令参数中')

    p = sp.add_parser('use', help='切换到已有配置；默认切换全部已初始化客户端',
                      description='切换当前用户的全局配置；已运行进程需退出后重新启动或续接。\n'
                                  'PROFILE 必须已存在，可用 ai-switch profile list 查看。\n'
                                  'Codex 仅推理强度变化时会先备份再切换；其他未保存变更默认会阻止覆盖。',
                      epilog='示例：\n  ai-switch use aster\n  ai-switch use micu --app codex\n  ai-switch use aster --dry-run\n'
                             '  ai-switch use micu --discard-changes\n\n'
                             '--discard-changes 将当前变更留在备份中，不写回原 profile，然后应用目标 profile。\n'
                             '它使用当前保存的 profile；要恢复固定基准，使用 baseline restore PROFILE。')
    p.add_argument('mode', metavar='PROFILE', help='已有配置名称，如 micu、aster 或你创建的 backup')
    p.add_argument('--app', choices=(*APPS, 'all'), default='all', help='要切换的客户端；默认 all（所有已初始化客户端）')
    p.add_argument('--dry-run', action='store_true', help='只预览切换计划，不修改配置')
    p.add_argument('--discard-changes', action='store_true', help='备份未保存变更并按目标配置切换；不 capture 到原 profile')
    p = sp.add_parser('baseline', help='固定、检查或恢复 Micu、AsterGate 及自定义配置基准',
                      description='固定基准独立于可编辑的 profile；edit/capture/delete 均不会改写它。\n'
                                  '恢复前先校验并备份当前文件；不会修改会话历史。\n'
                                  '省略 PROFILE 时默认为 micu，兼容原命令。',
                      epilog='示例：\n  ai-switch baseline list\n  ai-switch baseline status aster\n'
                             '  ai-switch baseline restore micu\n  ai-switch baseline restore aster --dry-run\n'
                             '  ai-switch baseline protect backup')
    actions = p.add_subparsers(dest='baseline_command', required=True, title='基准命令', metavar='操作')
    actions.add_parser('list', help='列出并校验所有已固定基准', description='显示基准来源和主模型，不输出凭据。')
    p = actions.add_parser('status', help='校验指定基准的配置、凭据和资源快照',
                           description='校验指定基准的哈希值，不输出凭据；省略 PROFILE 时检查 micu。')
    p.add_argument('name', nargs='?', default='micu', metavar='PROFILE', help='固定基准名称；默认 micu')
    p = actions.add_parser('protect', help='一次性固定配置；已固定则只校验',
                           description='Micu 固定初始化时的原始快照；其他名称固定当前保存的 profile、引用资源及公共设置。\n'
                                       '不会 capture 未保存改动，也不会切换当前配置。固定后不可覆盖。\n'
                                       '新版 init 自动固定 micu 和 aster。',
                           epilog='示例：\n  ai-switch baseline protect backup\n'
                                  '  ai-switch baseline protect aster --from-backup ~/.config/ai-switch/backups/备份目录')
    p.add_argument('name', nargs='?', default='micu', metavar='PROFILE', help='要固定的配置；默认 micu')
    p.add_argument('--from-backup', help='从本工具的备份目录提取两客户端 profile；用于固定修改前的配置（不适用于 micu）')
    p = actions.add_parser('restore', help='恢复指定基准，并重置、启用对应 profile',
                           description='默认恢复通道、模型、凭据、专属设置及引用资源，保留当前 MCP、权限等公共设置。\n'
                                       '--full-config 也恢复该基准保存的公共设置；Micu 还恢复存在的原始 Codex 登录快照。\n'
                                       '恢复会先备份当前文件，不需要先 capture，也不会修改固定基准或会话历史。',
                           epilog='示例：\n  ai-switch baseline restore --dry-run\n'
                                  '  ai-switch baseline restore micu\n  ai-switch baseline restore aster --app codex\n'
                                  '  ai-switch baseline restore aster --full-config')
    p.add_argument('name', nargs='?', default='micu', metavar='PROFILE', help='要恢复的固定基准；默认 micu')
    p.add_argument('--app', choices=(*APPS, 'all'), default='all', help='恢复哪个客户端；默认 all（所有已初始化客户端）')
    p.add_argument('--full-config', action='store_true', help='整份恢复：包括该基准保存的公共设置；Micu 包含原始登录快照')
    p.add_argument('--dry-run', action='store_true', help='只显示恢复范围，不修改文件')
    p = sp.add_parser('capture', help='保存当前配置的手动修改；不新增配置',
                      description='将客户端当前的模型、凭据等通道设置保存到正在使用的 profile。\n'
                                  '仍须属于同一通道；所有固定基准保留。',
                      epilog='示例：\n  ai-switch capture --app codex\n  ai-switch capture --app claude')
    p.add_argument('--app', choices=(*APPS, 'all'), default='all', help='要保存的客户端；默认 all（所有已初始化客户端）')
    p = sp.add_parser('status', help='查看当前通道、模型及配置是否一致',
                      description='显示已初始化客户端正在使用的配置，以及是否被外部工具修改。',
                      epilog='示例：\n  ai-switch status\n  ai-switch status --json')
    p.add_argument('--json', action='store_true', help='以 JSON 输出状态')
    p = sp.add_parser('check', help='检查配置、凭据与客户端；可选联网检查',
                      description='默认只检查本机配置、资源及凭据引用；--network 会检查各 profile 的模型列表接口。',
                      epilog='示例：\n  ai-switch check\n  ai-switch check --network')
    p.add_argument('--network', action='store_true', help='联网检查模型列表和认证；不发送聊天请求')
    sp.add_parser('recover', help='恢复因中断而未完成的操作',
                  description='存在未完成的切换或配置管理操作时，从恢复日志还原操作前的文件。\n'
                              '没有待恢复操作时不会修改配置。',
                  epilog='示例：\n  ai-switch recover')
    p = sp.add_parser('sessions', help='列出跨通道的历史会话，不打开会话',
                      description='输出文本列表，含会话 UUID 和工作目录；默认显示最近 20 条。\n'
                                  'Codex 列表跨 provider，只显示未归档会话，默认隐藏 subagent 和 guardian。\n'
                                  '过滤后再取 --limit 条；隐藏不删除会话，也不影响通过明确 UUID 续接。',
                      epilog='示例：\n  ai-switch sessions --app codex --limit 100\n  ai-switch sessions --app claude\n'
                             '  ai-switch sessions --app codex --include-subagents\n'
                             '  ai-switch run codex --mode micu --session UUID')
    p.add_argument('--app', choices=APPS, required=True, help='查看哪个客户端的会话')
    p.add_argument('--limit', type=int, default=20, metavar='数量', help='最多列出多少条；默认 20')
    p.add_argument('--include-subagents', action='store_true', help='同时显示子代理及 guardian 会话，标记为 [subagent]')
    p = sp.add_parser('repair-session', help='修复 Codex 历史 ID / 新版回退兼容问题，保留原会话',
                      description='处理旧会话中不兼容的响应记录 ID，适配 Codex 0.156+ 分页回退。\n'
                                  '保留完整历史、正文、推理、工具参数及结果，不修改原会话或祖先文件。\n'
                                  'Codex 0.156+ 会把 legacy 历史迁移到新的分页副本 UUID，支持回退编辑。\n'
                                  '旧版 Codex 的已有 legacy 副本仍可原 UUID 补全界面消息显示。\n'
                                  '同时识别已知修复副本的后续分支重新生成的响应 ID；不保证解决所有断流。\n'
                                  '不会切换配置或发送模型请求；新 UUID 可在 Micu/Aster 之间续接。',
                      epilog='示例：\n  ai-switch repair-session UUID --dry-run\n'
                             '  ai-switch repair-session UUID\n'
                             '  ai-switch run codex --mode aster --session 新UUID\n\n'
                             '新的副本会出现在 sessions 列表；请退出目标会话后操作，避免历史继续变化。')
    p.add_argument('session', metavar='UUID', help='需要修复的 Codex 会话 ID')
    p.add_argument('--app', choices=('codex',), default='codex', help='当前仅支持 codex（默认）')
    p.add_argument('--dry-run', action='store_true', help='检查历史并预览，不创建或更新会话')
    p = sp.add_parser('run', help='启动或续接会话；--takeover 可接管被旧 Codex 进程占用的会话',
                      description='默认按当前配置启动新会话；--session UUID 续接已有会话。\n'
                                  'Codex 接入教程中的 AsterGate 时，自动检查记录 ID 及新版分页历史兼容问题。\n'
                                  '有问题则保留原会话并创建兼容副本；原历史未变时复用已有副本及其后续对话。\n'
                                  'Micu、Claude 和新会话不自动修复；原生客户端内部 fork 需下次启动时检查。\n'
                                  'Codex 会话被占用时默认停止；--takeover 仅终止实际续接 UUID 的独立旧进程。\n'
                                  '等待退出和释放写锁最多 15 秒，不执行强杀；后台服务或多会话进程拒绝接管。\n'
                                  '--mode 会先全局切换该客户端的配置，再启动；不是仅对本次启动生效。',
                      epilog='示例：\n  ai-switch run codex\n  ai-switch run claude --mode aster\n'
                             '  ai-switch run codex --mode micu --session UUID\n'
                             '  ai-switch run codex --session UUID --dry-run\n'
                             '  ai-switch run codex --session UUID --takeover --dry-run\n'
                             '  ai-switch run codex --session UUID --takeover\n'
                             '  ai-switch run codex --mode aster --session UUID --no-auto-repair\n'
                             '  ai-switch run codex -- --no-alt-screen\n\n'
                             'UUID 请从 sessions 列表复制；-- 后可透传不覆盖通道、模型或会话的客户端参数。')
    p.add_argument('app', choices=APPS, help='要启动的客户端')
    p.add_argument('--mode', metavar='PROFILE', help='先切换到指定配置；省略则使用当前配置')
    p.add_argument('--session', metavar='UUID', help='要续接的会话 ID；省略则新开会话')
    p.add_argument('--cwd', metavar='目录', help='工作目录；续接默认使用原会话目录，新会话默认当前目录')
    p.add_argument('--no-auto-repair', action='store_true', help='跳过 Codex/Aster 启动前修复及副本复用，直接续接指定 UUID')
    p.add_argument('--takeover', action='store_true', help='Codex + UUID：核实并结束持锁旧进程，释放写锁后续接；Windows 使用进程终止，未保存工作可能中断')
    p.add_argument('--dry-run', action='store_true', help='预览接管、切换、自动修复和启动命令，不发送信号或写入会话/配置')

    p = sp.add_parser('profile', help='管理配置及公开模板：列出、查看、新增、修改、删除',
                      description='每个 profile 包含已初始化客户端的配置，可分别修改和启用。\n'
                                  'template 查看内置无凭据参考，不需要初始化。\n'
                                  'edit 只修改已有配置；要新增名称，先运行 add。',
                      epilog='示例：\n  ai-switch profile list\n  ai-switch profile show micu\n'
                             '  ai-switch profile add backup --from micu\n'
                             '  ai-switch profile add aster2 --from aster\n'
                             '  ai-switch profile edit backup --app codex\n'
                             '  ai-switch use backup\n  ai-switch use micu\n'
                             '  ai-switch profile delete backup\n\n'
                             '修改地址、模型、密钥等参数：ai-switch profile edit --help')
    actions = p.add_subparsers(dest='profile_command', required=True, title='配置管理命令', metavar='操作')
    p = actions.add_parser('template', help='查看内置 Aster 公开参考模板；无需初始化，不读取用户配置',
                           description='输出含凭据/路径占位符的公开模板，不包含作者或当前用户的密钥。\n'
                                       '使用 init 填入自己的凭据与资源，不能直接使用未替换的占位符。',
                           epilog='示例：\n  ai-switch profile template aster --app codex\n  ai-switch profile template aster --app claude')
    p.add_argument('name', choices=('aster',), help='内置模板名称；当前提供 aster')
    p.add_argument('--app', choices=(*APPS, 'all'), default='all', help='查看哪个客户端的模板；默认 all')
    p = actions.add_parser('list', help='列出全部配置；* 标记正在使用的客户端',
                           description='列出已有配置名称和主模型，* 分别标记 Claude / Codex 当前使用的配置。',
                           epilog='示例：\n  ai-switch profile list\n  ai-switch profile list --json')
    p.add_argument('--json', action='store_true', help='以 JSON 输出列表')
    p = actions.add_parser('show', help='查看已有配置的内容，密钥脱敏',
                           description='查看保存的 profile，不修改配置；密钥显示为 <redacted>。',
                           epilog='示例：\n  ai-switch profile show micu\n  ai-switch profile show aster --app codex')
    p.add_argument('name', metavar='PROFILE', help='已有配置名称')
    p.add_argument('--app', choices=(*APPS, 'all'), default='all', help='查看哪个客户端；默认 all（所有已初始化客户端）')
    p = actions.add_parser('add', help='从已有配置复制，创建新的独立配置',
                           description='复制已初始化客户端的地址、模型、凭据及专属设置；新增后不会自动切换。\n'
                                       '从 micu 复制普通接入配置，从 aster 复制教程配置；随后用 edit 修改。',
                           epilog='示例：\n  ai-switch profile add backup --from micu\n'
                                  '  ai-switch profile edit backup --app codex\n'
                                  '  ai-switch profile add aster2 --from aster')
    p.add_argument('name', metavar='NEW_PROFILE', help='新名称；1～64 位字母、数字、_ 或 -，以字母或数字开头')
    p.add_argument('--from', dest='source', required=True, metavar='PROFILE', help='复制来源，须为已有配置，如 micu 或 aster')
    p = actions.add_parser('delete', help='删除未使用的配置，保留历史与恢复备份',
                           description='任一客户端正在使用该配置时拒绝删除；请先切换到其他配置。\n'
                                       '删除 profile 不删除会话、恢复备份或任何已固定基准。',
                           epilog='示例：\n  ai-switch use micu\n  ai-switch profile delete backup')
    p.add_argument('name', metavar='PROFILE', help='要删除的已有配置名称')
    p = actions.add_parser('edit', help='修改已有配置；可用参数，也可打开编辑器',
                           description='PROFILE 必须已存在；如需创建 backup，先执行：\n'
                                       '  ai-switch profile add backup --from micu\n\n'
                                       '不带修改参数时打开 VISUAL / EDITOR 指定的编辑器，默认 vi。\n'
                                       '修改当前使用的配置会同步写入客户端设置，新启动生效。',
                           epilog='示例：\n'
                                  '  ai-switch profile edit backup --app codex\n'
                                  '  ai-switch profile edit backup --app claude\n'
                                  '  ai-switch profile edit backup --app codex --model gpt-6-astra --effort high\n'
                                  '  ai-switch profile edit backup --app codex \\\n'
                                  '    --base-url https://your-gateway.example/v1 --ask-api-key\n'
                                  '  ai-switch profile edit backup --app claude \\\n'
                                  '    --base-url https://your-gateway.example --ask-api-key\n'
                                  '  ai-switch profile edit backup --app codex --api-key-env BACKUP_API_KEY\n'
                                  '  ai-switch profile edit aster2 --app claude --read-guard on --ultracode on\n\n'
                                  '--api-key-env 填环境变量名；工具读取并保存其值。--ask-api-key 隐藏输入密钥。\n'
                                  '--file / --editor 不能与其他修改参数一起使用。')
    p.add_argument('name', metavar='PROFILE', help='要修改的已有配置名称；不会自动新增')
    p.add_argument('--app', choices=APPS, required=True, help='修改该配置中的哪个客户端')
    p.add_argument('--base-url', help='API 基地址；Codex 通常需含 /v1，Claude 通常不含')
    p.add_argument('--model', help='主模型名称或客户端别名')
    p.add_argument('--effort', help='推理强度，如 high / xhigh / ultra（Codex）')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--subagent-model', help='设置默认子代理模型')
    group.add_argument('--clear-subagent', action='store_true', help='移除默认子代理设置；不修改 Claude 的 Haiku 别名')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--api-key-env', metavar='ENV_VAR', help='读取并私密保存该环境变量的密钥值；参数填变量名')
    group.add_argument('--ask-api-key', action='store_true', help='在终端隐藏输入 API key')
    p.add_argument('--read-guard', choices=('on', 'off'), help='启用或关闭 Gemini Read hook；仅 Claude')
    p.add_argument('--ultracode', choices=('on', 'off'), help='启用或关闭 ultracode 模式；仅 Claude')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--catalog', help='指定模型目录 JSON 文件；仅 Codex')
    group.add_argument('--clear-catalog', action='store_true', help='移除自定义目录，恢复客户端原生模型目录；仅 Codex')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--file', help='从 JSON 文件替换该客户端的完整 profile；不能导入 show 的脱敏输出')
    group.add_argument('--editor', action='store_true', help='在私密临时文件中编辑完整 profile')
    return ap

def main(argv=None):
    ap=parser()
    args,tail=ap.parse_known_args(argv)
    if tail and args.command!='run':ap.error('不支持参数：'+ ' '.join(tail))
    args.client_args=tail
    if args.command == 'profile' and args.profile_command == 'template':
        selected = APPS if args.app == 'all' else (args.app,)
        content = {app: template_profiles.template(app) for app in selected}
        print(json.dumps(content if args.app == 'all' else content[args.app], ensure_ascii=False, indent=2))
        return 0
    manager=Manager(args.state_dir)
    try:
        if args.command=='init':manager.init(args)
        elif args.command=='use':manager.use(args.mode,args.app,args.dry_run,args.discard_changes)
        elif args.command=='status':
            report=manager.report()
            if args.json:print(json.dumps(report,ensure_ascii=False,indent=2))
            else:
                for app,d in report['apps'].items():
                    print(f'{app}: {d["mode"]} | {d["model"]} | 子代理 {d["subagent"]} | {d["base_url"]} | '+('配置一致' if d['matches_profile'] else '配置已变更'))
                    if d['changed_fields']:
                        print('  变化字段：'+', '.join(d['changed_fields'])+('（仅推理强度；切换时自动备份并恢复目标设置）' if d['effort_only'] else ''))
        elif args.command=='check':manager.check(args.network)
        elif args.command=='recover':manager.recover()
        elif args.command=='baseline':
            if args.baseline_command=='list':manager.list_baselines()
            elif args.baseline_command=='status':manager.baseline_status(args.name)
            elif args.baseline_command=='protect':manager.protect_baseline(args.name,args.from_backup)
            elif args.baseline_command=='restore':manager.restore_baseline(args.name,args.app,args.full_config,args.dry_run)
        elif args.command=='capture':manager.capture_current(args.app)
        elif args.command=='sessions':
            for item in manager.sessions(args.app,args.limit,args.include_subagents):
                marker = ' [subagent]' if item.get('subagent') else ''
                print(f'{item["id"]}  {item["provider"]}{marker}  {item["cwd"]}  {item["name"][:60]}')
        elif args.command=='repair-session':manager.repair_session(args.session,args.app,args.dry_run)
        elif args.command=='run':return manager.launch(args) or 0
        elif args.command=='profile':
            if args.profile_command=='list':
                rows=manager.list_profiles()
                if args.json:print(json.dumps(rows,ensure_ascii=False,indent=2))
                else:
                    for row in rows:
                        clients=' | '.join(f'{app}{"*" if data["active"] else ""}: {data["model"]}' for app,data in row['apps'].items())
                        print(f'{row["name"]} | {clients}')
                    print('* 表示该客户端当前使用的配置')
            elif args.profile_command=='show':print(json.dumps(manager.show_profile(args.name,args.app),ensure_ascii=False,indent=2))
            elif args.profile_command=='add':manager.add_profile(args.name,args.source)
            elif args.profile_command=='delete':manager.delete_profile(args.name)
            elif args.profile_command=='edit':manager.edit_profile(args)
    except (SwitchError,session_repair.RepairError,session_process.TakeoverError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        # JSON/TOML parser exceptions may contain secrets from the source; don't echo them.
        print('ai-switch: '+(str(exc) if isinstance(exc,(SwitchError,session_repair.RepairError,session_process.TakeoverError)) else f'{type(exc).__name__}；操作未完成，请检查文件格式与权限。'),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
