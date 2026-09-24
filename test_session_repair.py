import os
import contextlib
import argparse
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import ai_switch as s
import session_repair as repair


class SessionTests(unittest.TestCase):
    def setUp(self):
        version = patch.object(repair, 'native_history_mode', return_value='legacy')
        version.start(); self.addCleanup(version.stop)
        tmp = tempfile.TemporaryDirectory(prefix='ai-switch-session-unit-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root/'codex'
        self.home.mkdir()
        self.m = s.Manager(self.root/'manager')
        self.m.root.mkdir()
        s.atomic_write(self.m.state_path, s.json_bytes(dict(version=1, paths=dict(codex=str(self.home/'config.toml'), claude=str(self.root/'claude/settings.json')), active=dict(codex='micu', claude='micu'), last_backup=None)))
        self.db = sqlite3.connect(self.home/'state_5.sqlite')
        self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, model_provider TEXT, title TEXT, name TEXT, source TEXT, thread_source TEXT, agent_path TEXT, archived INTEGER DEFAULT 0, updated_at INTEGER, updated_at_ms INTEGER); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);')

    def add_thread(self, records, base=None, ident=None, source='cli', thread_source='user', agent_path=None, archived=0, updated=10):
        ident = ident or str(uuid.uuid4())
        meta = dict(id=ident, session_id=ident, timestamp='2026-09-01T00:00:00Z', cwd=str(self.root), model_provider='micu', source=source, history_mode='paginated')
        if base:
            meta.update(history_base=base, forked_from_id=base['thread_id'])
        lines = [dict(type='session_meta', payload=meta)] + records
        path = self.home/'sessions/2026/09/01'/f'rollout-2026-09-01T00-00-00-{ident}.jsonl'
        s.atomic_write(path, b''.join(s.json_bytes(x).replace(b'\n', b'')+b'\n' for x in lines))
        self.db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (ident, str(path), str(self.root), 'micu', 'Fixture thread', None, source, thread_source, agent_path, archived, updated, updated*1000))
        self.db.commit()
        return ident, path

    def message(self, ident='item_old_message', text='Retain item_old_message inside text unchanged'):
        return dict(type='response_item', payload=dict(type='message', id=ident, role='assistant', content=[dict(type='output_text', text=text)]))

    def add_segment(self, ident, parent_path, cut, records, update=True):
        lines=parent_path.read_bytes().splitlines(keepends=True)
        meta=json.loads(lines[0])['payload']
        start=(meta.get('history_base') or {}).get('end_ordinal_exclusive',0)
        offset=len(b''.join(lines[:cut-start]))
        meta['history_base']=dict(thread_id=repair.rollout_segment_id(parent_path),end_ordinal_exclusive=cut,end_byte_offset=offset)
        path=parent_path.with_name(f'rollout-2026-09-23T00-00-00-{ident}_{uuid.uuid4()}.jsonl')
        rows=[dict(type='session_meta',payload=meta)]+records
        for i,row in enumerate(rows):row['ordinal']=cut+i
        s.atomic_write(path,repair.serialize(rows))
        if update:
            self.db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(path),ident));self.db.commit()
        return path

    def cli(self, *args, expected=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = s.main(['--state-dir', str(self.m.root), *args])
        self.assertEqual(result, expected, out.getvalue())
        return out.getvalue()

    def test_list_hides_subagents_before_limit_and_retains_forks(self):
        user, _ = self.add_thread([], updated=10)
        fork, _ = self.add_thread([], base=dict(thread_id=user, end_ordinal_exclusive=1, end_byte_offset=0), updated=11)
        guardian, _ = self.add_thread([], source='{"subagent":{"other":"guardian"}}', thread_source='guardian_review', updated=30)
        sub, _ = self.add_thread([], source='{"subagent":{"thread_spawn":{"depth":1}}}', thread_source='subagent', agent_path='/root/work', updated=29)
        self.add_thread([], archived=1, updated=99)
        rows = self.m.sessions('codex', 2)
        self.assertEqual([x['id'] for x in rows], [fork, user])
        self.assertEqual([x['id'] for x in self.m.sessions('codex', 2, True)], [guardian, sub])
        self.assertNotIn('[subagent]', self.cli('sessions', '--app', 'codex'))
        self.assertIn('[subagent]', self.cli('sessions', '--app', 'codex', '--include-subagents'))
        self.cli('sessions', '--app', 'codex', '--limit', '0', expected=1)

    def test_list_detects_spawn_edges_and_keeps_root_agent_path(self):
        user, _ = self.add_thread([], agent_path='/root')
        child, _ = self.add_thread([], updated=20)
        self.db.execute('INSERT INTO thread_spawn_edges VALUES(?,?)', (user, child)); self.db.commit()
        self.assertEqual([x['id'] for x in self.m.sessions('codex')], [user])

    def test_list_older_schema_uses_source_and_updated_at(self):
        self.db.executescript('DROP TABLE threads; CREATE TABLE threads(id TEXT,cwd TEXT,model_provider TEXT,title TEXT,source TEXT,archived INTEGER,updated_at INTEGER);')
        self.db.executemany('INSERT INTO threads VALUES(?,?,?,?,?,?,?)', [('u', '/root', 'micu', 'User', 'cli', 0, 1), ('s', '/root', 'aster', 'Subagent', '{"subagent":"review"}', 0, 2)])
        self.db.commit()
        self.assertEqual([x['id'] for x in self.m.sessions('codex')], ['u'])

    def test_claude_sidechains_filtered_before_limit(self):
        directory = self.root/'claude/projects/project'
        directory.mkdir(parents=True)
        user, sub = str(uuid.uuid4()), str(uuid.uuid4())
        (directory/(user+'.jsonl')).write_text(json.dumps(dict(cwd=str(self.root), isSidechain=False))+'\n')
        (directory/(sub+'.jsonl')).write_text(json.dumps(dict(cwd=str(self.root), isSidechain=True))+'\n')
        self.assertEqual([x['id'] for x in self.m.sessions('claude',1)], [user])
        self.assertEqual(len(self.m.sessions('claude',20,True)), 2)

    def test_repair_fork_cut_uses_ordinals_and_preserves_payloads(self):
        call = dict(type='response_item', payload=dict(type='function_call',id='item_call',name='exec',arguments='{"id":"item_call"}',call_id='call_keep'))
        output = dict(type='response_item', payload=dict(type='function_call_output',id='fco_keep',call_id='call_keep',output='keep item_call'))
        parent, parent_path = self.add_thread([self.message(), call, output, self.message('item_excluded', 'Must not inherit later turn')])
        reasoning = dict(type='response_item',payload=dict(type='reasoning', id='item_reason',encrypted_content='ENCRYPTED_KEEP',summary=[]))
        event = dict(type='event_msg',payload=dict(type='item_completed',thread_id=parent,item=dict(type='Reasoning',id='item_reason',raw_content=['item_reason'])))
        child, child_path = self.add_thread([reasoning,event],base=dict(thread_id=parent,end_ordinal_exclusive=4,end_byte_offset=1))
        originals={p:p.read_bytes() for p in (parent_path,child_path)}
        plan=repair.prepare(self.home,child);repair.verify_sources(plan)
        self.assertEqual(plan['changed'],3)
        records=[json.loads(line) for line in plan['content'].splitlines()]
        self.assertEqual(len(records),6)
        meta=records[0]['payload'];self.assertEqual(meta['history_mode'],'legacy');self.assertNotIn('history_base',meta)
        self.assertNotEqual(meta['id'],child)
        self.assertEqual(records[1]['payload']['content'][0]['text'],'Retain item_old_message inside text unchanged')
        self.assertEqual(records[2]['payload']['arguments'],call['payload']['arguments'])
        self.assertEqual(records[2]['payload']['call_id'],'call_keep')
        self.assertEqual(records[3]['payload'],output['payload'])
        self.assertEqual(records[4]['payload']['encrypted_content'],'ENCRYPTED_KEEP')
        self.assertNotIn('id',records[4]['payload'])
        self.assertEqual(records[5]['payload']['item']['id'],'item_reason')
        self.assertEqual(records[5]['payload']['item']['raw_content'],['item_reason'])
        self.assertNotIn(b'Must not inherit',plan['content'])
        for p,raw in originals.items():self.assertEqual(p.read_bytes(),raw)

    def test_compaction_history_is_normalized_without_touching_text(self):
        message=self.message()['payload']
        compaction=dict(type='compacted',payload=dict(message='item_old_message',replacement_history=[message],guardian_history=[message],retained_context={'id':'item_old_message'}))
        tid,_=self.add_thread([compaction]);plan=repair.prepare(self.home,tid)
        item=json.loads(plan['content'].splitlines()[1])['payload']
        self.assertNotIn('id',item['replacement_history'][0])
        self.assertNotIn('id',item['guardian_history'][0])
        self.assertEqual(item['message'],'item_old_message');self.assertEqual(item['retained_context'],{'id':'item_old_message'})

    def test_repair_preview_and_create_do_not_write_native_database(self):
        tid,source=self.add_thread([self.message()]);original=source.read_bytes();database=(self.home/'state_5.sqlite').read_bytes();state=self.m.state_path.read_bytes()
        self.cli('repair-session',tid,'--dry-run')
        self.assertEqual(state,self.m.state_path.read_bytes());self.assertFalse((self.m.root/'repairs').exists())
        output=self.cli('repair-session',tid)
        manifest=json.loads(next((self.m.root/'repairs').glob('*.json')).read_text())
        self.assertIn(manifest['id'],output);self.assertEqual(source.read_bytes(),original)
        self.assertEqual((self.home/'state_5.sqlite').read_bytes(),database)
        if os.name != 'nt':self.assertEqual(Path(manifest['rollout_path']).stat().st_mode&0o777,0o600)
        self.assertEqual(self.m.sessions('codex',1)[0]['id'],manifest['id'])
        self.assertEqual(self.m.sessions('codex',1)[0]['cwd'],str(self.root))

    def test_no_change_creates_no_copy_and_invalid_uuid_fails(self):
        tid,_=self.add_thread([self.message('msg_valid')])
        self.assertIn('没有发现',self.cli('repair-session',tid));self.assertFalse((self.m.root/'repairs').exists())
        self.cli('repair-session','invalid',expected=1)

    def test_native_registered_or_archived_copy_is_not_duplicated(self):
        tid,_=self.add_thread([self.message()]);self.cli('repair-session',tid)
        manifest=json.loads(next((self.m.root/'repairs').glob('*.json')).read_text())
        self.db.execute('INSERT INTO threads SELECT ?,?,cwd,model_provider,title,name,source,thread_source,agent_path,0,updated_at,updated_at_ms FROM threads WHERE id=?',(manifest['id'],manifest['rollout_path'],tid));self.db.commit()
        self.assertEqual(sum(x['id']==manifest['id'] for x in self.m.sessions('codex')),1)
        self.db.execute('UPDATE threads SET archived=1 WHERE id=?',(manifest['id'],));self.db.commit()
        self.assertNotIn(manifest['id'],[x['id'] for x in self.m.sessions('codex')])

    def test_changed_source_rejected(self):
        tid,path=self.add_thread([self.message()]);plan=repair.prepare(self.home,tid)
        with path.open('a') as f:f.write('{}\n')
        with self.assertRaises(repair.RepairError):repair.verify_sources(plan)

    def test_missing_ancestor_cycle_boundary_and_symlink_rejected(self):
        tid,path=self.add_thread([self.message()],base=dict(thread_id=str(uuid.uuid4()),end_ordinal_exclusive=3))
        self.cli('repair-session',tid,expected=1)
        records=[json.loads(x) for x in path.read_text().splitlines()];records[0]['payload']['history_base']['thread_id']=tid
        path.write_text(''.join(json.dumps(x)+'\n' for x in records));self.cli('repair-session',tid,expected=1)
        other=self.root/'elsewhere.jsonl';path.rename(other);path.symlink_to(other);self.cli('repair-session',tid,expected=1)
        self.assertFalse((self.m.root/'repairs').exists())

    def test_failed_repair_rolls_back_new_copy_and_manifest(self):
        tid,source=self.add_thread([self.message()]);original=source.read_bytes();state=self.m.state_path.read_bytes();real=s.atomic_write;failed=False
        def fail_once(path,*args,**kwargs):
            nonlocal failed
            if path.parent==self.m.root/'repairs' and not failed:
                failed=True;raise OSError('fixture failure')
            return real(path,*args,**kwargs)
        with patch.object(s,'atomic_write',side_effect=fail_once):self.cli('repair-session',tid,expected=1)
        self.assertEqual(source.read_bytes(),original);self.assertEqual(self.m.state_path.read_bytes(),state)
        self.assertEqual(len(list((self.home/'sessions').rglob('*.jsonl'))),1)
        self.assertFalse((self.m.root/'pending.json').exists())

    def modern_messages(self, turn='turn_fixture', text='保留中文原文'):
        return [dict(type='event_msg', payload=dict(type='task_started', turn_id=turn)),
                dict(type='event_msg', payload=dict(type='item_completed', turn_id=turn,
                     item=dict(type='UserMessage', id='user_'+turn, content=[dict(type='text', text=text, text_elements=[])]))),
                self.message(),
                dict(type='event_msg', payload=dict(type='item_completed', turn_id=turn,
                     item=dict(type='AgentMessage', id='assistant_'+turn, phase='final_answer',
                               content=[dict(type='Text', text='The original answer.')]))) ]

    def old_copy(self):
        tid, original = self.add_thread(self.modern_messages())
        self.cli('repair-session', tid)
        manifest_path = next((self.m.root/'repairs').glob('*.json'))
        manifest = json.loads(manifest_path.read_text())
        path = Path(manifest['rollout_path'])
        records = [json.loads(x) for x in path.read_bytes().splitlines()]
        path.write_bytes(repair.serialize([x for x in records if 'ai_switch_display' not in x]))
        manifest['version'] = 1
        manifest.pop('display_events_version'); manifest.pop('display_events_added')
        manifest_path.write_bytes(s.json_bytes(manifest))
        return manifest['id'], path, manifest_path, original

    def test_new_copy_projects_messages_and_retains_every_original_event(self):
        events = self.modern_messages()
        tid, _ = self.add_thread(events)
        plan = repair.prepare(self.home, tid)
        records = [json.loads(x) for x in plan['content'].splitlines()]
        self.assertEqual(plan['manifest']['display_events_added'], {'user_message':1, 'agent_message':1})
        for event in (events[0], events[1], events[3]): self.assertIn(event, records)
        projected = [x['payload'] for x in records if 'ai_switch_display' in x]
        self.assertEqual(projected[0]['message'], '保留中文原文')
        self.assertEqual(projected[1]['phase'], 'final_answer')
        self.assertEqual(repair.add_display_events(records), (records, {}))

    def test_projection_deduplicates_per_turn_in_mixed_history(self):
        records = self.modern_messages('first') + self.modern_messages('second')
        # A prior native legacy event for the first turn must not be doubled.
        records.insert(2, dict(type='event_msg', payload=repair.legacy_message(records[1]['payload']['item'])))
        fixed, added = repair.add_display_events(records)
        self.assertEqual(added, {'agent_message':2, 'user_message':1})
        self.assertEqual(repair.add_display_events(fixed), (fixed, {}))

    def test_projection_keeps_media_and_utf8_text_element_offsets(self):
        item = dict(type='UserMessage', content=[dict(type='text', text='你好'),
                    dict(type='text', text='@file', text_elements=[dict(byte_range=dict(start=0,end=5),placeholder='@file')]),
                    dict(type='image', url='data:image/png;base64,AA'), dict(type='localImage', path='/tmp/image.png'),
                    dict(type='audio', url='data:audio/wav;base64,AA'), dict(type='localAudio', path='/tmp/audio.wav')])
        original = json.dumps(item)
        payload = repair.legacy_message(item)
        self.assertEqual(payload['message'], '你好\n@file')
        self.assertEqual(payload['text_elements'][0]['byte_range'], dict(start=7,end=12))
        self.assertEqual(payload['images'], ['data:image/png;base64,AA'])
        self.assertEqual(payload['local_images'], ['/tmp/image.png'])
        self.assertEqual(payload['local_audio'], ['/tmp/audio.wav'])
        self.assertEqual(json.dumps(item), original)

    def test_upgrade_dry_run_keeps_same_uuid_appends_context_and_original(self):
        tid, path, mp, original = self.old_copy()
        with path.open('ab') as out: out.write(repair.serialize(self.modern_messages('later', 'Appended after original repair.')))
        before = {p:p.read_bytes() for p in (path, mp, original, self.m.state_path, self.home/'state_5.sqlite')}
        self.assertIn('仅预览', self.cli('repair-session', tid, '--dry-run'))
        for p, raw in before.items(): self.assertEqual(p.read_bytes(), raw)
        self.assertIn(tid, self.cli('repair-session', tid))
        old = [json.loads(x) for x in before[path].splitlines()]
        new = [json.loads(x) for x in path.read_bytes().splitlines()]
        self.assertEqual([x for x in new if 'ai_switch_display' not in x], old)
        self.assertEqual(new[0]['payload']['id'], tid)
        self.assertEqual(original.read_bytes(), before[original])
        self.assertEqual((self.home/'state_5.sqlite').read_bytes(), before[self.home/'state_5.sqlite'])
        self.assertIn('消息显示记录已完整', self.cli('repair-session', tid))
        snapshot = json.loads((Path(self.m.load()['last_backup'])/'snapshot.json').read_text())
        import base64
        self.assertEqual(base64.b64decode(next(x['data'] for x in snapshot if x['path']==str(path))), before[path])

    def test_upgrade_failure_rolls_back_copy_and_manifest(self):
        tid, path, mp, _ = self.old_copy()
        before = {p:p.read_bytes() for p in (path, mp, self.m.state_path)}
        real, failed = s.atomic_write, False
        def fail_once(p, *args, **kwargs):
            nonlocal failed
            if p==mp and not failed:
                failed=True; raise OSError('fixture failure')
            return real(p,*args,**kwargs)
        with patch.object(s, 'atomic_write', side_effect=fail_once): self.cli('repair-session', tid, expected=1)
        for p, raw in before.items(): self.assertEqual(p.read_bytes(), raw)

    def test_upgrade_rejects_invalid_ownership_and_symlink(self):
        tid, path, mp, _ = self.old_copy()
        manifest = json.loads(mp.read_text());manifest['id']=str(uuid.uuid4());mp.write_bytes(s.json_bytes(manifest))
        self.cli('repair-session', tid, expected=1)
        manifest['id']=tid;mp.write_bytes(s.json_bytes(manifest))
        external = self.root/'external.jsonl';path.rename(external);path.symlink_to(external)
        self.cli('repair-session', tid, expected=1)

    def test_upgrade_detects_native_write_during_backup(self):
        tid, path, mp, _ = self.old_copy()
        before = {p:p.read_bytes() for p in (path,mp,self.m.state_path)}
        appended = b'{"type":"event_msg","payload":{"type":"thread_settings_applied"}}\n'
        real = s.atomic_write
        def append_during_backup(p,*args,**kwargs):
            result = real(p,*args,**kwargs)
            if p.name=='snapshot.json':
                with path.open('ab') as out: out.write(appended)
            return result
        with patch.object(s,'atomic_write',side_effect=append_during_backup):
            self.assertIn('发生变化', self.cli('repair-session', tid, expected=1))
        self.assertEqual(path.read_bytes(), before[path]+appended)
        for p in (mp,self.m.state_path): self.assertEqual(p.read_bytes(), before[p])
        self.assertFalse((self.m.root/'pending.json').exists())

    def repaired_native_fork(self, old_manifest=False):
        original, original_path = self.add_thread(self.modern_messages())
        self.cli('repair-session', original)
        mp = next((self.m.root/'repairs').glob('*.json'))
        manifest = json.loads(mp.read_text())
        if old_manifest:
            manifest.pop('removed_response_fingerprints', None)
            mp.write_bytes(s.json_bytes(manifest))
        records = [json.loads(x) for x in Path(manifest['rollout_path']).read_bytes().splitlines()]
        for record in records:
            for item in repair.response_items(record):
                if item.get('type') in repair.PREFIXES and not item.get('id'):
                    item['id'] = repair.PREFIXES[item['type']] + uuid.uuid4().hex
        fork, path = self.add_thread(records[1:])
        rows = [json.loads(x) for x in path.read_bytes().splitlines()]
        rows[0]['payload']['forked_from_id'] = manifest['id']
        path.write_bytes(repair.serialize(rows))
        return fork, path, manifest, mp, original_path

    def test_native_fork_regenerated_ids_removed_by_body_and_lineage(self):
        tid,path,manifest,mp,original = self.repaired_native_fork()
        before={p:p.read_bytes() for p in (path,mp,original)}
        self.assertEqual(repair.prepare(self.home,tid)['changed'],0)
        plan=repair.prepare(self.home,tid,repair_dir=self.m.root/'repairs')
        self.assertEqual(plan['changed'],1)
        self.assertEqual(plan['manifest']['regenerated_ids'],1)
        self.assertEqual(plan['manifest']['repair_reference_ids'],[manifest['id']])
        original_items=[i for r in map(json.loads,before[path].splitlines()) for i in repair.response_items(r)]
        repaired_items=[i for r in map(json.loads,plan['content'].splitlines()) for i in repair.response_items(r)]
        self.assertEqual([{k:v for k,v in i.items() if k!='id'} for i in original_items],repaired_items)
        self.assertIn('由原生分支重新生成', self.cli('repair-session',tid,'--dry-run'))
        for p,raw in before.items(): self.assertEqual(p.read_bytes(),raw)

    def test_old_manifest_fallback_and_multigeneration_fork(self):
        parent,parent_path,manifest,mp,_ = self.repaired_native_fork(old_manifest=True)
        records=[json.loads(x) for x in parent_path.read_bytes().splitlines()]
        child,path=self.add_thread(records[1:])
        rows=[json.loads(x) for x in path.read_bytes().splitlines()]
        rows[0]['payload']['forked_from_id']=parent
        path.write_bytes(repair.serialize(rows))
        plan=repair.prepare(self.home,child,repair_dir=self.m.root/'repairs')
        self.assertEqual(plan['manifest']['regenerated_ids'],1)
        repair.verify_sources(plan)
        # Reference data also has to stay stable until transaction commit.
        with Path(manifest['rollout_path']).open('ab') as f:f.write(b'{}\n')
        with self.assertRaises(repair.RepairError):repair.verify_sources(plan)

    def test_unrelated_session_valid_ids_and_changed_tool_results_are_kept(self):
        tid,path,manifest,_,_ = self.repaired_native_fork()
        records=[json.loads(x) for x in path.read_bytes().splitlines()]
        unrelated,_=self.add_thread(records[1:])
        self.assertEqual(repair.prepare(self.home,unrelated,repair_dir=self.m.root/'repairs')['changed'],0)
        for record in records:
            if record['type']=='response_item':record['payload']['content'][0]['text']='Different body'
        path.write_bytes(repair.serialize(records))
        self.assertEqual(repair.prepare(self.home,tid,repair_dir=self.m.root/'repairs')['changed'],0)

    def test_saved_fingerprints_work_after_reference_rollout_removed(self):
        tid,path,manifest,mp,_=self.repaired_native_fork()
        Path(manifest['rollout_path']).unlink()
        plan=repair.prepare(self.home,tid,repair_dir=self.m.root/'repairs')
        self.assertEqual(plan['changed'],1)
        value=json.loads(mp.read_text());value['removed_response_fingerprints']=['bad'];mp.write_bytes(s.json_bytes(value))
        self.cli('repair-session',tid,expected=1)

    def test_fork_repair_keeps_call_ids_encrypted_reasoning_and_compaction(self):
        call=dict(type='function_call',id='item_call',name='fixture',call_id='call_original',arguments='{}')
        reasoning=dict(type='reasoning',id='item_reason',encrypted_content='ORIGINAL_ENCRYPTED',summary=[])
        tid,_=self.add_thread([dict(type='compacted',payload=dict(message='summary',replacement_history=[call,reasoning]))])
        self.cli('repair-session',tid)
        mp=next((self.m.root/'repairs').glob('*.json'));manifest=json.loads(mp.read_text())
        rows=[json.loads(x) for x in Path(manifest['rollout_path']).read_bytes().splitlines()]
        for i in rows[1]['payload']['replacement_history']:i['id']=repair.PREFIXES[i['type']]+'regenerated'
        fork,path=self.add_thread(rows[1:]);rows=[json.loads(x) for x in path.read_bytes().splitlines()]
        rows[0]['payload']['forked_from_id']=manifest['id'];path.write_bytes(repair.serialize(rows))
        plan=repair.prepare(self.home,fork,repair_dir=self.m.root/'repairs');self.assertEqual(plan['changed'],2)
        items=json.loads(plan['content'].splitlines()[1])['payload']['replacement_history']
        self.assertEqual(items[0]['call_id'],'call_original');self.assertEqual(items[1]['encrypted_content'],'ORIGINAL_ENCRYPTED')
        self.assertTrue(all('id' not in x for x in items))

    def launch_fixture(self, session, mode='aster', app='codex', dry_run=False,
                       no_auto_repair=False, client_args=None, gateway=None, takeover=False):
        args=argparse.Namespace(app=app,mode=mode,session=session,cwd=str(self.root),
                                dry_run=dry_run,no_auto_repair=no_auto_repair,client_args=client_args or [],takeover=takeover)
        url=gateway or ('https://aster.empeirion.cn:44444/v1' if mode=='aster' else 'https://micu.example/v1')
        profile=dict(values=dict(model='gpt-fixture',model_provider=mode),agents={},env={},
                     providers={mode:dict(base_url=url)},launch_env={})
        out=io.StringIO()
        with (patch.object(self.m,'read_config',return_value=(None,{})),
              patch.object(self.m,'drift',return_value=([],False)),
              patch.object(self.m,'profile',return_value=profile),
              patch.object(self.m,'use'),
              patch.object(self.m,'report',return_value=dict(apps={a:dict(matches_profile=True) for a in s.APPS})),
              patch.object(s.shutil,'which',return_value='/bin/'+app),
              patch.object(s.os,'chdir'),patch.object(s.platform_runtime,'launch',return_value=0) as execute,
              contextlib.redirect_stdout(out)):
            self.m.launch(args)
        return out.getvalue(), execute.call_args.args[0] if execute.called else None

    def test_auto_launch_repairs_original_and_selects_new_uuid(self):
        tid,path=self.add_thread(self.modern_messages());before=path.read_bytes()
        output,cmd=self.launch_fixture(tid)
        manifest=json.loads(next((self.m.root/'repairs').glob('*.json')).read_text())
        self.assertNotEqual(manifest['id'],tid);self.assertEqual(cmd[-2:],['resume',manifest['id']])
        self.assertIn(manifest['id'],output);self.assertEqual(path.read_bytes(),before)

    def test_auto_launch_reuses_pending_copy_and_preserves_continuation(self):
        tid,_=self.add_thread(self.modern_messages());_,cmd=self.launch_fixture(tid)
        copy_id=cmd[-1];mp=self.m.root/'repairs'/(copy_id+'.json');manifest=json.loads(mp.read_text());path=Path(manifest['rollout_path'])
        with path.open('ab') as f:f.write(repair.serialize([self.message('msg_native','Later conversation remains')]))
        before=path.read_bytes()
        output,cmd=self.launch_fixture(tid)
        self.assertEqual(cmd[-1],copy_id);self.assertIn('自动复用兼容副本',output)
        self.assertEqual(path.read_bytes(),before);self.assertEqual(len(list((self.m.root/'repairs').glob('*.json'))),1)

    def test_auto_launch_clean_history_creates_no_copy(self):
        tid,path=self.add_thread([self.message('msg_real')]);before=path.read_bytes()
        output,cmd=self.launch_fixture(tid)
        self.assertEqual(cmd[-1],tid);self.assertIn('无需修复',output)
        self.assertEqual(path.read_bytes(),before);self.assertFalse((self.m.root/'repairs').exists())

    def test_auto_launch_changed_source_does_not_reuse_stale_branch(self):
        tid,path=self.add_thread(self.modern_messages());_,first=self.launch_fixture(tid)
        with path.open('ab') as f:f.write(repair.serialize([self.message('item_new','New original turn')]))
        _,second=self.launch_fixture(tid)
        self.assertNotEqual(first[-1],second[-1])
        manifest=json.loads((self.m.root/'repairs'/(second[-1]+'.json')).read_text())
        self.assertIn(b'New original turn',Path(manifest['rollout_path']).read_bytes())

    def test_auto_launch_dry_run_writes_nothing(self):
        tid,path=self.add_thread(self.modern_messages());original=path.read_bytes();state=self.m.state_path.read_bytes()
        output,cmd=self.launch_fixture(tid,dry_run=True)
        self.assertIsNone(cmd);self.assertIn('以下副本 UUID 仅为预览',output)
        self.assertEqual(path.read_bytes(),original);self.assertEqual(self.m.state_path.read_bytes(),state)
        self.assertFalse((self.m.root/'repairs').exists());self.assertFalse((self.m.root/'backups').exists())

    def test_auto_launch_micu_claude_new_session_and_opt_out_bypass(self):
        tid,_=self.add_thread(self.modern_messages())
        for kwargs in (dict(mode='micu'),dict(app='claude'),dict(no_auto_repair=True)):
            with self.subTest(kwargs=kwargs),patch.object(self.m,'repair_session',side_effect=AssertionError('must bypass')):
                _,cmd=self.launch_fixture(tid,**kwargs)
                self.assertEqual(cmd[-1],tid)
        with patch.object(self.m,'repair_session',side_effect=AssertionError('must bypass')):
            self.launch_fixture(None)
        self.assertFalse((self.m.root/'repairs').exists())

    def test_auto_launch_custom_aster_profile_and_other_gateway(self):
        tid,_=self.add_thread(self.modern_messages())
        with patch.object(self.m,'repair_session',side_effect=AssertionError('must bypass')):
            self.launch_fixture(tid,gateway='https://unrelated.example/v1')
        _,cmd=self.launch_fixture(tid,mode='backup',gateway='https://aster.empeirion.cn:44444/v1')
        self.assertNotEqual(cmd[-1],tid)

    def test_auto_launch_native_fork_regenerated_ids(self):
        tid,path,_,_,_=self.repaired_native_fork();before=path.read_bytes()
        output,cmd=self.launch_fixture(tid)
        self.assertNotEqual(cmd[-1],tid);self.assertIn('1 个由原生分支重新生成',output)
        self.assertEqual(path.read_bytes(),before)

    def test_auto_launch_existing_copy_new_bad_ids_and_reuse_chain(self):
        original,_=self.add_thread(self.modern_messages());_,cmd=self.launch_fixture(original)
        first=cmd[-1];m=json.loads((self.m.root/'repairs'/(first+'.json')).read_text());path=Path(m['rollout_path'])
        with path.open('ab') as f:f.write(repair.serialize([self.message('item_later','Later bad API ID')]))
        before=path.read_bytes();_,cmd=self.launch_fixture(first);second=cmd[-1]
        self.assertNotEqual(first,second);self.assertEqual(path.read_bytes(),before)
        _,cmd=self.launch_fixture(original);self.assertEqual(cmd[-1],second)

    def test_auto_launch_deleted_copy_is_recreated(self):
        tid,_=self.add_thread(self.modern_messages());_,cmd=self.launch_fixture(tid)
        first=cmd[-1];manifest=json.loads((self.m.root/'repairs'/(first+'.json')).read_text());Path(manifest['rollout_path']).unlink()
        _,cmd=self.launch_fixture(tid);self.assertNotEqual(cmd[-1],first)

    def test_auto_launch_repair_failure_stops_client_and_preserves_source(self):
        tid,path=self.add_thread(self.modern_messages());before=path.read_bytes()
        with patch.object(self.m,'transaction',side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):self.launch_fixture(tid)
        self.assertEqual(path.read_bytes(),before);self.assertFalse((self.m.root/'repairs').exists())

    def test_auto_launch_rejects_override_before_repair(self):
        tid,_=self.add_thread(self.modern_messages())
        with patch.object(self.m,'repair_session',side_effect=AssertionError('must validate first')):
            with self.assertRaises(s.SwitchError):self.launch_fixture(tid,client_args=['--model','other'])
        self.assertFalse((self.m.root/'repairs').exists())

    def test_no_auto_repair_cli_flag_is_parsed(self):
        with patch.object(s.Manager,'launch',return_value=0) as launch:
            self.cli('run','codex','--session',str(uuid.uuid4()),'--no-auto-repair')
        self.assertTrue(launch.call_args.args[0].no_auto_repair)

    def test_takeover_cli_flag_is_parsed_but_not_passed_to_codex(self):
        tid,_=self.add_thread([self.message('msg_valid')])
        with patch.object(s.Manager,'launch',return_value=0) as launch:
            self.cli('run','codex','--session',tid,'--takeover')
        self.assertTrue(launch.call_args.args[0].takeover)
        with patch.object(s.session_process,'ensure_available',return_value=False) as guard:
            _,cmd=self.launch_fixture(tid,takeover=True)
        self.assertTrue(guard.call_args_list[0].kwargs['takeover'])
        self.assertNotIn('--takeover',cmd)

    def test_takeover_requires_codex_and_uuid_before_any_process_check(self):
        with patch.object(s.session_process,'ensure_available',side_effect=AssertionError('no process check')):
            for kwargs in (dict(session=None),dict(session=str(uuid.uuid4()),app='claude'),dict(session='bad')):
                with self.assertRaises(s.SwitchError):self.launch_fixture(takeover=True,**kwargs)

    def test_takeover_resolves_compatible_copy_before_selecting_process(self):
        tid,_=self.add_thread(self.modern_messages());_,first=self.launch_fixture(tid)
        with patch.object(s.session_process,'ensure_available',return_value=False) as guard:
            _,cmd=self.launch_fixture(tid,takeover=True)
        self.assertEqual(first[-1],cmd[-1])
        self.assertEqual(guard.call_args_list[0].args[1],first[-1])

    def test_busy_session_stops_before_history_repair(self):
        tid,_=self.add_thread(self.modern_messages())
        with patch.object(s.session_process,'ensure_available',side_effect=s.session_process.TakeoverError('busy')):
            with patch.object(self.m,'repair_session',side_effect=AssertionError('must not repair')):
                with self.assertRaises(s.session_process.TakeoverError):self.launch_fixture(tid)
        self.assertFalse((self.m.root/'repairs').exists())

    def test_takeover_preview_does_not_repair_actively_written_history(self):
        tid,_=self.add_thread(self.modern_messages())
        with patch.object(s.session_process,'ensure_available',return_value=True) as guard:
            with patch.object(self.m,'repair_session',side_effect=AssertionError('must not repair')):
                out,cmd=self.launch_fixture(tid,takeover=True,dry_run=True)
        self.assertIn('退出后才检查历史',out);self.assertIsNone(cmd)
        self.assertTrue(guard.call_args.kwargs['dry_run'])
        self.assertFalse((self.m.root/'repairs').exists())

    def test_native_history_version_detection_and_failure(self):
        # Call the original implementation separately from the fixture's default.
        import importlib.util
        spec = importlib.util.spec_from_file_location('repair_version_probe', repair.__file__)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        import subprocess
        for output, expected in [('codex-cli 0.155.1', 'legacy'), ('codex-cli 0.156.0', 'paginated'),
                                 ('codex-cli 0.156.1', 'paginated'), ('codex 1.0.0', 'paginated'),
                                 ('unknown', 'legacy')]:
            with patch.object(module.subprocess, 'run', return_value=argparse.Namespace(stdout=output)):
                self.assertEqual(module.native_history_mode(), expected)
        with patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('codex', 10)):
            self.assertEqual(module.native_history_mode(), 'legacy')

    def test_paginated_migration_new_uuid_preserves_context_and_source(self):
        tid, path, mp, original = self.old_copy()
        recent = self.modern_messages('recent', 'Recently appended message')
        recent[2] = self.message('msg_recent', 'Recent native reply')
        with path.open('ab') as f:
            f.write(repair.serialize(recent))
        before = {p:p.read_bytes() for p in (path, mp, original, self.home/'state_5.sqlite', self.m.state_path)}
        with patch.object(repair, 'native_history_mode', return_value='paginated'):
            self.cli('repair-session', tid, '--dry-run')
            for p, raw in before.items(): self.assertEqual(p.read_bytes(), raw)
            _, command = self.launch_fixture(tid)
            new_id = command[-1]
            self.assertNotEqual(new_id, tid)
            manifest = json.loads((self.m.root/'repairs'/(new_id+'.json')).read_text())
            rows = list(map(json.loads, Path(manifest['rollout_path']).read_bytes().splitlines()))
            self.assertEqual(rows[0]['payload']['history_mode'], 'paginated')
            self.assertEqual([r['ordinal'] for r in rows], list(range(len(rows))))
            for p in (path, mp, original, self.home/'state_5.sqlite'): self.assertEqual(p.read_bytes(), before[p])
            source_items = [i for r in map(json.loads,before[path].splitlines()) for i in repair.response_items(r)]
            self.assertEqual([i for r in rows for i in repair.response_items(r)], source_items)
            self.assertIn(b'Recently appended message', Path(manifest['rollout_path']).read_bytes())
            _, again = self.launch_fixture(tid)
            self.assertEqual(again[-1], new_id)
            self.cli('repair-session', new_id)  # Already paginated, no legacy upgrade.
            self.assertEqual(len(list((self.m.root/'repairs').glob('*.json'))), 2)

    def test_paginated_projection_preserves_media_spans_and_deduplicates(self):
        user = dict(type='user_message', message='你好 @file', text_elements=[dict(byte_range=dict(start=7,end=12),placeholder='@file')],
                    images=['data:image/png;base64,AA'], local_images=['/tmp/image.png'],
                    audio=['data:audio/wav;base64,AA'], local_audio=['/tmp/audio.wav'])
        records = [dict(type='event_msg',payload=dict(type='task_started',turn_id='turn_one')),
                   dict(type='event_msg',payload=user),
                   dict(type='event_msg',payload=dict(type='agent_message',message='Answer',phase='final_answer'))]
        original = repair.serialize(records)
        rows, added = repair.add_paginated_messages(records, 'thread')
        self.assertEqual(added, {'user_message':1, 'agent_message':1})
        projected = next(r['payload']['item'] for r in rows if r.get('ai_switch_paginated'))
        self.assertEqual(repair.legacy_message(projected), user)
        self.assertEqual(repair.serialize(records), original)
        self.assertEqual(repair.add_paginated_messages(rows, 'thread'), (rows, {}))
        # Equal text in another turn must still be displayed.
        second = [dict(type='event_msg',payload=dict(type='task_started',turn_id='turn_two')), records[1]]
        _, added = repair.add_paginated_messages(rows+second, 'thread')
        self.assertEqual(added, {'user_message':1})

    def test_paginated_projection_orphan_message_fails_closed(self):
        with self.assertRaises(repair.RepairError):
            repair.add_paginated_messages([dict(type='event_msg',payload=dict(type='user_message',message='orphan'))], 'thread')

    def test_paginated_id_repair_flattens_fork_and_keeps_modern_events(self):
        parent, _ = self.add_thread(self.modern_messages())
        child, path = self.add_thread([self.message('item_later')], base=dict(thread_id=parent,end_ordinal_exclusive=5))
        before = path.read_bytes()
        plan = repair.prepare(self.home,child,history_mode='paginated')
        rows = list(map(json.loads,plan['content'].splitlines()))
        self.assertEqual(rows[0]['payload']['history_mode'],'paginated')
        self.assertNotIn('history_base',rows[0]['payload'])
        self.assertEqual([r['ordinal'] for r in rows],list(range(len(rows))))
        self.assertFalse(plan['manifest']['paginated_events_added'])
        self.assertFalse(plan['manifest']['display_events_added'])
        self.assertEqual(path.read_bytes(),before)

    def test_format_migration_carries_id_fingerprints_for_later_forks(self):
        tid,path,mp,_ = self.old_copy()
        old = json.loads(mp.read_text())
        plan = repair.prepare(self.home,tid,repair_dir=self.m.root/'repairs',source_path=path,history_mode='paginated')
        self.assertTrue(plan['format_migrated'])
        self.assertEqual(plan['changed'],0)
        self.assertEqual(plan['manifest']['removed_response_fingerprints'],old['removed_response_fingerprints'])

    def test_paginated_clean_history_remains_unchanged(self):
        tid,path=self.add_thread([self.message('msg_valid')]);before=path.read_bytes()
        with patch.object(repair,'native_history_mode',return_value='paginated'):
            _,command=self.launch_fixture(tid)
            self.assertEqual(command[-1],tid)
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse((self.m.root/'repairs').exists())

    def test_paginated_migration_transaction_failure_preserves_original(self):
        tid,path,mp,_ = self.old_copy();before={p:p.read_bytes() for p in (path,mp,self.m.state_path)}
        with patch.object(repair,'native_history_mode',return_value='paginated'):
            with patch.object(self.m,'transaction',side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError):self.launch_fixture(tid)
        for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)
        self.assertEqual(len(list((self.m.root/'repairs').glob('*.json'))),1)

    def test_same_uuid_revert_segments_preserve_selected_prefix(self):
        tid,original=self.add_thread([self.message(text='Keep early'),self.message('item_drop','Dropped by revert')])
        latest=self.add_segment(tid,original,2,[self.message('item_new','Keep revised')])
        before={p:p.read_bytes() for p in (original,latest,self.home/'state_5.sqlite')}
        plan=repair.prepare(self.home,tid,history_mode='paginated',repair_dir=self.m.root/'repairs')
        rows=list(map(json.loads,plan['content'].splitlines()))
        self.assertEqual([r['payload']['content'][0]['text'] for r in rows if r['type']=='response_item'],['Keep early','Keep revised'])
        self.assertEqual(plan['changed'],2)
        repair.verify_sources(plan)
        for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)

    def test_multiple_reverts_resolve_by_byte_boundary_not_latest_database_row(self):
        tid,root=self.add_thread([self.message(text='A'),self.message('item_old','Old longer turn')])
        one=self.add_segment(tid,root,2,[self.message('item_b','B')])
        two=self.add_segment(tid,one,4,[self.message('item_c','C')])
        latest=self.add_segment(tid,two,6,[self.message('item_d','D')])
        plan=repair.prepare(self.home,tid,history_mode='paginated')
        rows=list(map(json.loads,plan['content'].splitlines()))
        self.assertEqual([r['payload']['content'][0]['text'] for r in rows if r['type']=='response_item'],list('ABCD'))
        self.assertEqual(len(plan['manifest']['source_nodes']),4)

    def test_earlier_revert_and_cross_thread_fork_use_original_prefix(self):
        tid,root=self.add_thread([self.message(text='A'),self.message('item_b','B'),self.message('item_old','Wrong old tail')])
        one=self.add_segment(tid,root,3,[self.message('item_later','Wrong newer tail')])
        reverted=self.add_segment(tid,root,2,[self.message('item_revised','Revised')])
        # A different thread forked before the parent's later revert.
        fork,_=self.add_thread([self.message('item_fork','Fork')],base=dict(thread_id=repair.rollout_segment_id(one),end_ordinal_exclusive=5,end_byte_offset=one.stat().st_size))
        plan=repair.prepare(self.home,fork)
        self.assertIn(b'Wrong newer tail',plan['content']);self.assertNotIn(b'Revised',plan['content'])
        plan=repair.prepare(self.home,tid)
        self.assertIn(b'Revised',plan['content']);self.assertNotIn(b'Wrong',plan['content'])

    def test_ambiguous_segment_boundary_is_rejected(self):
        tid,root=self.add_thread([self.message(text='A'),self.message('item_tail','Original tail')])
        first=self.add_segment(tid,root,2,[self.message('item_one','Different branch one')])
        second=self.add_segment(tid,root,2,[self.message('item_two','Another branch two')])
        # Two conflicting files claim the same physical segment identity.
        duplicate=first.with_name(first.name.replace(repair.rollout_segment_id(first),repair.rollout_segment_id(second)))
        duplicate=duplicate.with_name(duplicate.name.replace('T00-00-00','T01-00-00'))
        duplicate.write_bytes(first.read_bytes())
        leaf=self.add_segment(tid,second,4,[self.message('item_leaf','Leaf')])
        rows=list(map(json.loads,leaf.read_bytes().splitlines()))
        rows[0]['payload']['history_base']['end_byte_offset']=1
        leaf.write_bytes(repair.serialize(rows))
        with self.assertRaisesRegex(repair.RepairError,'多个不同候选'):
            repair.prepare(self.home,tid)

    def test_same_uuid_clean_paginated_launch_does_not_create_copy(self):
        tid,root=self.add_thread([self.message('msg_a','A'),self.message('msg_old','Old')])
        latest=self.add_segment(tid,root,2,[self.message('msg_b','B')])
        with patch.object(repair,'native_history_mode',return_value='paginated'):
            out,cmd=self.launch_fixture(tid)
        self.assertEqual(cmd[-1],tid);self.assertIn('无需修复',out)
        self.assertFalse((self.m.root/'repairs').exists())

    def test_source_uuid_reverted_to_new_path_does_not_reuse_stale_copy(self):
        tid,root=self.add_thread([self.message(text='A'),self.message('item_b','Old B')])
        _,first=self.launch_fixture(tid)
        latest=self.add_segment(tid,root,2,[self.message('item_c','Revised C')])
        _,second=self.launch_fixture(tid)
        self.assertNotEqual(first[-1],second[-1])
        manifest=json.loads((self.m.root/'repairs'/(second[-1]+'.json')).read_text())
        self.assertIn(b'Revised C',Path(manifest['rollout_path']).read_bytes())
        self.assertNotIn(b'Old B',Path(manifest['rollout_path']).read_bytes())


if __name__=='__main__':unittest.main()
