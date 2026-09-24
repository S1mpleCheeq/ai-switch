"""Private configuration files and interoperable OS file locks."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import os
from pathlib import Path
import tempfile


class PlatformError(OSError):
    pass


def windows_sid():
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [w.HLOCAL]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [w.LPVOID, ctypes.POINTER(w.LPWSTR)]
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = w.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def protect_file(path, mode=0o600):
    """Protect before writing secrets. Windows ACLs replace POSIX mode bits."""
    if os.name != 'nt':
        os.chmod(path, mode)
        return
    from ctypes import wintypes as w
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)]
    advapi.GetSecurityDescriptorDacl.argtypes = [w.LPVOID, ctypes.POINTER(w.BOOL), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.BOOL)]
    advapi.SetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID]
    advapi.SetNamedSecurityInfoW.restype = w.DWORD
    kernel.LocalFree.argtypes = [w.HLOCAL]
    inherit = 'OICI' if Path(path).is_dir() else ''
    descriptor = w.LPVOID()
    sddl = 'D:P' + ''.join(f'(A;{inherit};FA;;;{sid})' for sid in (windows_sid(), 'SY', 'BA'))
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), w.LPVOID()
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        error = advapi.SetNamedSecurityInfoW(str(path), 1, 4 | 0x80000000, None, None, acl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


def private_dir(path):
    path = Path(path)
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise PlatformError('私密目录不能是符号链接或普通文件。')
        return
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir() or directory.is_symlink():
                raise
        else:
            protect_file(directory, 0o700)


def sync_directory(path):
    if os.name == 'nt':
        return  # Windows cannot open directories via os.open; file fsync remains.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.ai-switch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            protect_file(temporary, mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_lock(fd, acquire=True):
    """Nonblocking whole-file exclusive lock, matching Rust std File::try_lock."""
    if os.name != 'nt':
        import fcntl
        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)
        return
    from ctypes import wintypes as w
    import msvcrt
    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', w.DWORD), ('OffsetHigh', w.DWORD), ('hEvent', w.HANDLE)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LockFileEx.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)]
    overlap = Overlapped()
    handle = msvcrt.get_osfhandle(fd)
    ok = (kernel.LockFileEx(handle, 3, 0, 0xffffffff, 0xffffffff, ctypes.byref(overlap)) if acquire else
          kernel.UnlockFileEx(handle, 0, 0xffffffff, 0xffffffff, ctypes.byref(overlap)))
    if not ok:
        error = ctypes.get_last_error()
        if acquire and error in (33, 997):
            raise BlockingIOError(errno.EWOULDBLOCK, '文件已被其他进程锁定。')
        raise ctypes.WinError(error)


@contextlib.contextmanager
def configuration_lock(path):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise PlatformError('配置锁不能是符号链接。')
    with path.open('a+b') as handle:
        protect_file(path)
        file_lock(handle.fileno())
        try:
            yield handle
        finally:
            file_lock(handle.fileno(), False)
