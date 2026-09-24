#!/usr/bin/env python3
"""Build a source release from an explicit allowlist; never include user state."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile

FILES = (
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "README.md",
    "requirements.txt",
    "pyproject.toml",
    "install.py",
    "release.py",
    "ai_switch.py",
    ".github/workflows/ci.yml",
    "docs/setup.md",
    "docs/compatibility.md",
    "docs/security.md",
    "docs/releasing.md",
    "docs/architecture.md",
    "src/ai_switch/__init__.py",
    "src/ai_switch/__main__.py",
    "src/ai_switch/_version.py",
    "src/ai_switch/config.py",
    "src/ai_switch/service.py",
    "src/ai_switch/manager.py",
    "src/ai_switch/storage.py",
    "src/ai_switch/profiles.py",
    "src/ai_switch/baselines.py",
    "src/ai_switch/switching.py",
    "src/ai_switch/diagnostics.py",
    "src/ai_switch/launching.py",
    "src/ai_switch/onboarding.py",
    "src/ai_switch/templates.py",
    "src/ai_switch/cli.py",
    "src/ai_switch/clients/__init__.py",
    "src/ai_switch/clients/configuration.py",
    "src/ai_switch/clients/claude.py",
    "src/ai_switch/clients/codex.py",
    "src/ai_switch/sessions/__init__.py",
    "src/ai_switch/sessions/service.py",
    "src/ai_switch/sessions/repair.py",
    "src/ai_switch/platforms/__init__.py",
    "src/ai_switch/platforms/errors.py",
    "src/ai_switch/platforms/io.py",
    "src/ai_switch/platforms/linux.py",
    "src/ai_switch/platforms/portable.py",
    "src/ai_switch/platforms/processes.py",
    "src/ai_switch/platforms/runtime.py",
    "src/ai_switch/resources/__init__.py",
    "src/ai_switch/resources/read_guard.py",
    "src/ai_switch/resources/templates/aster/README.md",
    "src/ai_switch/resources/templates/aster/claude.json",
    "src/ai_switch/resources/templates/aster/codex.json",
    "tests/__init__.py",
    "tests/test_ai_switch.py",
    "tests/test_onboarding.py",
    "tests/test_platform_io.py",
    "tests/test_platform_runtime.py",
    "tests/test_release.py",
    "tests/test_session_process.py",
    "tests/test_session_process_portable.py",
    "tests/test_session_repair.py",
    "tests/fixtures/cli-1.9.1.json",
    "tests/integration/native_clients.py",
)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{25,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
)


def collect(root):
    root = Path(root).resolve()
    content = {}
    for name in FILES:
        path = root / name
        if any(
            item.is_symlink()
            for item in (path, *path.parents)
            if item != root and root in item.parents
        ):
            raise ValueError(f"发布文件不允许符号链接：{name}")
        if not path.is_file():
            raise ValueError(f"发布文件缺失：{name}")
        data = path.read_bytes()
        if len(data) > 5_000_000:
            raise ValueError(f"发布文件超过预期大小：{name}")
        text = data.decode("utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                raise ValueError(
                    f"疑似敏感内容，未打包：{name}:{number}（不输出匹配值）"
                )
        content[name] = data
    # Public template credentials must remain placeholders even if they don't
    # happen to match a well-known service's token prefix.
    claude = json.loads(content["src/ai_switch/resources/templates/aster/claude.json"])
    codex = json.loads(content["src/ai_switch/resources/templates/aster/codex.json"])
    if (
        claude["env"]["ANTHROPIC_AUTH_TOKEN"] != "__ASTERGATE_API_KEY__"
        or codex["providers"]["aster"]["experimental_bearer_token"]
        != "__ASTERGATE_API_KEY__"
        or claude["launch_env"]
        or codex["launch_env"]
    ):
        raise ValueError("公开模板必须只使用凭据占位符，launch_env 必须为空。")
    return content


def build(root, output, platform=None):
    content = collect(root)
    version = re.search(
        rb'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"\r?$',
        content["src/ai_switch/_version.py"],
        re.M,
    )
    if not version:
        raise ValueError("无法读取发布版本。")
    prefix = "ai-switch-" + version[1].decode()
    if platform:
        if platform not in ("linux", "macos", "windows"):
            raise ValueError("未知发布平台。")
        prefix += "-" + platform
    buffer = io.BytesIO()
    if platform == "windows":
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(content.items()):
                item = zipfile.ZipInfo(
                    prefix + "/" + name, date_time=(1980, 1, 1, 0, 0, 0)
                )
                item.external_attr = 0o100644 << 16
                item.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(item, data)
    else:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=buffer, mtime=0
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, data in sorted(content.items()):
                    item = tarfile.TarInfo(prefix + "/" + name)
                    item.size = len(data)
                    item.mode = 0o644
                    item.mtime = 0
                    archive.addfile(item, io.BytesIO(data))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / (prefix + (".zip" if platform == "windows" else ".tar.gz"))
    path.write_bytes(buffer.getvalue())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".sha256").write_text(digest + "  " + path.name + "\n")
    return path, len(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查白名单文件与常见密钥形式，不生成压缩包",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parent / "dist"
    )
    parser.add_argument(
        "--platform",
        choices=("all", "source", "linux", "macos", "windows"),
        default="all",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    try:
        if args.check:
            print(f"发布文件检查通过：{len(collect(root))} 个文件。")
        else:
            platforms = (
                (None, "linux", "macos", "windows")
                if args.platform == "all"
                else (None if args.platform == "source" else args.platform,)
            )
            for platform in platforms:
                path, count = build(root, args.output_dir, platform)
                print(f"已打包 {count} 个公开文件：{path}\nSHA256：{path.name}.sha256")
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
