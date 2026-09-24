import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import session_process as process


HOLDER = '''
import fcntl,signal,sys
files=[]
if sys.argv[1]=='ignore':signal.signal(signal.SIGTERM,signal.SIG_IGN)
for path in sys.argv[2:]:
    stream=open(path,'a');fcntl.flock(stream,fcntl.LOCK_EX);files.append(stream)
print('ready',flush=True)
while True:signal.pause()
'''


@unittest.skipUnless(sys.platform == 'linux' and hasattr(signal, 'pidfd_send_signal'), 'Linux pidfd fixture required')
class ProcessTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='ai-switch-process-test-')
        self.addCleanup(tmp.cleanup)
        self.home=Path(tmp.name).resolve();(self.home/'thread-writer-locks').mkdir()
        self.session=str(uuid.uuid4())
        self.path=self.home/'thread-writer-locks'/(self.session+'.lock')

    def holder(self, paths=None, ignore=False):
        paths=paths or [self.path]
        child=subprocess.Popen([sys.executable,'-u','-c',HOLDER,'ignore' if ignore else 'normal',
                                *map(str,paths)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if child.poll() is None:
                child.kill()  # Test fixture only; production never sends SIGKILL.
            child.wait(timeout=3);child.stdout.close();child.stderr.close()
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(),'ready')
        return child

    @contextlib.contextmanager
    def as_native(self, child, arguments=None):
        real=process.process_identity
        def identity(pid):
            value=real(pid)
            if pid==child.pid:
                value.update(executable='/fixture/codex',arguments=arguments or [b'codex',b'resume'])
            return value
        with patch.object(process,'process_identity',side_effect=identity):yield

    def test_missing_and_stale_unlocked_file_are_not_busy_or_deleted(self):
        self.assertIsNone(process.writer(self.home,self.session))
        self.path.write_bytes(b'')
        with patch.object(signal,'pidfd_send_signal',side_effect=AssertionError('no signal')):
            self.assertFalse(process.ensure_available(self.home,self.session,takeover=True))
        self.assertTrue(self.path.exists())

    def test_default_reports_real_owner_without_signals(self):
        child=self.holder()
        self.assertEqual(process.writer(self.home,self.session).pid,child.pid)
        with patch.object(signal,'pidfd_send_signal',side_effect=AssertionError('no signal')):
            with self.assertRaisesRegex(process.TakeoverError,'--takeover'):
                process.ensure_available(self.home,self.session)
        self.assertIsNone(child.poll())

    def test_dry_run_keeps_process_lock_and_inode(self):
        child=self.holder();inode=self.path.stat().st_ino;out=[]
        with self.as_native(child),patch.object(signal,'pidfd_send_signal',side_effect=AssertionError('no signal')):
            self.assertTrue(process.ensure_available(self.home,self.session,takeover=True,dry_run=True,emit=out.append))
        self.assertIsNone(child.poll());self.assertEqual(self.path.stat().st_ino,inode)
        self.assertEqual(process.writer(self.home,self.session).pid,child.pid)
        self.assertIn('未发送信号',''.join(out))

    def test_takeover_only_signals_target_and_waits_for_release(self):
        child=self.holder();other_path=self.path.with_name(str(uuid.uuid4())+'.lock');other=self.holder([other_path])
        inode=self.path.stat().st_ino
        with self.as_native(child),patch.object(signal,'pidfd_send_signal',wraps=signal.pidfd_send_signal) as send:
            self.assertFalse(process.ensure_available(self.home,self.session,takeover=True,timeout=3,emit=lambda _:None))
        child.wait(timeout=2)
        self.assertEqual(child.returncode,-signal.SIGTERM)
        self.assertEqual([call.args[1] for call in send.call_args_list],[signal.SIGTERM])
        self.assertIsNone(other.poll());self.assertIsNone(process.writer(self.home,self.session))
        self.assertEqual(self.path.stat().st_ino,inode)

    def test_timeout_never_force_kills_or_deletes_lock(self):
        child=self.holder(ignore=True)
        with self.as_native(child),patch.object(signal,'pidfd_send_signal',wraps=signal.pidfd_send_signal) as send:
            with self.assertRaisesRegex(process.TakeoverError,'超时'):
                process.ensure_available(self.home,self.session,takeover=True,timeout=.15,emit=lambda _:None)
        self.assertEqual([call.args[1] for call in send.call_args_list],[signal.SIGTERM])
        self.assertIsNone(child.poll());self.assertEqual(process.writer(self.home,self.session).pid,child.pid)

    def test_non_codex_and_shared_daemon_are_rejected(self):
        child=self.holder()
        with self.assertRaisesRegex(process.TakeoverError,'不是当前用户的原生 Codex'):
            process.ensure_available(self.home,self.session,takeover=True)
        with self.as_native(child,[b'codex',b'app-server']):
            with self.assertRaisesRegex(process.TakeoverError,'后台服务'):
                process.ensure_available(self.home,self.session,takeover=True)
        self.assertIsNone(child.poll())

    def test_multiple_sessions_in_same_process_are_rejected(self):
        other=self.path.with_name(str(uuid.uuid4())+'.lock');child=self.holder([self.path,other])
        with self.as_native(child):
            with self.assertRaisesRegex(process.TakeoverError,'其他会话'):
                process.ensure_available(self.home,self.session,takeover=True)
        self.assertIsNone(child.poll())

    def test_changed_lock_owner_and_pid_identity_are_not_signalled(self):
        child=self.holder();held=process.writer(self.home,self.session)
        changed=process.Writer(held.path,held.key,held.pid+10000)
        with self.as_native(child),patch.object(process,'writer',side_effect=[held,changed]):
            with patch.object(signal,'pidfd_send_signal',side_effect=AssertionError('no signal')):
                with self.assertRaisesRegex(process.TakeoverError,'占用者已变化'):
                    process.ensure_available(self.home,self.session,takeover=True)
        identity=process.process_identity(child.pid)
        with patch.object(process,'process_identity',side_effect=[identity,dict(identity,start='different')]):
            with self.assertRaisesRegex(process.TakeoverError,'身份已变化'):
                process.ensure_available(self.home,self.session,takeover=True)
        self.assertIsNone(child.poll())

    def test_self_takeover_is_rejected(self):
        import fcntl
        with self.path.open('w') as file:
            fcntl.flock(file,fcntl.LOCK_EX)
            held=process.writer(self.home,self.session)
            identity=process.process_identity(os.getpid());identity['executable']='/fixture/codex'
            with self.assertRaisesRegex(process.TakeoverError,'接管自己'):
                process.validate_owner(held,identity)

    def test_symlink_lock_and_unknown_kernel_owner_are_rejected(self):
        target=self.home/'target';target.write_text('')
        self.path.symlink_to(target)
        with self.assertRaisesRegex(process.TakeoverError,'符号链接'):
            process.ensure_available(self.home,self.session,takeover=True)
        self.path.unlink();child=self.holder()
        with patch.object(process,'kernel_locks',return_value=[]):
            with self.assertRaisesRegex(process.TakeoverError,'无法可靠识别'):
                process.ensure_available(self.home,self.session,takeover=True)
        self.assertIsNone(child.poll())

    def test_non_linux_dispatches_to_native_backend(self):
        import session_process_portable as portable
        with patch.object(process.sys,'platform','darwin'), patch.object(portable,'ensure_available',return_value=False) as native:
            self.assertFalse(process.ensure_available(self.home,self.session,takeover=True))
        native.assert_called_once()
        self.assertTrue(native.call_args.kwargs['takeover'])


if __name__=='__main__':unittest.main()
