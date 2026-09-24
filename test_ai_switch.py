import argparse, contextlib, io, json, os, ssl, tempfile, unittest, uuid
from pathlib import Path
from unittest.mock import patch
import tomlkit
import ai_switch as s
import read_guard

class ManagerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='ai-switch-unit-');self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for app in s.APPS:(self.root/app).mkdir()
  self.claude=self.root/'claude/settings.json';self.codex=self.root/'codex/config.toml'
  self.claude.write_text(json.dumps(dict(env={'ANTHROPIC_BASE_URL':'https://micu.example','ANTHROPIC_AUTH_TOKEN':'MICU_SECRET','ANTHROPIC_MODEL':'claude-sonnet-5','SHARED':'keep'},model='sonnet',effortLevel='high',permissions={'allow':[],'deny':['Read(.env)']},hooks={'PreToolUse':[{'matcher':'Read','hooks':[{'type':'command','command':'echo existing'}]}]})))
  self.codex.write_text('# preserve preferences\nmodel_provider="micu"\nmodel="gpt-6-astra"\nmodel_reasoning_effort="xhigh"\n[model_providers.micu]\nname="Micu"\nbase_url="https://micu.example/v1"\nwire_api="responses"\nenv_key="MICU_FIXTURE_KEY"\n[model_providers.aster]\nname="old inactive provider"\nbase_url="https://old.example/v1"\nwire_api="responses"\n[agents]\nmax_threads=8\n[mcp_servers.sample]\ncommand="echo"\nargs=["keep"]\n')
  self.original={p:p.read_bytes() for p in (self.claude,self.codex)}
  (self.root/'catalog.json').write_text(json.dumps({'models':[{'slug':'gpt-6-astra'},{'slug':'gemini-3.8-flash-high'}]}))
  (self.root/'ca.crt').write_text(ssl.DER_cert_to_PEM_cert(ssl.create_default_context().get_ca_certs(binary_form=True)[0]))
  env=patch.dict(os.environ,{'ASTERGATE_API_KEY':'ASTER_SECRET','MICU_FIXTURE_KEY':'MICU_SECRET'});env.start();self.addCleanup(env.stop)
  self.m=s.Manager(self.root/'state');self.args=argparse.Namespace(claude_dir=str(self.root/'claude'),codex_dir=str(self.root/'codex'),catalog=str(self.root/'catalog.json'),ca=str(self.root/'ca.crt'),aster_key_env='ASTERGATE_API_KEY')
  with contextlib.redirect_stdout(io.StringIO()):self.m.init(self.args)
 def use(self,*args,**kwargs):
  with contextlib.redirect_stdout(io.StringIO()):self.m.use(*args,**kwargs)
 def test_roundtrip_exact_original_and_history(self):
  history=self.root/'codex/history.jsonl';history.write_text('untouched')
  self.use('aster');c=json.loads(self.claude.read_text());d=tomlkit.parse(self.codex.read_text())
  self.assertEqual(c['env']['CLAUDE_CODE_SUBAGENT_MODEL'],'gemini-3.8-flash-high');self.assertTrue(c['ultracode']);self.assertFalse(c['enableArtifact'])
  self.assertEqual(c['permissions'],{'allow':[],'deny':['Read(.env)']});self.assertEqual(len(c['hooks']['PreToolUse']),2)
  self.assertEqual(d['agents']['default_subagent_model'],'gemini-3.8-flash-high');self.assertEqual(d['mcp_servers']['sample']['command'],'echo')
  self.use('micu')
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
  self.assertEqual(history.read_text(),'untouched');self.assertFalse((self.m.root/'pending.json').exists())
 def test_common_settings_edits_survive(self):
  self.use('aster');c=json.loads(self.claude.read_text());c['statusLine']={'type':'command','command':'echo new'};self.claude.write_text(json.dumps(c))
  d=tomlkit.parse(self.codex.read_text());d['agents']['max_threads']=12;d['mcp_servers']['other']={'command':'other'};self.codex.write_text(tomlkit.dumps(d));self.use('micu')
  c=json.loads(self.claude.read_text());d=tomlkit.parse(self.codex.read_text())
  self.assertEqual(c['statusLine']['command'],'echo new');self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL',c['env']);self.assertNotIn('ultracode',c)
  self.assertEqual(d['agents'].unwrap(),{'max_threads':12});self.assertIn('other',d['mcp_servers']);self.assertNotIn('model_catalog_json',d);self.assertIn('# preserve preferences',self.codex.read_text())
 def test_drift_and_explicit_capture(self):
  c=json.loads(self.claude.read_text());c['model']='opus';self.claude.write_text(json.dumps(c))
  with self.assertRaises(s.SwitchError):self.use('aster')
  self.assertFalse(self.m.report()['apps']['claude']['matches_profile'])
  with contextlib.redirect_stdout(io.StringIO()):self.m.capture_current('claude')
  self.use('aster');self.use('micu');self.assertEqual(json.loads(self.claude.read_text())['model'],'opus')
  self.assertEqual((self.m.root/'baseline/claude.json').read_bytes(),self.original[self.claude])
 def test_failed_second_write_rolls_back(self):
  real=s.atomic_write;failed=False
  def fail_once(path,*args,**kwargs):
   nonlocal failed
   if path==self.codex and not failed:failed=True;raise OSError('simulated disk full')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_once):
   with self.assertRaises(OSError):self.use('aster')
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
  self.assertEqual(self.m.load()['active'],{'claude':'micu','codex':'micu'})
 def test_crash_journal_recovery(self):
  state=self.m.load();self.use('aster');backup=self.m.load()['last_backup'];s.atomic_write(self.m.root/'pending.json',s.json_bytes({'backup':backup}))
  with self.assertRaises(s.SwitchError):self.m.load()
  with contextlib.redirect_stdout(io.StringIO()):self.m.recover()
  self.assertEqual(self.m.load(),state)
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
 def test_separate_switch_and_dry_run(self):
  self.use('aster','claude',True)
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
  self.use('aster','claude');self.assertEqual(self.m.load()['active'],{'claude':'aster','codex':'micu'});self.assertEqual(self.codex.read_bytes(),self.original[self.codex])
 def test_secrets_private_and_status_redacted(self):
  self.use('aster')
  for file in self.m.root.rglob('*'):
   if file.is_file():self.assertEqual(file.stat().st_mode&0o777,0o600)
  report=json.dumps(self.m.report())
  for key in ['ASTER_SECRET','MICU_SECRET']:self.assertNotIn(key,report)
 def test_launch_native_resume_overrides(self):
  sid=str(uuid.uuid4())
  for app in s.APPS:
   args=argparse.Namespace(app=app,mode='micu',session=sid,cwd=str(self.root),dry_run=False,client_args=[])
   with patch.object(s.shutil,'which',return_value='/bin/'+app),patch.object(s.os,'chdir'),patch.object(s.os,'execvpe') as launch,contextlib.redirect_stdout(io.StringIO()),patch.dict(os.environ,{'ANTHROPIC_BASE_URL':'https://wrong','CLAUDE_CODE_OAUTH_TOKEN':'wrong','CLAUDE_CODE_SUBAGENT_MODEL':'gemini-3.8-flash-high'}):self.m.launch(args)
   _,cmd,env=launch.call_args.args;self.assertIn(sid,cmd);self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN',env);self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL',env)
   if app=='codex':self.assertIn('model_provider="micu"',cmd);self.assertEqual(env['MICU_FIXTURE_KEY'],'MICU_SECRET')
   else:
    overlay=json.loads((self.m.root/'runtime/claude-micu.json').read_text());self.assertFalse(overlay['ultracode']);self.assertEqual(env['ANTHROPIC_BASE_URL'],'https://micu.example')
    self.assertEqual(env['CLAUDE_CONFIG_DIR'],str(self.claude.parent))
 def test_busy_takeover_does_not_switch_profiles_before_release(self):
  args=argparse.Namespace(app='codex',mode='aster',session=str(uuid.uuid4()),cwd=str(self.root),dry_run=False,client_args=[],takeover=True)
  before={p:p.read_bytes() for p in (self.codex,self.claude,self.m.state_path)}
  with patch.object(s.shutil,'which',return_value='/bin/codex'),patch.object(s.session_process,'ensure_available',side_effect=s.session_process.TakeoverError('timeout')):
   with self.assertRaises(s.session_process.TakeoverError):self.m.launch(args)
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)
 def test_takeover_invalid_configuration_and_workdir_never_reach_process(self):
  args=argparse.Namespace(app='codex',mode='aster',session=str(uuid.uuid4()),cwd=str(self.root/'missing'),dry_run=False,client_args=[],takeover=True)
  with patch.object(s.session_process,'ensure_available',side_effect=AssertionError('no signals')):
   with self.assertRaises(s.SwitchError):self.m.launch(args)
   args.cwd=str(self.root)
   config=tomlkit.parse(self.codex.read_text());config['model']='external-change';self.codex.write_text(tomlkit.dumps(config))
   with self.assertRaises(s.SwitchError):self.m.launch(args)
 def test_default_claude_launch_keeps_native_global_mcp_location(self):
  fake_home=self.root/'user';claude_dir=fake_home/'.claude';claude_dir.mkdir(parents=True)
  settings=claude_dir/'settings.json';settings.write_bytes(self.claude.read_bytes())
  global_config=fake_home/'.claude.json';global_config.write_text('{"mcpServers":{"sample":{"command":"echo"}}}')
  original=global_config.read_bytes()
  state=self.m.load();state['paths']['claude']=str(settings);s.atomic_write(self.m.state_path,s.json_bytes(state))
  args=argparse.Namespace(app='claude',mode='micu',session=None,cwd=str(self.root),dry_run=False,client_args=[])
  with patch.object(s.Path,'home',return_value=fake_home),patch.object(s.shutil,'which',return_value='/bin/claude'),patch.object(s.os,'chdir'),patch.object(s.os,'execvpe') as launch,contextlib.redirect_stdout(io.StringIO()),patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(self.root/'unrelated')}):
   self.m.launch(args)
  self.assertNotIn('CLAUDE_CONFIG_DIR',launch.call_args.args[2])
  self.assertEqual(global_config.read_bytes(),original)
  self.assertFalse((claude_dir/'.claude.json').exists())
 def test_integrity_check_detects_asset_change(self):
  (self.m.root/'assets/read_guard.py').write_text('modified')
  with patch.object(s.shutil,'which',return_value='/bin/tool'),contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(s.SwitchError):self.m.check()
 def test_reinit_preserves_baseline(self):
  with self.assertRaises(s.SwitchError):self.m.init(self.args)
  self.assertEqual((self.m.root/'baseline/claude.json').read_bytes(),self.original[self.claude])

 def cli(self,*args,expected=0):
  output=io.StringIO()
  with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
   result=s.main(['--state-dir',str(self.m.root),*args])
  self.assertEqual(result,expected,output.getvalue())
  return output.getvalue()
 def test_profile_crud_keeps_originals_and_roundtrips(self):
  self.cli('profile','add','backup','--from','micu')
  self.assertEqual([r['name'] for r in self.m.list_profiles()],['aster','backup','micu'])
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
  self.cli('profile','edit','backup','--app','codex','--base-url','https://backup.example/v1','--model','gpt-backup','--effort','high','--api-key-env','ASTERGATE_API_KEY')
  self.cli('profile','edit','backup','--app','claude','--base-url','https://backup.example','--model','claude-backup','--api-key-env','ASTERGATE_API_KEY')
  self.use('backup')
  c=json.loads(self.claude.read_text());d=tomlkit.parse(self.codex.read_text())
  self.assertEqual(c['model'],'claude-backup');self.assertEqual(c['env']['ANTHROPIC_MODEL'],'claude-backup')
  self.assertEqual(d['model_provider'],'ai_switch_backup');self.assertEqual(d['model_providers']['ai_switch_backup']['base_url'],'https://backup.example/v1')
  self.assertNotIn('MICU_FIXTURE_KEY',self.m.profile('backup','codex')['launch_env'])
  self.assertTrue(all(x['matches_profile'] for x in self.m.report()['apps'].values()))
  self.use('micu')
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
  self.cli('profile','delete','backup');self.assertNotIn('backup',self.m.profile_names())
  self.assertEqual((self.m.root/'baseline/codex.toml').read_bytes(),self.original[self.codex])
 def test_copied_tutorial_retains_features_independent_of_name(self):
  self.cli('profile','add','aster-account2','--from','aster');self.use('aster-account2')
  c=json.loads(self.claude.read_text());d=tomlkit.parse(self.codex.read_text())
  self.assertTrue(c['ultracode']);self.assertEqual(len(c['hooks']['PreToolUse']),2)
  self.assertEqual(d['agents']['default_subagent_model'],'gemini-3.8-flash-high')
  self.assertEqual(d['model_provider'],'ai_switch_aster-account2')
  self.assertEqual(self.m.profile('aster-account2','codex')['options']['ca_file'],str(self.m.root/'assets/astergate-ca.crt'))
  self.cli('profile','edit','aster-account2','--app','claude','--read-guard','off','--ultracode','off','--clear-subagent')
  self.cli('profile','edit','aster-account2','--app','codex','--clear-subagent','--clear-catalog')
  c=json.loads(self.claude.read_text());d=tomlkit.parse(self.codex.read_text())
  self.assertFalse(c['ultracode']);self.assertEqual(len(c['hooks']['PreToolUse']),1)
  self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL',c['env']);self.assertNotIn('model_catalog_json',d)
  self.assertNotIn('default_subagent_model',d['agents'])
  self.use('micu')
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
 def test_active_profile_edit_updates_live_and_preserves_common_settings(self):
  self.cli('profile','edit','micu','--app','codex','--model','gpt-other')
  self.assertEqual(tomlkit.parse(self.codex.read_text())['model'],'gpt-other')
  self.assertTrue(self.m.report()['apps']['codex']['matches_profile'])
  self.assertIn('# preserve preferences',self.codex.read_text())
  self.use('aster');self.use('micu')
  self.assertEqual(tomlkit.parse(self.codex.read_text())['model'],'gpt-other')
  self.assertEqual((self.m.root/'baseline/codex.toml').read_bytes(),self.original[self.codex])
 def test_active_profile_edit_failure_rolls_back_profile_and_live(self):
  original_profile=self.m.profile_path('micu','codex').read_bytes();real=s.atomic_write;failed=False
  def fail_once(path,*args,**kwargs):
   nonlocal failed
   if path==self.codex and not failed:failed=True;raise OSError('fixture failure')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_once):self.cli('profile','edit','micu','--app','codex','--model','gpt-other',expected=1)
  self.assertEqual(self.m.profile_path('micu','codex').read_bytes(),original_profile)
  self.assertEqual(self.codex.read_bytes(),self.original[self.codex])
  self.assertTrue(self.m.report()['apps']['codex']['matches_profile'])
 def test_active_profile_edit_rejects_drift(self):
  self.codex.write_text(self.codex.read_text().replace('model="gpt-6-astra"','model="external-model"'))
  self.cli('profile','edit','micu','--app','codex','--model','edited-model',expected=1)
  self.assertEqual(self.m.profile('micu','codex')['values']['model'],'gpt-6-astra')
  self.assertEqual(tomlkit.parse(self.codex.read_text())['model'],'external-model')
 def test_delete_refuses_profile_used_by_either_client(self):
  self.cli('profile','delete','micu',expected=1);self.use('aster','claude')
  self.cli('profile','delete','aster',expected=1);self.cli('profile','delete','micu',expected=1)
 def test_delete_failure_and_crash_recovery(self):
  self.cli('profile','add','backup','--from','micu');before={a:self.m.profile_path('backup',a).read_bytes() for a in s.APPS}
  real=s.atomic_write;failed=False
  def fail_state(path,*args,**kwargs):
   nonlocal failed
   if path==self.m.state_path and not failed:failed=True;raise OSError('fixture failure')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_state):self.cli('profile','delete','backup',expected=1)
  for app,data in before.items():self.assertEqual(self.m.profile_path('backup',app).read_bytes(),data)
  self.cli('profile','delete','backup');backup=self.m.load()['last_backup']
  s.atomic_write(self.m.root/'pending.json',s.json_bytes({'backup':backup}))
  self.cli('recover')
  for app,data in before.items():self.assertEqual(self.m.profile_path('backup',app).read_bytes(),data)
 def test_failed_add_is_not_listed_and_can_retry(self):
  real=s.atomic_write;failed=False
  def fail_second(path,*args,**kwargs):
   nonlocal failed
   if path==self.m.profile_path('backup','codex') and not failed:failed=True;raise OSError('fixture failure')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_second):self.cli('profile','add','backup','--from','micu',expected=1)
  self.assertNotIn('backup',self.m.profile_names());self.cli('profile','add','backup','--from','micu')
 def test_invalid_names_duplicates_and_provider_collisions(self):
  for name in ('../escape','a/b','.','-bad','a'*65):self.cli('profile','add','--from','micu','--',name,expected=1)
  self.cli('profile','add','micu','--from','aster',expected=1)
  d=tomlkit.parse(self.codex.read_text());d['model_providers']['ai_switch_backup']={'name':'unmanaged','base_url':'https://other.example','wire_api':'responses'};self.codex.write_text(tomlkit.dumps(d))
  self.cli('profile','add','backup','--from','micu',expected=1)
  self.assertEqual(tomlkit.parse(self.codex.read_text())['model_providers']['ai_switch_backup']['name'],'unmanaged')
 def test_profile_show_never_displays_credentials(self):
  for name in s.MODES:
   output=self.cli('profile','show',name)
   self.assertIn('<redacted>',output)
   for secret in ['MICU_SECRET','ASTER_SECRET']:self.assertNotIn(secret,output)
  self.assertEqual(len(json.loads(self.cli('profile','list','--json'))),2)
 def test_invalid_edits_leave_profile_and_live_unchanged(self):
  self.cli('profile','add','backup','--from','micu');before=self.m.profile_path('backup','codex').read_bytes()
  for flags in [('--base-url','file:///tmp/x'),('--base-url','https://token@host/v1'),('--base-url','https://host/v1?key=secret'),('--model',''),('--effort','invalid'),('--api-key-env','NO_SUCH_FIXTURE_KEY'),('--read-guard','on')]:
   self.cli('profile','edit','backup','--app','codex',*flags,expected=1)
   self.assertEqual(self.m.profile_path('backup','codex').read_bytes(),before)
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
 def test_import_and_editor_validate_before_commit(self):
  self.cli('profile','add','backup','--from','micu');p=self.m.profile('backup','claude');p['values']['model']='claude-import'
  file=self.root/'edit.json';file.write_text(json.dumps(p))
  self.cli('profile','edit','backup','--app','claude','--file',str(file));self.assertEqual(self.m.profile('backup','claude')['values']['model'],'claude-import')
  file.write_text(json.dumps({'bad':'invalid'}));self.cli('profile','edit','backup','--app','claude','--file',str(file),expected=1)
  def editor(cmd):
   self.assertEqual(Path(cmd[-1]).stat().st_mode&0o777,0o600)
   p=json.loads(Path(cmd[-1]).read_text());p['values']['model']='claude-editor';Path(cmd[-1]).write_text(json.dumps(p))
   return argparse.Namespace(returncode=0)
  with patch.object(s.sys.stdin,'isatty',return_value=True),patch.object(s.subprocess,'run',side_effect=editor):self.cli('profile','edit','backup','--app','claude')
  self.assertEqual(self.m.profile('backup','claude')['values']['model'],'claude-editor');self.assertEqual(list(self.m.root.glob('.edit-*')),[])
 def test_capture_and_launch_custom_profile(self):
  self.cli('profile','add','backup','--from','micu');self.use('backup')
  c=json.loads(self.claude.read_text());c['model']='custom-saved';self.claude.write_text(json.dumps(c))
  self.cli('capture','--app','claude');self.assertEqual(self.m.profile('backup','claude')['values']['model'],'custom-saved')
  self.cli('capture','--app','codex');self.assertTrue(all(x['matches_profile'] for x in self.m.report()['apps'].values()))
  output=self.cli('run','codex','--mode','backup','--dry-run');self.assertIn('model_provider="ai_switch_backup"',output)
 def test_check_includes_added_profiles(self):
  self.cli('profile','add','backup','--from','aster');p=self.m.profile('backup','codex');p['providers']['ai_switch_backup'].pop('experimental_bearer_token')
  s.atomic_write(self.m.profile_path('backup','codex'),s.json_bytes(p))
  with patch.object(s.shutil,'which',return_value='/bin/tool'):
   output=self.cli('check',expected=1)
  self.assertIn('codex/backup',output)
 def test_upgrade_reads_legacy_profiles_without_rewriting(self):
  before={path:path.read_bytes() for path in (self.m.root/'profiles').glob('*/*.json')}
  self.cli('profile','list');self.cli('profile','show','aster')
  for path,data in before.items():self.assertEqual(path.read_bytes(),data)
 def test_managed_provider_cleanup_preserves_unmanaged_provider(self):
  d=tomlkit.parse(self.codex.read_text());d['model_providers']['unmanaged']={'name':'unmanaged','base_url':'https://other.example','wire_api':'responses'};self.codex.write_text(tomlkit.dumps(d))
  self.cli('profile','add','backup','--from','micu');self.cli('profile','add','third','--from','backup')
  for mode in ['backup','third','micu']:
   self.use(mode);d=tomlkit.parse(self.codex.read_text());self.assertIn('unmanaged',d['model_providers'])
  self.assertNotIn('ai_switch_backup',d['model_providers']);self.assertNotIn('ai_switch_third',d['model_providers'])

 def test_native_effort_change_does_not_block_switch_or_overwrite_profile(self):
  self.use('aster')
  profile=self.m.profile_path('aster','codex').read_bytes()
  d=tomlkit.parse(self.codex.read_text());d['model_reasoning_effort']='xhigh';self.codex.write_text(tomlkit.dumps(d));modified=self.codex.read_bytes()
  report=self.m.report()['apps']['codex'];self.assertTrue(report['effort_only']);self.assertEqual(report['changed_fields'],['model_reasoning_effort'])
  self.use('micu')
  self.assertEqual(self.m.profile_path('aster','codex').read_bytes(),profile)
  self.assertEqual(self.codex.read_bytes(),self.original[self.codex])
  backup=Path(self.m.load()['last_backup'])/'snapshot.json'
  import base64
  saved=next(x for x in json.loads(backup.read_text()) if x['path']==str(self.codex))
  self.assertEqual(base64.b64decode(saved['data']),modified)
 def test_launch_reconciles_effort_change_for_same_profile(self):
  self.use('aster');d=tomlkit.parse(self.codex.read_text());d['model_reasoning_effort']='xhigh';self.codex.write_text(tomlkit.dumps(d))
  args=argparse.Namespace(app='codex',mode='aster',session=None,cwd=str(self.root),dry_run=False,client_args=[])
  with patch.object(s.shutil,'which',return_value='/bin/codex'),patch.object(s.os,'chdir'),patch.object(s.os,'execvpe') as launch,contextlib.redirect_stdout(io.StringIO()):self.m.launch(args)
  self.assertEqual(tomlkit.parse(self.codex.read_text())['model_reasoning_effort'],'ultra')
  self.assertIn('model_reasoning_effort="ultra"',launch.call_args.args[1])
 def test_force_switch_backs_up_credential_drift_and_keeps_profile(self):
  self.use('aster');profile=self.m.profile_path('aster','codex').read_bytes()
  d=tomlkit.parse(self.codex.read_text());d['model_providers']['aster']['experimental_bearer_token']='UNSAVED_SECRET';self.codex.write_text(tomlkit.dumps(d))
  output=self.cli('use','micu',expected=1)
  self.assertIn('model_providers.aster.experimental_bearer_token',output);self.assertIn('--discard-changes',output);self.assertNotIn('UNSAVED_SECRET',output)
  self.cli('use','micu','--discard-changes','--dry-run');self.assertEqual(self.m.load()['active']['codex'],'aster')
  self.cli('use','micu','--discard-changes')
  self.assertEqual(self.m.profile_path('aster','codex').read_bytes(),profile);self.assertEqual(self.codex.read_bytes(),self.original[self.codex])
 def test_effort_plus_route_change_still_requires_explicit_choice(self):
  self.use('aster');d=tomlkit.parse(self.codex.read_text());d['model_reasoning_effort']='xhigh';d['model_providers']['aster']['base_url']='https://other.example/v1';self.codex.write_text(tomlkit.dumps(d))
  self.assertFalse(self.m.report()['apps']['codex']['effort_only']);self.cli('use','micu',expected=1)
 def test_force_switch_failure_rolls_back_both_clients_and_unsaved_changes(self):
  self.use('aster');d=tomlkit.parse(self.codex.read_text());d['model']='unsaved-model';self.codex.write_text(tomlkit.dumps(d))
  before={p:p.read_bytes() for p in [self.codex,self.claude,self.m.state_path]};real=s.atomic_write;failed=False
  def fail_once(path,*args,**kwargs):
   nonlocal failed
   if path==self.codex and not failed:failed=True;raise OSError('fixture failure')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_once):self.cli('use','micu','--discard-changes',expected=1)
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)


 def baseline_files(self):
  return {p:p.read_bytes() for p in (self.m.root/'baseline').iterdir() if p.is_file()}
 def legacy_baseline(self):
  for name in ['original-profiles.json','original-manifest.json']:(self.m.root/'baseline'/name).unlink()
 def test_baseline_protected_from_edit_capture_and_delete(self):
  baseline=self.baseline_files()
  self.cli('profile','edit','micu','--app','codex','--model','gpt-new','--api-key-env','ASTERGATE_API_KEY')
  self.cli('capture');self.use('aster');self.cli('profile','delete','micu')
  for p,data in baseline.items():self.assertEqual(p.read_bytes(),data)
  self.cli('baseline','status');self.cli('baseline','protect')
  self.cli('baseline','restore')
  self.assertEqual(self.m.profile('micu','codex')['values']['model'],'gpt-6-astra')
  self.assertEqual(self.m.profile('micu','codex')['launch_env']['MICU_FIXTURE_KEY'],'MICU_SECRET')
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
 def test_baseline_restore_preserves_common_settings_and_history(self):
  self.cli('profile','edit','micu','--app','codex','--model','gpt-changed');self.use('aster')
  d=tomlkit.parse(self.codex.read_text());d['mcp_servers']['new']={'command':'new'};d['model']='unsaved';self.codex.write_text(tomlkit.dumps(d))
  c=json.loads(self.claude.read_text());c['statusLine']={'type':'command','command':'new'};self.claude.write_text(json.dumps(c))
  history=self.root/'codex/history.jsonl';history.write_text('keep-history')
  self.cli('baseline','restore')
  d=tomlkit.parse(self.codex.read_text());c=json.loads(self.claude.read_text())
  self.assertEqual(d['model_provider'],'micu');self.assertEqual(d['model'],'gpt-6-astra');self.assertIn('new',d['mcp_servers']);self.assertEqual(c['statusLine']['command'],'new')
  self.assertNotIn('model_catalog_json',d);self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL',c['env']);self.assertEqual(history.read_text(),'keep-history')
  self.assertTrue(all(v['matches_profile'] for v in self.m.report()['apps'].values()))
 def test_baseline_full_restore_recovers_invalid_config_and_auth(self):
  self.legacy_baseline();auth=self.m.root/'baseline/codex-auth.json';auth.write_text('{"OPENAI_API_KEY":"INITIAL_FIXTURE"}')
  self.cli('baseline','protect');self.codex.with_name('auth.json').write_text('{"OPENAI_API_KEY":"OTHER_FIXTURE"}')
  self.use('aster');self.codex.write_text('invalid=');self.claude.write_text('{broken')
  self.cli('baseline','restore','--full-config')
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
  self.assertEqual(self.codex.with_name('auth.json').read_bytes(),auth.read_bytes())
  self.assertEqual(self.m.load()['active'],dict(claude='micu',codex='micu'))
 def test_baseline_dry_run_and_single_client_restore(self):
  self.use('aster');before={p:p.read_bytes() for p in [self.claude,self.codex,self.m.state_path,*self.baseline_files()]}
  self.cli('baseline','restore','--full-config','--dry-run')
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)
  self.cli('baseline','restore','--app','codex')
  self.assertEqual(self.m.load()['active'],dict(claude='aster',codex='micu'))
  self.assertEqual(self.claude.read_bytes(),before[self.claude]);self.assertEqual(self.codex.read_bytes(),self.original[self.codex])
 def test_baseline_tampering_blocks_restore(self):
  (self.m.root/'baseline/codex.toml').write_text('model="tampered"')
  self.cli('baseline','status',expected=1);self.cli('baseline','restore',expected=1)
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
 def test_baseline_rejects_unverified_added_auth(self):
  (self.m.root/'baseline/codex-auth.json').write_text('{}')
  self.cli('baseline','restore','--full-config',expected=1)
  self.assertFalse(self.codex.with_name('auth.json').exists())
 def test_baseline_failure_rolls_back_profiles_and_clients(self):
  self.cli('profile','edit','micu','--app','codex','--model','gpt-modified');self.use('aster')
  paths=[self.codex,self.claude,self.m.state_path,*((self.m.root/'profiles').glob('*/*.json'))]
  before={p:p.read_bytes() for p in paths};real=s.atomic_write;failed=False
  def fail_once(path,*args,**kwargs):
   nonlocal failed
   if path==self.codex and not failed:failed=True;raise OSError('fixture failure')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_once):self.cli('baseline','restore',expected=1)
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)
  self.assertFalse((self.m.root/'pending.json').exists())
 def test_legacy_baseline_protection_is_once_only(self):
  self.legacy_baseline();before={p:p.read_bytes() for p in [self.claude,self.codex]}
  self.cli('baseline','protect');baseline=self.baseline_files();self.cli('baseline','protect')
  for p,data in {**before,**baseline}.items():self.assertEqual(p.read_bytes(),data)
  self.cli('baseline','status')
 def test_legacy_baseline_refuses_changed_profile(self):
  self.legacy_baseline();self.cli('profile','edit','micu','--app','codex','--model','changed')
  self.cli('baseline','protect',expected=1)
  self.assertFalse((self.m.root/'baseline/original-manifest.json').exists())
  self.assertEqual((self.m.root/'baseline/codex.toml').read_bytes(),self.original[self.codex])
 def test_baseline_and_discard_have_different_targets(self):
  self.cli('profile','edit','micu','--app','codex','--model','gpt-changed');self.use('aster')
  self.cli('use','micu','--discard-changes');self.assertEqual(tomlkit.parse(self.codex.read_text())['model'],'gpt-changed')
  self.cli('baseline','restore');self.assertEqual(tomlkit.parse(self.codex.read_text())['model'],'gpt-6-astra')

 def remove_aster_baseline(self):
  for p in self.m.named_baseline_dir('aster').iterdir():p.unlink()
 def test_aster_baseline_automatic_and_independent_of_working_profile(self):
  directory=self.m.named_baseline_dir('aster');before={p:p.read_bytes() for p in directory.iterdir()}
  self.cli('profile','edit','aster','--app','codex','--effort','high','--api-key-env','MICU_FIXTURE_KEY')
  self.use('aster');self.cli('capture');self.use('micu');self.cli('profile','delete','aster')
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)
  self.assertIn('aster',self.cli('baseline','list'));self.cli('baseline','status','aster')
  self.cli('baseline','restore','aster')
  p=self.m.profile('aster','codex')
  self.assertEqual(p['values']['model_reasoning_effort'],'ultra');self.assertEqual(p['providers']['aster']['experimental_bearer_token'],'ASTER_SECRET')
  self.assertEqual(self.m.load()['active'],dict(claude='aster',codex='aster'))
  self.assertTrue(all(v['matches_profile'] for v in self.m.report()['apps'].values()))
  self.cli('baseline','restore','micu')
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
 def test_aster_baseline_restores_resources_preserves_common_settings_and_history(self):
  assets={p:p.read_bytes() for p in (self.m.root/'assets').iterdir()}
  for p in assets:p.write_bytes(b'modified-resource')
  (self.m.root/'assets/model-catalog.json').unlink()
  c=json.loads(self.claude.read_text());c['statusLine']={'command':'new'};self.claude.write_text(json.dumps(c))
  d=tomlkit.parse(self.codex.read_text());d['mcp_servers']['new']={'command':'new'};self.codex.write_text(tomlkit.dumps(d))
  history=self.root/'codex/history.jsonl';history.write_text('keep')
  self.cli('baseline','restore','aster')
  for p,data in assets.items():self.assertEqual(p.read_bytes(),data)
  self.assertIn('new',tomlkit.parse(self.codex.read_text())['mcp_servers']);self.assertIn('statusLine',json.loads(self.claude.read_text()))
  self.assertEqual(history.read_text(),'keep')
 def test_aster_baseline_full_restore_recovers_broken_configs(self):
  original=self.m.named_baseline('aster')
  self.codex.write_text('broken=');self.claude.write_text('{broken')
  auth=self.codex.with_name('auth.json');auth.write_text('current-auth')
  self.cli('baseline','restore','aster','--full-config')
  for app,path in [('claude',self.claude),('codex',self.codex)]:
   self.assertEqual(path.read_bytes(),s.base64.b64decode(original['configs'][app]))
  self.assertEqual(auth.read_text(),'current-auth')
 def test_aster_baseline_dry_run_single_app_and_idempotent_protect(self):
  paths=[self.claude,self.codex,self.m.state_path,*self.m.named_baseline_dir('aster').iterdir()]
  before={p:p.read_bytes() for p in paths}
  self.cli('baseline','restore','aster','--dry-run');self.cli('baseline','protect','aster')
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)
  self.cli('baseline','restore','aster','--app','codex')
  self.assertEqual(self.claude.read_bytes(),before[self.claude]);self.assertEqual(self.m.load()['active'],dict(claude='micu',codex='aster'))
 def test_named_baseline_custom_profile_pins_saved_not_unsaved_values(self):
  self.cli('profile','add','backup','--from','micu');self.use('backup')
  d=tomlkit.parse(self.codex.read_text());d['model']='unsaved';self.codex.write_text(tomlkit.dumps(d))
  live={p:p.read_bytes() for p in (self.claude,self.codex)}
  self.cli('baseline','protect','backup')
  for p,data in live.items():self.assertEqual(p.read_bytes(),data)
  self.cli('use','micu','--discard-changes');self.cli('profile','delete','backup')
  self.cli('baseline','restore','backup')
  self.assertEqual(tomlkit.parse(self.codex.read_text())['model'],'gpt-6-astra')
  self.assertEqual(tomlkit.parse(self.codex.read_text())['model_provider'],'ai_switch_backup')
 def test_legacy_aster_baseline_from_backup_pins_before_capture(self):
  self.remove_aster_baseline();self.use('aster')
  d=tomlkit.parse(self.codex.read_text());d['model_reasoning_effort']='xhigh';self.codex.write_text(tomlkit.dumps(d))
  self.cli('capture');backup=self.m.load()['last_backup']
  before={p:p.read_bytes() for p in [self.claude,self.codex,*((self.m.root/'profiles').glob('*/*.json'))]}
  self.cli('baseline','protect','aster','--from-backup',backup)
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)
  self.assertEqual(self.m.named_baseline('aster')['profiles']['codex']['values']['model_reasoning_effort'],'ultra')
  self.assertEqual(self.m.profile('aster','codex')['values']['model_reasoning_effort'],'xhigh')
  self.cli('baseline','restore','aster');self.assertEqual(tomlkit.parse(self.codex.read_text())['model_reasoning_effort'],'ultra')
 def test_legacy_aster_baseline_rejects_incomplete_backup_and_changed_assets(self):
  self.remove_aster_baseline();self.cli('profile','edit','aster','--app','codex','--effort','high')
  self.cli('baseline','protect','aster','--from-backup',self.m.load()['last_backup'],expected=1)
  self.use('aster');self.cli('capture');backup=self.m.load()['last_backup']
  (self.m.root/'assets/model-catalog.json').write_text('{}')
  self.cli('baseline','protect','aster','--from-backup',backup,expected=1)
  self.assertFalse((self.m.named_baseline_dir('aster')/'manifest.json').exists())
 def test_named_baseline_tampering_and_resource_symlink_block_restore(self):
  path=self.m.named_baseline_dir('aster')/'snapshot.json';original=path.read_bytes();path.write_bytes(original+b' ')
  self.cli('baseline','status','aster',expected=1);self.cli('baseline','restore','aster',expected=1)
  path.write_bytes(original)
  asset=self.m.root/'assets/model-catalog.json';asset.unlink();asset.symlink_to(self.root/'catalog.json')
  self.cli('baseline','restore','aster',expected=1)
  for p,data in self.original.items():self.assertEqual(p.read_bytes(),data)
 def test_named_baseline_restore_failure_rolls_back_configs_profiles_and_resources(self):
  self.cli('profile','edit','aster','--app','codex','--effort','high')
  (self.m.root/'assets/read_guard.py').write_bytes(b'changed')
  paths=[self.claude,self.codex,self.m.state_path,*((self.m.root/'profiles').glob('*/*.json')),*((self.m.root/'assets').iterdir())]
  before={p:p.read_bytes() for p in paths};real=s.atomic_write;failed=False
  def fail_once(path,*args,**kwargs):
   nonlocal failed
   if path==self.codex and not failed:failed=True;raise OSError('fixture failure')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_once):self.cli('baseline','restore','aster',expected=1)
  for p,data in before.items():self.assertEqual(p.read_bytes(),data)
  self.assertFalse((self.m.root/'pending.json').exists())
 def test_named_baseline_protect_failure_rolls_back_and_can_retry(self):
  self.cli('profile','add','backup','--from','micu');state=self.m.state_path.read_bytes();real=s.atomic_write;failed=False
  def fail_once(path,*args,**kwargs):
   nonlocal failed
   if path==self.m.named_baseline_dir('backup')/'manifest.json' and not failed:failed=True;raise OSError('fixture failure')
   return real(path,*args,**kwargs)
  with patch.object(s,'atomic_write',side_effect=fail_once):self.cli('baseline','protect','backup',expected=1)
  self.assertEqual(self.m.state_path.read_bytes(),state)
  self.assertFalse((self.m.named_baseline_dir('backup')/'snapshot.json').exists())
  self.cli('baseline','protect','backup');self.cli('baseline','status','backup')


