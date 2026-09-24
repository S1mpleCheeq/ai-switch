import gzip, json, os, tempfile, threading, subprocess, time, signal, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse, contextlib, io, ssl, sys
from unittest.mock import patch
import ai_switch as switch
import platform_runtime

parser=argparse.ArgumentParser(description="Exercise installed native clients against local fake APIs; no real credentials.")
parser.add_argument("--catalog",type=Path)
args=parser.parse_args()
os.umask(0o077)
root=Path(tempfile.mkdtemp(prefix='ai-switch-native-')).resolve()
if args.catalog is None:
 args.catalog=root/'fixture-catalog.json'
 model=dict(display_name='Local fixture',description='Synthetic integration test model',default_reasoning_level='high',supported_reasoning_levels=[dict(effort=e,description=e) for e in ('low','medium','high','xhigh')],shell_type='unified_exec',visibility='list',supported_in_api=True,priority=1,base_instructions='Reply briefly. Do not use tools.',supports_reasoning_summaries=True,support_verbosity=True,default_verbosity='low',apply_patch_tool_type='freeform',truncation_policy=dict(mode='tokens',limit=10000),context_window=272000,effective_context_window_percent=95,experimental_supported_tools=[],input_modalities=['text'])
 args.catalog.write_text(json.dumps(dict(models=[dict(model,slug=slug) for slug in ('gpt-6-astra','gemini-3.8-flash-high')])),encoding='utf-8')
