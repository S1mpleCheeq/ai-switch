import json
import os
from pathlib import Path
import subprocess
import sys
import shlex
import tempfile
import unittest
from unittest.mock import patch

import platform_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_native_argument_passthrough_has_no_shell_expansion(self):
        arguments = ['a b', '中文', '%PATH%', '$(echo BAD)', 'x&echo BAD', '"quoted"']
        command = runtime.native_command([sys.executable, '-c', 'import sys,json;print(json.dumps(sys.argv[1:]))', *arguments])
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_default_editor(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime.editor_command(), ['notepad.exe' if os.name == 'nt' else 'vi'])

    def test_editor_full_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='editor space-') as directory:
            path = Path(directory)/'editor.exe'
            path.touch()
            with patch.dict(os.environ, {'VISUAL': str(path)}):
                self.assertEqual(runtime.editor_command(), [str(path)])

    def test_hook_recognition_preserves_unrelated_and_shell_commands(self):
        script = Path.cwd()/'fixture guard.py'
        self.assertTrue(runtime.is_managed_hook(shlex.join(['python3.10','-X','utf8',str(script)]), script))
        self.assertFalse(runtime.is_managed_hook(shlex.join(['python3',str(script.with_name('unrelated.py'))]), script))
        self.assertFalse(runtime.is_managed_hook('echo hello && '+shlex.join(['python3',str(script)]), script))
        self.assertFalse(runtime.is_managed_hook('invalid "quote', script))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows npm shims')
    def test_npm_shim_is_resolved_without_cmd_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root/'codex.cmd'; shim.touch()
            entry = root/'node_modules/@openai/codex/bin/codex.js'
            entry.parent.mkdir(parents=True); entry.touch()
            (root/'node.exe').touch()
            self.assertEqual(runtime.native_command([str(shim), 'resume', 'a&b']),
                             [str(root/'node.exe'), str(entry), 'resume', 'a&b'])
            with self.assertRaisesRegex(OSError, '无法安全解析'):
                runtime.native_command([str(root/'unknown.cmd')])
            claude = root/'node_modules/@anthropic-ai/claude-code'
            (claude/'bin').mkdir(parents=True)
            (claude/'bin/claude.exe').touch()
            (claude/'package.json').write_text(json.dumps({'bin': {'claude': 'bin/claude.exe'}}))
            self.assertEqual(runtime.native_command([str(root/'claude.cmd'), 'a&b']),
                             [str(claude/'bin/claude.exe'), 'a&b'])


if __name__ == '__main__':
    unittest.main()
