#!/usr/bin/env python3
"""Install ai-switch with its pinned, vendored TOML dependency; no network access."""
import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import tempfile
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir',type=Path,default=Path.home()/'bin')
    parser.add_argument('--lib-dir',type=Path,default=Path.home()/'.local/share/ai-switch')
    args=parser.parse_args()
    if os.name == 'nt':
        parser.error('原生 Windows 暂不支持安装；请在 WSL2 内安装 Python、ai-switch 和原生客户端。')
    if sys.version_info < (3, 10):
        parser.error('Python 3.10+ is required.')
    try:
        import tomlkit
    except ImportError:
        parser.error('Install requirements.txt in the project virtual environment first.')
    if importlib.metadata.version('tomlkit') != '0.13.3':
        parser.error('Use the project virtual environment with tomlkit==0.13.3.')
    source=Path(__file__).resolve().parent
    lib=args.lib_dir.expanduser().resolve();binary=args.bin_dir.expanduser().resolve()
    lib.parent.mkdir(parents=True,exist_ok=True);binary.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.ai-switch-install-',dir=lib.parent))
    old=lib.with_name(lib.name+'.previous')
    try:
        for name in ('ai_switch.py','session_repair.py','session_process.py','read_guard.py','template_profiles.py','README.md','requirements.txt','LICENSE'):
            shutil.copy2(source/name,stage/name)
        for name in ('templates','docs'):
            shutil.copytree(source/name,stage/name)
        shutil.copytree(Path(tomlkit.__file__).parent,stage/'vendor/tomlkit',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        dist=importlib.metadata.distribution('tomlkit')
        for f in dist.files or []:
            if 'LICENSE' in str(f).upper():
                shutil.copy2(dist.locate_file(f),stage/'vendor/TOMLKIT-LICENSE');break
        if old.exists():
            shutil.rmtree(old)
        if lib.exists():lib.rename(old)
        try:
            stage.rename(lib)
            fd,temp=tempfile.mkstemp(prefix='.ai-switch-',dir=binary)
            with os.fdopen(fd,'w') as file:
                file.write('#!/usr/bin/env python3\nimport runpy, sys\nfrom pathlib import Path\nsource = '+repr(str(lib))+'\nsys.path.insert(0, source)\nrunpy.run_path(str(Path(source) / "ai_switch.py"), run_name="__main__")\n')
                file.flush();os.fsync(file.fileno())
            os.chmod(temp,0o755);os.replace(temp,binary/'ai-switch')
        except BaseException:
            if lib.exists():shutil.rmtree(lib)
            if old.exists():old.rename(lib)
            raise
        print(f'Installed {binary / "ai-switch"}; implementation: {lib}')
    finally:
        if stage.exists():shutil.rmtree(stage)


if __name__=='__main__':main()
