#!/usr/bin/env python3
"""Install an OS-native launcher and local dependencies without downloading."""
import argparse
import importlib
import importlib.metadata
import os
from pathlib import Path
import shutil
import sys
import tempfile

MODULES = ('ai_switch.py','session_repair.py','session_process.py','read_guard.py',
           'template_profiles.py','platform_io.py','platform_runtime.py',
           'session_process_portable.py','README.md','requirements.txt','LICENSE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home()/'bin')
    parser.add_argument('--lib-dir', type=Path, default=Path.home()/'.local/share/ai-switch')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('Python 3.10+ is required.')
    for name, version in (('tomlkit','0.13.3'), ('psutil','7.0.0'), ('distlib','0.3.9')):
        try:
            if importlib.metadata.version(name) != version:
                raise ImportError()
        except (ImportError, importlib.metadata.PackageNotFoundError):
            parser.error('Install requirements.txt in the project virtual environment first.')
    source = Path(__file__).resolve().parent
    lib = args.lib_dir.expanduser().resolve(); binary = args.bin_dir.expanduser().resolve()
    lib.parent.mkdir(parents=True, exist_ok=True); binary.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.ai-switch-install-', dir=lib.parent))
    launch_stage = Path(tempfile.mkdtemp(prefix='.ai-switch-launch-', dir=binary))
    old = lib.with_name(lib.name+'.previous')
    try:
        for name in MODULES:
            shutil.copy2(source/name, stage/name)
        for name in ('templates','docs'):
            shutil.copytree(source/name, stage/name)
        for name in ('tomlkit','psutil'):
            module = importlib.import_module(name)
            shutil.copytree(Path(module.__file__).parent, stage/'vendor'/name,
                            ignore=shutil.ignore_patterns('__pycache__','*.pyc','tests'))
        for name in ('tomlkit','psutil','distlib'):
            dist = importlib.metadata.distribution(name)
            for f in dist.files or []:
                if 'LICENSE' in str(f).upper() and dist.locate_file(f).is_file():
                    shutil.copy2(dist.locate_file(f), stage/'vendor'/(name.upper()+'-LICENSE'))
                    break
        loader = ('import runpy, sys\nfrom pathlib import Path\nsource = '+repr(str(lib))+
                  '\nsys.path.insert(0, source)\nrunpy.run_path(str(Path(source) / "ai_switch.py"), run_name="__main__")\n')
        if os.name == 'nt':
            from distlib.scripts import ScriptMaker
            class Maker(ScriptMaker):
                def _get_script_text(self, entry):
                    return loader
            maker = Maker(None, str(launch_stage))
            maker.executable = getattr(sys, '_base_executable', sys.executable)
            maker.variants = {''}
            maker.make('ai-switch = ai_switch:main')
            filename = 'ai-switch.exe'
        else:
            filename = 'ai-switch'
            (launch_stage/filename).write_text('#!/usr/bin/env python3\n'+loader, encoding='utf-8')
            os.chmod(launch_stage/filename, 0o755)
        if old.exists():shutil.rmtree(old)
        if lib.exists():lib.rename(old)
        try:
            stage.rename(lib)
            os.replace(launch_stage/filename, binary/filename)
        except BaseException:
            if lib.exists():shutil.rmtree(lib)
            if old.exists():old.rename(lib)
            raise
        print(f'Installed {binary / filename}; implementation: {lib}')
    finally:
        if stage.exists():shutil.rmtree(stage)
        if launch_stage.exists():shutil.rmtree(launch_stage)


if __name__ == '__main__':main()
