import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_switch.platforms import io


class PlatformIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ai-switch-空 格-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_atomic_write_replacement_failure_preserves_original(self):
        path = self.root / "state" / "secret.json"
        io.atomic_write(path, b"original")
        with patch.object(io.os, "replace", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                io.atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(path.parent.glob(".ai-switch-*")), [])
        io.atomic_write(path, "中文".encode())
        self.assertEqual(path.read_text(encoding="utf-8"), "中文")

    def test_real_cross_process_lock_exclusion_and_release(self):
        path = self.root / "lock"
        script = 'import sys;from ai_switch.platforms import io as platform_io;\nwith platform_io.configuration_lock(sys.argv[1]): print("acquired")'
        with io.configuration_lock(path):
            result = subprocess.run(
                [sys.executable, "-c", script, str(path)], capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"acquired", result.stdout)
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)], capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"acquired", result.stdout)

    def test_permissions_are_private(self):
        path = self.root / "private" / "key"
        io.atomic_write(path, b"fixture")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            return
        from ctypes import wintypes as w

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi.GetNamedSecurityInfoW.argtypes = [
            w.LPWSTR,
            ctypes.c_int,
            w.DWORD,
            w.LPVOID,
            w.LPVOID,
            ctypes.POINTER(w.LPVOID),
            w.LPVOID,
            ctypes.POINTER(w.LPVOID),
        ]
        advapi.GetAce.argtypes = [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)]
        advapi.ConvertSidToStringSidW.argtypes = [w.LPVOID, ctypes.POINTER(w.LPWSTR)]
        kernel.LocalFree.argtypes = [w.HLOCAL]
        acl, descriptor = w.LPVOID(), w.LPVOID()
        self.assertEqual(
            advapi.GetNamedSecurityInfoW(
                str(path),
                1,
                4,
                None,
                None,
                ctypes.byref(acl),
                None,
                ctypes.byref(descriptor),
            ),
            0,
        )
        try:
            count = ctypes.c_ushort.from_address(acl.value + 4).value
            sids = set()
            for index in range(count):
                ace, value = w.LPVOID(), w.LPWSTR()
                self.assertTrue(advapi.GetAce(acl, index, ctypes.byref(ace)))
                self.assertEqual(ctypes.c_ubyte.from_address(ace.value).value, 0)
                self.assertTrue(
                    advapi.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(value))
                )
                try:
                    sids.add(value.value)
                finally:
                    kernel.LocalFree(value)
            self.assertEqual(sids, {io.windows_sid(), "S-1-5-18", "S-1-5-32-544"})
        finally:
            kernel.LocalFree(descriptor)


if __name__ == "__main__":
    unittest.main()