requests=[]
hold_next=threading.Event();hold_started=threading.Event();hold_release=threading.Event()
class H(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"data":[]}')
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
  if self.headers.get('Content-Encoding')=='gzip':raw=gzip.decompress(raw)
  if self.headers.get('Content-Encoding')=='zstd':
   import zstandard
   raw=zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
  d=json.loads(raw or '{}');requests.append((self.path,d))
  if '/responses' in self.path and hold_next.is_set():
   hold_next.clear();hold_started.set();hold_release.wait(40);return
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
  def emit(t,x):
   self.wfile.write(('event: '+t+'\ndata: '+json.dumps(x)+'\n\n').encode())
  if '/responses' in self.path:
   msg={'id':'msg_test','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'fixture response','annotations':[]}]}
   response={'id':'resp_'+uuid.uuid4().hex,'object':'response','created_at':int(time.time()),'status':'in_progress','model':d['model'],'output':[]}
   emit('response.created',{'type':'response.created','response':response})
   emit('response.output_item.added',{'type':'response.output_item.added','output_index':0,'item':dict(msg,status='in_progress',content=[])})
   emit('response.content_part.added',{'type':'response.content_part.added','item_id':'msg_test','output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}})
   emit('response.output_text.delta',{'type':'response.output_text.delta','item_id':'msg_test','output_index':0,'content_index':0,'delta':'fixture response'})
   emit('response.output_item.done',{'type':'response.output_item.done','output_index':0,'item':msg})
   emit('response.completed',{'type':'response.completed','response':dict(response,status='completed',output=[msg],usage={'input_tokens':10,'output_tokens':2,'total_tokens':12})})
  else:
   emit('message_start',{'type':'message_start','message':{'id':'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant','content':[],'model':d.get('model'),'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}})
   emit('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})
   emit('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'fixture response'}})
   emit('content_block_stop',{'type':'content_block_stop','index':0})
   emit('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':2}})
   emit('message_stop',{'type':'message_stop'})
  self.wfile.flush()
server=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
base=f'http://127.0.0.1:{server.server_port}'
env={k:v for k,v in os.environ.items() if k.upper() in ('PATH','LANG','TERM','SHELL','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA','CLAUDE_CODE_GIT_BASH_PATH')}
env.update(HOME=str(root),USERPROFILE=str(root),APPDATA=str(root/'appdata'),LOCALAPPDATA=str(root/'localappdata'),PYTHONUTF8='1',CODEX_HOME=str(root/'codex'),CLAUDE_CONFIG_DIR=str(root/'claude'),ANTHROPIC_AUTH_TOKEN='fixture',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',DISABLE_AUTOUPDATER='1')
(root/'codex').mkdir();(root/'claude').mkdir()
(root/'codex/config.toml').write_text('model="gpt-6-astra"\nmodel_provider="micu"\nmodel_reasoning_effort="xhigh"\n[model_providers.micu]\nname="Micu"\nbase_url="'+base+'/micu/v1"\nwire_api="responses"\nexperimental_bearer_token="fixture"\n')
(root/'claude/settings.json').write_text(json.dumps({'model':'sonnet','effortLevel':'high','env':{'ANTHROPIC_BASE_URL':'https://micu.example','ANTHROPIC_AUTH_TOKEN':'fixture','ANTHROPIC_DEFAULT_SONNET_MODEL':'claude-sonnet-5'}}))
(root/'ca.crt').write_text(ssl.DER_cert_to_PEM_cert(ssl.create_default_context().get_ca_certs(binary_form=True)[0]))
manager=switch.Manager(root/'switch')
with patch.dict(os.environ,dict(env, ASTERGATE_API_KEY='fixture'),clear=True):
 manager.init(argparse.Namespace(claude_dir=str(root/'claude'),codex_dir=str(root/'codex'),catalog=str(args.catalog),ca=str(root/'ca.crt'),aster_key_env='ASTERGATE_API_KEY'))
# Replace both isolated profiles with local fixture endpoints, then render current Micu.
for mode in switch.MODES:
 for app in switch.APPS:
  profile=manager.profile(mode,app)
  if app=='codex':profile['providers'][mode]['base_url']=base+'/'+mode+'/v1'
  else:profile['env']['ANTHROPIC_BASE_URL']=base+'/'+mode
  switch.atomic_write(manager.root/'profiles'/mode/(app+'.json'),switch.json_bytes(profile))
state=manager.load()
for app in switch.APPS:
 _,config=manager.read_config(state,app)
 switch.atomic_write(Path(state['paths'][app]),manager.render(app,config,manager.profile('micu',app),'micu'))
manager.add_profile('backup-micu','micu')
manager.add_profile('backup-aster','aster')

def client_command(app,mode,sid=None):
 with patch.dict(os.environ,env,clear=True),patch.object(switch.platform_runtime,'launch',return_value=0) as launch,patch.object(switch.os,'chdir'),contextlib.redirect_stdout(io.StringIO()):
  manager.launch(argparse.Namespace(app=app,mode=mode,session=sid,cwd=str(root),dry_run=False,client_args=[]))
 cmd,child_env=launch.call_args.args
 return platform_runtime.native_command(cmd),child_env

def run(label,cmd,extra={}):
 p=subprocess.Popen(cmd,env=extra or env,cwd=root,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=os.name != "nt")
 try:out,err=p.communicate(timeout=45)
 except subprocess.TimeoutExpired:
  if os.name == 'nt':
   import psutil
   for child in psutil.Process(p.pid).children(recursive=True): child.kill()
   p.kill()
  else:os.killpg(p.pid,signal.SIGKILL)
  out,err=p.communicate();raise RuntimeError(label+' timed out '+err[-1500:])
 (root/(label+'.out')).write_text(out);(root/(label+'.err')).write_text(err)
 print(label,'exit',p.returncode,'requests',[(path,d.get('model')) for path,d in requests],flush=True)
 if p.returncode:raise RuntimeError(label+' failed: '+err[-2000:]+out[-1000:])
 return out
try:
 cmd,child_env=client_command('codex','aster')
 out=run('codex-new',cmd+['exec','--skip-git-repo-check','--json','REMEMBER fixture-7'],child_env)
 sid=next(json.loads(l)['thread_id'] for l in out.splitlines() if json.loads(l).get('type')=='thread.started')
 for mode in ['micu','aster','backup-micu','backup-aster']:
  route=mode.removeprefix('backup-')
  before=len(requests);cmd,child_env=client_command('codex',mode,sid)
  # The interactive resume flags are identical; exec makes the fixture noninteractive.
  cmd.insert(cmd.index('resume'),'exec')
  out=run('codex-'+mode,cmd+['--skip-git-repo-check','--json','CONTINUE fixture-7'],child_env)
  req=requests[before:];assert any('/'+route+'/' in p for p,d in req)
  assert any('REMEMBER fixture-7' in json.dumps(d) for p,d in req)
  # Ultra is a client orchestration mode: native Codex sends xhigh to the API.
  assert all(d.get('reasoning',{}).get('effort')=='xhigh' for p,d in req if '/responses' in p)
  assert manager.profile(mode,'codex')['values']['model_reasoning_effort']==('xhigh' if route=='micu' else 'ultra')
 print('codex full-profile resume both directions retained history',flush=True)
 # Hold one request from an actual native Codex process in this isolated home.
 # Verify takeover against its real lock, never a user session or remote API.
 cmd,child_env=client_command('codex','micu',sid)
 cmd.insert(cmd.index('resume'),'exec')
 hold_next.set()
 child=subprocess.Popen(cmd+['--skip-git-repo-check','--json','WAIT for local takeover fixture'],
                        env=child_env,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        start_new_session=os.name != 'nt')
 try:
  assert hold_started.wait(30), 'Native takeover fixture did not reach local API'
  assert switch.session_process.ensure_available(root/'codex',sid,takeover=True,dry_run=True)
  assert child.poll() is None, 'Dry run terminated the native client'
  assert not switch.session_process.ensure_available(root/'codex',sid,takeover=True,timeout=15)
  child.communicate(timeout=15)
 finally:
  hold_release.set()
  if child.poll() is None:
   import psutil
   for item in psutil.Process(child.pid).children(recursive=True): item.kill()
   child.kill();child.communicate(timeout=5)
 cmd,child_env=client_command('codex','aster',sid)
 cmd.insert(cmd.index('resume'),'exec')
 before=len(requests)
 run('codex-after-takeover',cmd+['--skip-git-repo-check','--json','CONTINUE fixture-7 after takeover'],child_env)
 assert any('REMEMBER fixture-7' in json.dumps(d) for p,d in requests[before:])
 print('codex native takeover and same-UUID continuation passed',flush=True)

 sid=str(uuid.uuid4())
 for i,mode in enumerate(['aster','micu','aster','backup-micu','backup-aster']):
  route=mode.removeprefix('backup-')
  cmd,child_env=client_command('claude',mode,sid if i else None)
  cmd+=['--output-format','json','-p','REMEMBER fixture-9' if i==0 else 'CONTINUE fixture-9']
  if not i:cmd+=['--session-id',sid]
  before=len(requests);out=run('claude-'+str(i),cmd,child_env)
  req=[(p,d) for p,d in requests[before:] if '/messages' in p];assert any('/'+route+'/' in p for p,d in req)
  expected='claude-fable-5-1' if route=='aster' else 'claude-sonnet-5'
  assert all(d['model']==expected for p,d in req)
  if i:assert any('REMEMBER fixture-9' in json.dumps(d) for p,d in req)
  if route=='micu':
   assert 'CLAUDE_CODE_SUBAGENT_MODEL' not in child_env
   assert manager.profile(mode,'claude')['values'].get('ultracode') is None
 print('claude full-profile resume both directions retained history',flush=True)
finally:
 (root/'requests.json').write_text(json.dumps(requests));print('artifacts',root,flush=True);server.shutdown()
