"""Explicit Linux Codex writer-lock takeover; never delete locks or kill groups."""
from __future__ import annotations

from dataclasses import dataclass
try:
    import fcntl
except ImportError:
    fcntl = None
import os
from pathlib import Path
import select
import signal
import stat
import sys
import time
import uuid


class TakeoverError(Exception):
    pass


@dataclass(frozen=True)
class Writer:
    path: Path
    key: tuple[int, int, int]
    pid: int | None


def file_key(info):
    return os.major(info.st_dev), os.minor(info.st_dev), info.st_ino


def parse_locks(lines):
    result = []
    for line in lines:
        if line.startswith('lock:'):
            line = line.removeprefix('lock:').lstrip()
        parts = line.split()
        if len(parts) < 6 or '->' in parts or parts[1] != 'FLOCK' or parts[3] != 'WRITE':
            continue
        try:
            major, minor, inode = parts[5].split(':')
            result.append((int(parts[4]), (int(major, 16), int(minor, 16), int(inode))))
        except ValueError:
            continue
    return result


def kernel_locks():
    return parse_locks(Path('/proc/locks').read_text().splitlines())


def locked_descriptors(pid):
    """Correlate kernel locks with visible fd inodes, including overlay mounts."""
    result = []
    try:
        descriptors = list(Path(f'/proc/{pid}/fd').iterdir())
    except (FileNotFoundError, PermissionError):
        return result
    for descriptor in descriptors:
        try:
            lines = Path(f'/proc/{pid}/fdinfo/{descriptor.name}').read_text().splitlines()
            locks = parse_locks(line for line in lines if line.startswith('lock:'))
            if any(owner == pid for owner, _ in locks):
                result.append((file_key(descriptor.stat()), Path(os.readlink(descriptor))))
        except (FileNotFoundError, PermissionError):
            continue
    return result


def writer(codex_home, session):
    """An existing empty lock file is not evidence of a live owner."""
    if fcntl is None:
        raise TakeoverError('原生 Windows 暂不支持会话锁检查；请在 WSL2 内运行。')
    session = str(uuid.UUID(session))
    directory = Path(codex_home).resolve()/'thread-writer-locks'
    path = directory/(session+'.lock')
    if directory.is_symlink() or path.is_symlink():
        raise TakeoverError('会话写锁路径经过符号链接，未结束进程。')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TakeoverError('会话写锁不是普通文件，未结束进程。')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if sys.platform != 'linux':
                return Writer(path, file_key(info), None)
            locks = kernel_locks()
            candidates = {pid for pid, key in locks if key == file_key(info)}
            if not candidates:
                # overlayfs may expose a different device number than the
                # backing inode shown by /proc/locks. fdinfo gives the binding.
                candidates = {pid for pid, _ in locks if pid > 0}
            owners = {pid for pid in candidates
                      if any(key == file_key(info) for key, _ in locked_descriptors(pid))}
            pid = next(iter(owners)) if len(owners) == 1 and min(owners) > 0 else None
            return Writer(path, file_key(info), pid)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    finally:
        os.close(fd)


def process_identity(pid):
    proc = Path('/proc')/str(pid)
    # /proc/<pid>/stat's comm can contain spaces or closing parentheses.
    fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
    executable = os.readlink(proc/'exe').removesuffix(' (deleted)')
    arguments = (proc/'cmdline').read_bytes().split(b'\0')
    return dict(start=fields[19], ppid=int(fields[1]), uid=proc.stat().st_uid,
                executable=executable, arguments=arguments)


