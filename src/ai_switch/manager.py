"""Composition root and stable API for CLI commands. Services own behavior."""

from .storage import StateStore
from .profiles import ProfileService
from .baselines import BaselineService
from .clients.configuration import ConfigurationService
from .onboarding import OnboardingService
from .switching import SwitchService
from .diagnostics import DiagnosticsService
from .sessions.service import SessionService
from .launching import LaunchService


class Manager:
    def __init__(self, root=None):
        self.storage = StateStore(root)
        self.profiles = ProfileService(self)
        self.baselines = BaselineService(self)
        self.clients = ConfigurationService(self)
        self.onboarding = OnboardingService(self)
        self.switching = SwitchService(self)
        self.diagnostics = DiagnosticsService(self)
        self.session_service = SessionService(self)
        self.launcher = LaunchService(self)

    @property
    def root(self):
        return self.storage.root

    @property
    def state_path(self):
        return self.storage.state_path

    def load(self):
        return self.storage.load()

    def lock(self):
        return self.storage.lock()

    def apps(self, state=None, requested="all"):
        return self.storage.apps(state, requested)

    def profile(self, mode, app):
        return self.profiles.profile(mode, app)

    def normalize_profile(self, mode, app, data):
        return self.profiles.normalize_profile(mode, app, data)

    def profile_path(self, name, app):
        return self.profiles.profile_path(name, app)

    def profile_names(self):
        return self.profiles.profile_names()

    def managed_provider_names(self):
        return self.profiles.managed_provider_names()

    def validate_profile(self, app, data):
        return self.profiles.validate_profile(app, data)

    def expected_capture(self, app, profile):
        return self.profiles.expected_capture(app, profile)

    def drift(self, app, config, profile):
        return self.profiles.drift(app, config, profile)

    def original_snapshot_files(self, profiles):
        return self.baselines.original_snapshot_files(profiles)

    def original_profiles(self):
        return self.baselines.original_profiles()

    def protect_original(self):
        return self.baselines.protect_original()

    def baseline_status(self, name="micu"):
        return self.baselines.baseline_status(name)

    def restore_original(self, app="all", full_config=False, dry_run=False):
        return self.baselines.restore_original(app, full_config, dry_run)

    def named_baseline_dir(self, name):
        return self.baselines.named_baseline_dir(name)

    def baseline_resource_paths(self, app, profile):
        return self.baselines.baseline_resource_paths(app, profile)

    def named_baseline_files(self, name, profiles, configs, source):
        return self.baselines.named_baseline_files(name, profiles, configs, source)

    def named_baseline(self, name):
        return self.baselines.named_baseline(name)

    def protect_baseline(self, name="micu", from_backup=None):
        return self.baselines.protect_baseline(name, from_backup)

    def list_baselines(self):
        return self.baselines.list_baselines()

    def restore_baseline(
        self, name="micu", app="all", full_config=False, dry_run=False
    ):
        return self.baselines.restore_baseline(name, app, full_config, dry_run)

    def list_profiles(self):
        return self.profiles.list_profiles()

    def show_profile(self, name, app="all"):
        return self.profiles.show_profile(name, app)

    def add_profile(self, name, source):
        return self.profiles.add_profile(name, source)

    def delete_profile(self, name):
        return self.profiles.delete_profile(name)

    def edit_profile(self, args):
        return self.profiles.edit_profile(args)

    def apply_profile_edits(self, app, profile, args):
        return self.profiles.apply_profile_edits(app, profile, args)

    def read_config(self, state, app):
        return self.clients.read_config(state, app)

    def hook_command(self):
        return self.clients.hook_command()

    def clean_hooks(self, hooks):
        return self.clients.clean_hooks(hooks)

    def capture(self, app, config):
        return self.clients.capture(app, config)

    def render(self, app, config, profile, mode):
        return self.clients.render(app, config, profile, mode)

    def original_if_equal(self, app, result, data, mode):
        return self.clients.original_if_equal(app, result, data, mode)

    def init(self, args):
        return self.onboarding.init(args)

    def use(self, mode, app="all", dry_run=False, discard_changes=False):
        return self.switching.use(mode, app, dry_run, discard_changes)

    def transaction(self, files, state, before_commit=None):
        return self.storage.transaction(files, state, before_commit)

    def restore_snapshot(self, snapshots):
        return self.storage.restore_snapshot(snapshots)

    def recover(self):
        return self.storage.recover()

    def capture_current(self, app):
        return self.switching.capture_current(app)

    def report(self):
        return self.diagnostics.report()

    def reusable_repair(self, session):
        return self.session_service.reusable_repair(session)

    def repair_session(self, session, app="codex", dry_run=False, automatic=False):
        return self.session_service.repair_session(session, app, dry_run, automatic)

    def pending_repaired_sessions(self, known_ids, include_subagents=False):
        return self.session_service.pending_repaired_sessions(
            known_ids, include_subagents
        )

    def sessions(self, app, limit=20, include_subagents=False):
        return self.session_service.sessions(app, limit, include_subagents)

    def launch(self, args):
        return self.launcher.launch(args)

    def check(self, network=False):
        return self.diagnostics.check(network)
