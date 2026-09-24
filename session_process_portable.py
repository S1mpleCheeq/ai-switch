"""macOS/Windows Codex lock ownership and explicit single-process takeover."""
from __future__ import annotations

import contextlib
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

import platform_io


class TakeoverError(Exception):
    pass


@dataclass(frozen=True)
class Writer:
    path: Path
    key: tuple
    pid: int | None


def windows_file_users(path):
    """Restart Manager reports open-file users; accept only one verified user."""
    from ctypes import wintypes as w
    class Unique(ctypes.Structure):
        _fields_ = [('pid', w.DWORD), ('started', w.FILETIME)]
    class Info(ctypes.Structure):
        _fields_ = [('process', Unique), ('name', w.WCHAR * 256),
                    ('service', w.WCHAR * 64), ('kind', ctypes.c_int),
                    ('status', w.ULONG), ('session', w.DWORD), ('restartable', w.BOOL)]
    rm = ctypes.WinDLL('rstrtmgr', use_last_error=True)
    rm.RmStartSession.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    rm.RmRegisterResources.argtypes = [w.DWORD, w.UINT, ctypes.POINTER(w.LPCWSTR), w.UINT, ctypes.POINTER(Unique), w.UINT, ctypes.POINTER(w.LPCWSTR)]
    rm.RmGetList.argtypes = [w.DWORD, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT), ctypes.POINTER(Info), ctypes.POINTER(w.DWORD)]
    rm.RmEndSession.argtypes = [w.DWORD]
    handle, key = w.DWORD(), ctypes.create_unicode_buffer(33)
    error = rm.RmStartSession(ctypes.byref(handle), 0, key)
    if error:
        raise ctypes.WinError(error)
    try:
        files = (w.LPCWSTR * 1)(str(path))
        error = rm.RmRegisterResources(handle, 1, files, 0, None, 0, None)
        if error:
            raise ctypes.WinError(error)
        count, needed, reason = w.UINT(), w.UINT(), w.DWORD()
        for _ in range(5):
            items = (Info * count.value)() if count.value else None
            error = rm.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count), items, ctypes.byref(reason))
            if error == 234:
                count.value = needed.value
                continue
            if error:
                raise ctypes.WinError(error)
            return {items[i].process.pid for i in range(count.value)}
        raise TakeoverError('会话占用者持续变化，未结束进程。')
    finally:
        rm.RmEndSession(handle)


def file_users(path):
    if os.name == 'nt':
        return windows_file_users(path)
    # lsof obtains the macOS process file-descriptor table. Do not infer an
    # owner merely from a command line or the presence of an empty lock file.
    executable = shutil.which('lsof') or '/usr/sbin/lsof'
    result = subprocess.run([executable, '-nP', '-F', 'p', '--', str(path)],
                            capture_output=True, text=True, timeout=10)
    if result.returncode not in (0, 1):
        raise TakeoverError('无法读取系统文件占用信息，未结束进程。')
    return {int(line[1:]) for line in result.stdout.splitlines()
            if line.startswith('p') and line[1:].isdigit()}


