import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

import ai_switch as s
import getpass
import shutil
import sys

from ai_switch import templates as template_profiles


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ai-switch-onboarding-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for app in s.APPS:
            (self.root / app).mkdir()
        self.ca = self.root / "ca.crt"
        self.ca.write_text(
            ssl.DER_cert_to_PEM_cert(
                ssl.create_default_context().get_ca_certs(binary_form=True)[0]
            )
        )
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(
            json.dumps(
                {"models": [{"slug": "gpt-6-astra"}, {"slug": "gemini-3.8-flash-high"}]}
            )
        )
        self.m = s.Manager(self.root / "state")
        self.env = patch.dict(
            os.environ,
            {
                "ASTERGATE_API_KEY": "ASTER_FIXTURE",
                "MICU_FIXTURE": "MICU_FIXTURE_VALUE",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def init(self, app, ask=False):
        self.selected = app
        self.live = (
            self.root / app / ("settings.json" if app == "claude" else "config.toml")
        )
        if app == "claude":
            self.live.write_text(
                json.dumps(
                    dict(
                        model="sonnet",
                        env=dict(
                            ANTHROPIC_BASE_URL="https://micu.example",
                            ANTHROPIC_AUTH_TOKEN="MICU_FIXTURE_VALUE",
                        ),
                    )
                )
            )
        else:
            self.live.write_text(
                'model="gpt-6-astra"\nmodel_provider="micu"\n[model_providers.micu]\nname="Micu"\nbase_url="https://micu.example/v1"\nwire_api="responses"\nenv_key="MICU_FIXTURE"\n'
            )
        self.original = self.live.read_bytes()
        self.other = (
            self.root
            / ("codex" if app == "claude" else "claude")
            / ("config.toml" if app == "claude" else "settings.json")
        )
        self.other.write_bytes(b"Unselected file must never be read or modified")
        args = argparse.Namespace(
            app=app,
            codex_dir=str(self.root / "codex"),
            claude_dir=str(self.root / "claude"),
            catalog=str(self.catalog) if app == "codex" else None,
            ca=str(self.ca),
            aster_key_env="ASTERGATE_API_KEY",
            ask_api_key=ask,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.init(args)

    def cli(self, *args, expected=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = s.main(["--state-dir", str(self.m.root), *args])
        self.assertEqual(code, expected, out.getvalue())
        return out.getvalue()

    def lifecycle(self, app):
        self.init(app)
        self.assertEqual(self.m.apps(), (app,))
        self.assertEqual(set(self.m.load()["paths"]), {app})
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertNotIn("ASTER_FIXTURE", self.cli("profile", "show", "aster"))
        self.cli("profile", "list")
        self.cli("baseline", "list")
        self.cli("use", "aster")
        self.cli("use", "micu")
        self.assertEqual(self.live.read_bytes(), self.original)
        self.cli("profile", "add", "backup", "--from", "micu")
        self.cli(
            "profile",
            "edit",
            "backup",
            "--app",
            app,
            "--model",
            "fixture-model",
            "--ask-api-key",
            expected=1,
        )
        self.cli("profile", "edit", "backup", "--app", app, "--model", "fixture-model")
        self.cli("baseline", "protect", "backup")
        self.cli("use", "backup")
        self.cli("capture", "--app", app)
        self.cli("use", "micu")
        self.cli("profile", "delete", "backup")
        self.cli("baseline", "restore", "backup")
        self.cli("baseline", "restore", "micu")
        self.assertEqual(self.live.read_bytes(), self.original)
        self.cli("baseline", "restore", "aster", "--full-config")
        self.cli("baseline", "restore", "micu", "--full-config")
        self.assertEqual(self.live.read_bytes(), self.original)
        with patch.object(
            shutil,
            "which",
            side_effect=lambda name: "/fixture/" + name if name == app else None,
        ):
            self.cli("check")
            self.cli("run", app, "--dry-run")
        self.assertEqual(set(self.m.report()["apps"]), {app})
        self.assertEqual(
            self.other.read_bytes(), b"Unselected file must never be read or modified"
        )
        other_app = "claude" if app == "codex" else "codex"
        self.assertFalse(self.m.profile_path("micu", other_app).exists())
        self.assertFalse(self.m.profile_path("aster", other_app).exists())

    def test_codex_only_full_lifecycle(self):
        self.lifecycle("codex")
        self.assertEqual(
            self.m.profile("micu", "codex")["launch_env"]["MICU_FIXTURE"],
            "MICU_FIXTURE_VALUE",
        )

    def test_claude_only_full_lifecycle_without_catalog(self):
        self.lifecycle("claude")
        self.assertFalse((self.m.root / "assets/model-catalog.json").exists())
        self.assertFalse((self.m.root / "baseline/codex-auth.json").exists())

    def test_missing_client_commands_fail_clearly_without_changes(self):
        self.init("claude")
        before = self.m.state_path.read_bytes()
        for command in [
            ("use", "aster", "--app", "codex"),
            ("run", "codex", "--dry-run"),
            ("sessions", "--app", "codex"),
            ("capture", "--app", "codex"),
            ("baseline", "restore", "aster", "--app", "codex"),
            ("repair-session", str(uuid.uuid4()), "--dry-run"),
        ]:
            self.assertIn("未在此管理目录初始化", self.cli(*command, expected=1))
        self.assertEqual(self.m.state_path.read_bytes(), before)

    def test_interactive_key_never_appears_in_output(self):
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(getpass, "getpass", return_value="ASKED_FIXTURE") as ask,
        ):
            self.init("codex", ask=True)
        ask.assert_called_once()
        self.assertEqual(
            self.m.profile("aster", "codex")["providers"]["aster"][
                "experimental_bearer_token"
            ],
            "ASKED_FIXTURE",
        )
        self.assertNotIn("ASKED_FIXTURE", self.cli("profile", "show", "aster"))
        if os.name != "nt":
            self.assertEqual(
                self.m.profile_path("aster", "codex").stat().st_mode & 0o777, 0o600
            )

    def test_template_works_before_init_without_reading_state(self):
        with patch.object(
            type(self.m.storage),
            "load",
            side_effect=AssertionError("must not read state"),
        ):
            value = json.loads(
                self.cli("profile", "template", "aster", "--app", "codex")
            )
        self.assertEqual(
            value["providers"]["aster"]["experimental_bearer_token"],
            "__ASTERGATE_API_KEY__",
        )
        self.assertFalse(self.m.root.exists())
        with self.assertRaisesRegex(s.SwitchError, "占位符"):
            self.m.validate_profile("codex", value)

    def test_templates_work_independently_of_posix_lock_module(self):
        script = (
            "import sys;sys.modules['fcntl']=None;import ai_switch;"
            "assert 'ai_switch.platforms.linux' not in sys.modules;"
            "raise SystemExit(ai_switch.main(['profile','template','aster']))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("__ASTERGATE_API_KEY__", result.stdout)
        self.assertFalse(self.m.root.exists())

    def test_codex_missing_catalog_fails_before_saving_state(self):
        self.init("codex")
        other = s.Manager(self.root / "new-state")
        args = argparse.Namespace(
            app="codex",
            codex_dir=str(self.root / "codex"),
            claude_dir=str(self.root / "claude"),
            catalog=None,
            ca=str(self.ca),
            aster_key_env="ASTERGATE_API_KEY",
        )
        with self.assertRaisesRegex(s.SwitchError, "--catalog"):
            other.init(args)
        self.assertFalse(other.state_path.exists())
        self.assertFalse((other.root / "profiles").exists())


class CLICompatibilityTests(unittest.TestCase):
    def test_all_arguments_defaults_and_choices_match_1_9_1(self):
        # Captured from the released 1.9.1 parser, not generated from this code.
        expected = json.loads(
            (Path(__file__).parent / "fixtures/cli-1.9.1.json").read_text(
                encoding="utf-8"
            )
        )

        def normalize(value):
            if isinstance(value, str):
                return value.replace(str(Path.home()), "<HOME>")
            if isinstance(value, (tuple, list)):
                return [normalize(item) for item in value]
            if isinstance(value, (bool, int, float)) or value is None:
                return value
            return str(value)

        def collect(parser, path=()):
            children, actions = [], []
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for key, child in action.choices.items():
                        children.extend(collect(child, (*path, key)))
                elif action.dest not in ("help", "version"):
                    actions.append(
                        dict(
                            dest=action.dest,
                            flags=action.option_strings,
                            nargs=action.nargs,
                            default=normalize(action.default),
                            required=action.required,
                            choices=normalize(action.choices),
                        )
                    )
            return [dict(command=list(path), actions=actions)] + children

        self.assertEqual(collect(s.parser()), expected)


if __name__ == "__main__":
    unittest.main()