def validate_owner(held, identity):
    """Refuse self/ancestors, non-Codex owners, daemons and shared writers."""
    if identity['uid'] != os.getuid() or Path(identity['executable']).name != 'codex':
        raise TakeoverError(f'PID {held.pid} 不是当前用户的原生 Codex 进程，未结束进程。')
    if any(arg in (b'app-server', b'exec-server', b'daemon', b'remote-control')
           for arg in identity['arguments'][1:]):
        raise TakeoverError(f'PID {held.pid} 是后台服务，不能按单个会话结束它；未结束进程。')
    ancestor = os.getpid()
    seen = set()
    while ancestor > 0 and ancestor not in seen:
        if ancestor == held.pid:
            raise TakeoverError('目标进程是当前命令自身或其父进程，不能在该会话内部接管自己；请在另一个终端执行。')
        seen.add(ancestor)
        try:
            fields = Path(f'/proc/{ancestor}/stat').read_text().rsplit(')', 1)[1].split()
            ancestor = int(fields[1])
        except FileNotFoundError:
            break
    # Scan the owner's open lock descriptors, including other CODEX_HOME roots.
    # Holding a second conversation's writer lock makes process-wide termination
    # too broad, even if both threads happen to use the same model/provider.
    seen_target = False
    for key, link in locked_descriptors(held.pid):
        if key == held.key:
            seen_target = True
        if link.parent.name == 'thread-writer-locks' and key != held.key:
            try:
                uuid.UUID(link.stem)
            except ValueError:
                continue
            raise TakeoverError(f'PID {held.pid} 同时持有其他会话的写锁，未结束进程。')
    if not seen_target:
        raise TakeoverError('持锁进程的文件描述符已变化，未结束进程；请重试。')


def ensure_available(codex_home, session, *, takeover=False, dry_run=False,
                     timeout=15.0, emit=print):
    """Return True only for a dry-run plan to terminate a currently live owner."""
    if sys.platform != 'linux':
        import session_process_portable as portable
        try:
            return portable.ensure_available(codex_home, session, takeover=takeover,
                dry_run=dry_run, timeout=timeout, emit=emit)
        except portable.TakeoverError as exc:
            raise TakeoverError(str(exc)) from None
    held = writer(codex_home, session)
    if held is None:
        return False
    owner = f'PID {held.pid}' if held.pid else '无法识别的进程'
    if not takeover:
        if sys.platform != 'linux':
            raise TakeoverError(f'会话 {session} 仍被其他进程占用；当前平台不支持自动接管，请先退出旧客户端。')
        raise TakeoverError(f'会话 {session} 正被 {owner} 占用；如需结束旧进程并续接，请加 --takeover（可先加 --dry-run 预览）。')
    if held.pid is None:
        raise TakeoverError('会话仍被占用，但无法可靠识别持锁 PID；未结束进程。')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise TakeoverError('当前系统不支持安全的 PID 句柄接管；未结束进程。')
    handle = None
    try:
        identity = process_identity(held.pid)
        handle = os.pidfd_open(held.pid)
        if process_identity(held.pid) != identity:
            raise TakeoverError('持锁进程身份已变化，未结束进程；请重试。')
        validate_owner(held, identity)
        current = writer(codex_home, session)
        if current is None:
            return False
        if current != held or process_identity(held.pid) != identity:
            raise TakeoverError('会话占用者已变化，未结束进程；请重试。')
        if dry_run:
            emit(f'仅预览：将向会话 {session} 的 PID {held.pid} 发送 SIGTERM，等待退出及写锁释放；未发送信号。')
            return True
        emit(f'接管会话 {session}：向 PID {held.pid} 发送 SIGTERM，等待退出及写锁释放（最多 {timeout:g} 秒）。')
        # The pidfd pins the process identity even if its numeric PID is reused.
        signal.pidfd_send_signal(handle, signal.SIGTERM)
        deadline = time.monotonic()+timeout
        while True:
            exited = bool(select.select([handle], [], [], 0)[0])
            current = writer(codex_home, session)
            if exited and current is None:
                emit('旧进程已退出，写锁已释放；继续检查历史并启动 Codex。')
                return False
            if current is not None and (current.key != held.key or current.pid not in (None, held.pid)):
                raise TakeoverError('等待期间会话被其他进程占用，未结束新占用者；未启动客户端。')
            if time.monotonic() >= deadline:
                raise TakeoverError(f'等待 PID {held.pid} 退出及释放写锁超时；未执行 SIGKILL，未启动客户端。')
            time.sleep(min(.1, max(0, deadline-time.monotonic())))
    except (ProcessLookupError, FileNotFoundError):
        if writer(codex_home, session) is None:
            return False  # The specific owner went away before the signal.
        raise TakeoverError('检查期间持锁进程发生变化；未启动客户端，请重试。') from None
    except PermissionError:
        raise TakeoverError('没有足够权限核实或结束持锁进程；未启动客户端。') from None
    finally:
        if handle is not None:
            os.close(handle)