def busy(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise TakeoverError('会话写锁经过符号链接，未结束进程。')
    try:
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            try:
                platform_io.file_lock(stream.fileno())
            except BlockingIOError:
                return (info.st_dev, info.st_ino)
            else:
                platform_io.file_lock(stream.fileno(), False)
                return None
    except FileNotFoundError:
        return None


def writer(home, session):
    path = Path(home).resolve()/'thread-writer-locks'/(str(uuid.UUID(session))+'.lock')
    key = busy(path)
    if key is None:
        return None
    owners = file_users(path)
    owners.discard(os.getpid())
    return Writer(path, key, next(iter(owners)) if len(owners) == 1 else None)


def identity(pid):
    import psutil
    proc = psutil.Process(pid)
    with proc.oneshot():
        return {'pid': pid, 'started': proc.create_time(), 'user': proc.username(),
                'executable': proc.exe(), 'arguments': proc.cmdline()}


def validate_owner(held, info, home):
    import psutil
    current = psutil.Process()
    if (info['user'] != current.username()
            or Path(info['executable']).name.lower() not in ('codex', 'codex.exe')):
        raise TakeoverError('占用者不是当前用户的原生 Codex，未结束进程。')
    if any(arg in ('app-server', 'exec-server', 'daemon', 'remote-control') for arg in info['arguments'][1:]):
        raise TakeoverError('占用者是后台服务，不能按单个会话结束它。')
    if held.pid in {current.pid, *(p.pid for p in current.parents())}:
        raise TakeoverError('不能接管当前命令自身或父进程；请在另一个终端执行。')
    process = psutil.Process(held.pid)
    if os.name == 'nt':
        # Windows open_files() is incomplete. Scan actual locked files under
        # the process's CODEX_HOME and query Restart Manager for each instead.
        env = process.environ()
        configured = env.get('CODEX_HOME') or str(Path(env.get('USERPROFILE', str(Path.home())))/'.codex')
        if Path(configured).resolve() != Path(home).resolve():
            raise TakeoverError('占用进程的 CODEX_HOME 与目标不一致，未结束进程。')
        paths = list((Path(home)/'thread-writer-locks').glob('*.lock'))
        for path in paths:
            if path.resolve() != held.path and busy(path) is not None and held.pid in file_users(path):
                raise TakeoverError('占用进程同时持有其他会话，未结束进程。')
    else:
        for opened in process.open_files():
            path = Path(opened.path)
            if path.parent.name == 'thread-writer-locks' and path.resolve() != held.path:
                try:
                    uuid.UUID(path.stem)
                except ValueError:
                    continue
                raise TakeoverError('占用进程同时打开其他会话的写锁，未结束进程。')
    return process


@contextlib.contextmanager
def termination_handle(info, dry_run):
    """Pin Windows PID identity using a kernel handle until termination."""
    if os.name != 'nt':
        yield None
        return
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    handle = kernel.OpenProcess(0x100000 | 0x1000 | (0 if dry_run else 1), False, info['pid'])
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        started = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)/10_000_000 - 11644473600
        if abs(started - info['started']) > .001:
            raise TakeoverError('占用进程身份已变化，未结束进程。')
        yield handle
    finally:
        kernel.CloseHandle(handle)


def terminate(process, handle):
    if os.name != 'nt':
        process.terminate()  # psutil checks process creation time before SIGTERM.
        return
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    if not kernel.TerminateProcess(handle, 1):
        raise ctypes.WinError(ctypes.get_last_error())


def ensure_available(home, session, *, takeover=False, dry_run=False, timeout=15.0, emit=print):
    import psutil
    try:
        held = writer(home, session)
        if held is None:
            return False
        if not takeover:
            raise TakeoverError(f'会话 {session} 仍被其他进程占用；可使用 --takeover（先加 --dry-run 预览）。')
        if held.pid is None:
            raise TakeoverError('无法唯一核实会话占用者，未结束任何进程。')
        info = identity(held.pid)
        process = validate_owner(held, info, home)
        with termination_handle(info, dry_run) as handle:
            if writer(home, session) != held or identity(held.pid) != info:
                raise TakeoverError('会话占用者或进程身份已变化，未结束进程。')
            action = '终止单个 Windows 进程（未保存工作可能丢失）' if os.name == 'nt' else '发送 SIGTERM'
            if dry_run:
                emit(f'仅预览：将向会话 {session} 的 PID {held.pid} {action}；未结束进程。')
                return True
            emit(f'接管会话 {session}：向 PID {held.pid} {action}，等待写锁释放。')
            terminate(process, handle)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = writer(home, session)
                exited = not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
                if exited and current is None:
                    emit('旧进程已退出，写锁已释放；继续续接。')
                    return False
                if current and (current.key != held.key or current.pid not in (None, held.pid)):
                    raise TakeoverError('会话已被新进程占用，未结束新进程。')
                time.sleep(.1)
            raise TakeoverError('等待旧进程退出及写锁释放超时，未启动客户端、未追加终止操作。')
    except psutil.NoSuchProcess:
        if writer(home, session) is None:
            return False
        raise TakeoverError('占用进程发生变化，请重试。') from None
    except (psutil.AccessDenied, PermissionError):
        raise TakeoverError('没有权限核实会话占用者，未继续操作。') from None
