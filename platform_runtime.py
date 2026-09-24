"""Native command launch and quoting without passing user arguments to a shell."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def native_command(command):
    command = list(map(str, command))
    if os.name == 'nt' and Path(command[0]).suffix.lower() in ('.cmd', '.bat'):
        # npm creates shell shims. Resolve their JS entry point and invoke Node
        # directly; arbitrary shell arguments must never be expanded by cmd.exe.
        folder = Path(command[0]).parent
        packages = {'codex': ('@openai/codex', 'bin/codex.js'),
                    'claude': ('@anthropic-ai/claude-code', 'cli.js')}
        name = Path(command[0]).stem.lower()
        if name in packages:
            package, entry = packages[name]
            script = folder/'node_modules'/package/entry
            node = folder/'node.exe'
            executable = str(node) if node.is_file() else shutil.which('node')
            if script.is_file() and executable:
                return [executable, str(script), *command[1:]]
        raise OSError('无法安全解析客户端 .cmd 启动器；请安装原生可执行文件或标准 npm 客户端。')
    return command


def launch(command, env):
    command = native_command(command)
    if os.name != 'nt':
        os.execvpe(command[0], command, env)
        return
    child = subprocess.Popen(command, env=env)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Both processes share the console; the native client also receives
            # Ctrl+C. Let it flush history and release its writer lock.
            continue


def display_command(command):
    return subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command)


def hook_command(path):
    # Claude command hooks use a POSIX shell (Git Bash on native Windows).
    # Forward slashes avoid backslash interpretation in Git Bash paths.
    python = Path(sys.executable).as_posix()
    return shlex.join([python, '-X', 'utf8', Path(path).as_posix()])


def editor_command():
    value = os.environ.get('VISUAL') or os.environ.get('EDITOR')
    if not value:
        return ['notepad.exe' if os.name == 'nt' else 'vi']
    if Path(value).is_file():
        return [value]
    if os.name != 'nt':
        return shlex.split(value)
    # Parse the Windows command-line grammar, not Unix shlex escaping.
    import ctypes
    from ctypes import wintypes as w
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
    kernel.LocalFree.argtypes = [w.HLOCAL]
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(value, ctypes.byref(count))
    if not pointer:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(pointer)
