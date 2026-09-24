import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import session_process_portable as process

HOLDER = '''import sys,time,platform_io
files=[]
for path in sys.argv[1:]:
 f=open(path,'a+b');platform_io.file_lock(f.fileno());files.append(f)
print('ready',flush=True)
while True:time.sleep(.1)
'''


@unittest.skipUnless(sys.platform == 'darwin' or os.name == 'nt', 'Native macOS/Windows takeover')
class PortableProcessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ai-switch-takeover-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        (self.home/'thread-writer-locks').mkdir()
        self.session = str(uuid.uuid4())
        self.path = self.home/'thread-writer-locks'/(self.session+'.lock')

    def holder(self, paths=None):
        child = subprocess.Popen([sys.executable, '-u', '-c', HOLDER, *map(str, paths or [self.path])],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=dict(os.environ, CODEX_HOME=str(self.home)))
        def cleanup():
            if child.poll() is None: child.kill()
            child.wait(timeout=5);child.stdout.close();child.stderr.close()
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        return child

    @contextlib.contextmanager
    def native(self, child):
        actual = process.identity
        def identity(pid):
            result = actual(pid)
            if pid == child.pid:
                result['executable'] = str(self.home/('codex.exe' if os.name == 'nt' else 'codex'))
                result['arguments'] = ['codex', 'resume', self.session]
            return result
        with patch.object(process, 'identity', side_effect=identity):
            yield

    def test_free_and_stale_locks_are_not_removed(self):
        self.assertFalse(process.ensure_available(self.home, self.session, takeover=True))
        self.path.touch()
        self.assertFalse(process.ensure_available(self.home, self.session, takeover=True))
        self.assertTrue(self.path.exists())

    def test_default_and_dry_run_do_not_terminate(self):
        child = self.holder()
        self.assertEqual(process.writer(self.home, self.session).pid, child.pid)
        with self.assertRaisesRegex(process.TakeoverError, '--takeover'):
            process.ensure_available(self.home, self.session)
        with self.native(child), patch.object(process, 'terminate', side_effect=AssertionError('no termination')):
            self.assertTrue(process.ensure_available(self.home, self.session, takeover=True, dry_run=True, emit=lambda _:None))
        self.assertIsNone(child.poll())

    def test_takeover_releases_only_target_and_preserves_lock_file(self):
        child = self.holder()
        other_path = self.path.with_name(str(uuid.uuid4())+'.lock')
        other = self.holder([other_path])
        with self.native(child):
            self.assertFalse(process.ensure_available(self.home, self.session, takeover=True, timeout=10, emit=lambda _:None))
        child.wait(timeout=5)
        self.assertIsNone(other.poll())
        self.assertTrue(self.path.exists())
        self.assertIsNone(process.writer(self.home, self.session))

    def test_non_client_and_multi_session_processes_are_rejected(self):
        child = self.holder([self.path, self.path.with_name(str(uuid.uuid4())+'.lock')])
        with self.assertRaisesRegex(process.TakeoverError, '不是当前用户'):
            process.ensure_available(self.home, self.session, takeover=True)
        with self.native(child), self.assertRaisesRegex(process.TakeoverError, '其他会话'):
            process.ensure_available(self.home, self.session, takeover=True)
        self.assertIsNone(child.poll())

    def test_ambiguous_owner_and_identity_race_are_rejected(self):
        child = self.holder()
        with patch.object(process, 'file_users', return_value={child.pid, child.pid+100000}):
            with self.assertRaisesRegex(process.TakeoverError, '无法唯一'):
                process.ensure_available(self.home, self.session, takeover=True)
        actual = process.identity(child.pid)
        with self.native(child), patch.object(process, 'writer', side_effect=[process.writer(self.home,self.session), None]):
            with self.assertRaisesRegex(process.TakeoverError, '已变化'):
                process.ensure_available(self.home,self.session,takeover=True)
        self.assertIsNone(child.poll())


if __name__ == '__main__':unittest.main()