class HookTests(unittest.TestCase):
 def test_gemini_bounds_only_preserves_other_fields(self):
  with tempfile.NamedTemporaryFile(mode='w') as file:
   file.write('one\ntwo\nthree\n');file.flush();p={'model':'gemini-3.8-flash-high','tool_name':'Read','hook_event_name':'PreToolUse','tool_input':{'file_path':file.name,'offset':900,'limit':2,'pages':'1'}}
   with patch.dict(os.environ,{'AI_SWITCH_MODE':'aster'}):
    out=read_guard.handle(p)['hookSpecificOutput']['updatedInput'];self.assertEqual(out,dict(p['tool_input'],offset=2))
    p['tool_input']['offset']=1;self.assertIsNone(read_guard.handle(p));p['model']='claude-fable-5-1';p['tool_input']['offset']=900;self.assertIsNone(read_guard.handle(p))
 def test_inherited_subagent_default_is_not_current_model(self):
  with patch.dict(os.environ,{'CLAUDE_CODE_SUBAGENT_MODEL':'gemini-3.8-flash-high','AI_SWITCH_MODE':'aster'}):self.assertIsNone(read_guard.handle({'tool_name':'Read','hook_event_name':'PostToolUse','tool_input':{'limit':20}}))
 def test_model_from_transcript_and_micu_disabled(self):
  with tempfile.NamedTemporaryFile(mode='w') as file:
   file.write(json.dumps({'type':'assistant','message':{'model':'gemini-3.8-flash-high'}})+'\n');file.flush();p={'transcript_path':file.name,'tool_name':'Read','hook_event_name':'PostToolUse','tool_input':{'limit':20}}
   with patch.dict(os.environ,{'AI_SWITCH_MODE':'aster'}):self.assertIn('additionalContext',read_guard.handle(p)['hookSpecificOutput'])
   with patch.dict(os.environ,{'AI_SWITCH_MODE':'micu'}):self.assertIsNone(read_guard.handle(p))
if __name__=='__main__':unittest.main()
