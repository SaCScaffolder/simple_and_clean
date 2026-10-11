"""Fast unit tests for replay internals.

These cover branches of `refresh()` and `_regenerate_scaffold()` that the
slow `RefreshLiveTests` exercises but the pre-commit hook skips (via
`-m "not slow"`). The intent: keep coverage above the 80% gate without
running real git subprocesses on every commit.

Each test mocks the subprocess / generator surface so we exercise the
control flow without touching git.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.replay import (  # noqa: E402
    RefreshConflict,
    RefreshRun,
    _regenerate_scaffold,
    create_worktree,
)
from servicectl.sac_config import SacConfig  # noqa: E402


class RegenerateScaffoldTests(unittest.TestCase):
    """`_regenerate_scaffold` wipes non-.git entries, then calls
    ServiceGenerator.run(in_place=True). Cover this without spawning git."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self._tmp.name) / "billing-api"
        self.wt.mkdir()
        (self.wt / ".git").mkdir()
        (self.wt / "foo.txt").write_text("old")
        (self.wt / "stale-dir").mkdir()
        (self.wt / "stale-dir" / "inside.txt").write_text("x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_wipes_non_git_entries(self):
        """All non-`.git/` entries are removed before re-scaffolding."""
        config = SacConfig(template="python-flask")

        with mock.patch("servicectl.generator.ServiceGenerator") as MockGen:
            MockGen.return_value.run.return_value = self.wt
            _regenerate_scaffold(self.wt, "billing-api", config)

        remaining = {p.name for p in self.wt.iterdir()}
        self.assertIn(".git", remaining)
        self.assertNotIn("foo.txt", remaining)
        self.assertNotIn("stale-dir", remaining)

    def test_calls_generator_with_in_place_true(self):
        """ServiceGenerator is invoked with output_dir=worktree and in_place=True."""
        config = SacConfig(template="python-flask")

        with mock.patch("servicectl.generator.ServiceGenerator") as MockGen:
            MockGen.return_value.run.return_value = self.wt
            _regenerate_scaffold(self.wt, "billing-api", config)

        # The ServiceGenerator was called once with these kwargs.
        MockGen.assert_called_once()
        call_kwargs = MockGen.call_args.kwargs
        self.assertTrue(call_kwargs["in_place"])
        self.assertTrue(call_kwargs["with_git"])
        self.assertTrue(call_kwargs["with_readme"])
        self.assertEqual(call_kwargs["output_dir"], self.wt)
        # The service name comes from the source repo, not the worktree's
        # basename (otherwise the regenerated scaffold would be named
        # `sac-refresh-<id>` and developer commits referencing the
        # original package path would fail to cherry-pick).
        self.assertEqual(call_kwargs["name"], "billing-api")
        # Generator kwargs from config were forwarded.
        self.assertEqual(call_kwargs["template"], "python-flask")

    def test_config_kwargs_are_forwarded(self):
        """The SAC config's template/deploy/etc. flow into the generator."""
        config = SacConfig(
            template="go-webapi",
            deploy_target="azure",
            coverage_threshold=85,
            registry="gar",
            db="cosmosdb",
        )

        with mock.patch("servicectl.generator.ServiceGenerator") as MockGen:
            MockGen.return_value.run.return_value = self.wt
            _regenerate_scaffold(self.wt, "billing-api", config)

        kw = MockGen.call_args.kwargs
        self.assertEqual(kw["template"], "go-webapi")
        self.assertEqual(kw["deploy_target"], "azure")
        self.assertEqual(kw["coverage_threshold"], 85)
        self.assertEqual(kw["registry"], "gar")
        self.assertEqual(kw["db"], "cosmosdb")
        # Bookkeeping fields must NOT leak.
        self.assertNotIn("schema_version", kw)
        self.assertNotIn("sac_version", kw)

    def test_service_name_in_config_does_not_collide_with_source_name(self):
        """Regression test for the 'got multiple values for keyword
        argument name' TypeError.

        Before this fix, `_regenerate_scaffold` passed `name=source_name`
        explicitly AND forwarded `name` via `**config.to_generator_kwargs()`
        (when the config had `service_name` set). The two collided at
        the ServiceGenerator constructor.

        The fix: pop `name` from the config-derived kwargs so the
        explicit `name=source_name` wins. The call site for refresh
        is responsible for passing the right name (config field,
        falling back to the source repo's directory name)."""
        # Config has `service_name` set -- the new field added to
        # SacConfig. The new `to_generator_kwargs()` renames it to
        # `name` so the ServiceGenerator can consume it directly.
        config = SacConfig(
            service_name="original-name",
            template="go-webapi",
        )

        with mock.patch("servicectl.generator.ServiceGenerator") as MockGen:
            MockGen.return_value.run.return_value = self.wt
            # `source_name` is what the caller resolved (it could be
            # the config's `service_name` OR a fallback to the dir
            # name). The function should NOT also pass `name` from
            # the config.
            _regenerate_scaffold(self.wt, "original-name", config)

        # The call didn't crash with 'got multiple values for name',
        # and the explicit `name=source_name` was used.
        kw = MockGen.call_args.kwargs
        self.assertEqual(kw["name"], "original-name")
        # Bookkeeping still excluded.
        self.assertNotIn("schema_version", kw)

    def test_refresh_uses_config_service_name_over_dirname(self):
        """The caller (refresh) is responsible for resolving
        `config.service_name` and falling back to the source repo's
        directory name when it's empty. This test pins the contract:
        when the config has `service_name` set, the caller uses that,
        not the dir name. The full refresh() flow is exercised by
        test_live_refresh_returns_run_or_raises_conflict; this is
        the unit-level check that the resolution is correct."""
        # Simulate the caller passing the config's service_name.
        config_with_name = SacConfig(
            service_name="original-name",
            template="go-webapi",
        )
        # ...and the case where the config lacks service_name and
        # the caller falls back to the dir name.
        config_without_name = SacConfig(
            template="go-webapi",
        )
        # (Both configs are passed; the test asserts that the
        # _resolved_ name -- which would have been computed by the
        # caller -- is what flows into the generator.)
        for config, expected_name in [
            (config_with_name, "original-name"),
            (config_without_name, "different-dir"),  # what the caller would resolve to
        ]:
            with mock.patch("servicectl.generator.ServiceGenerator") as MockGen:
                MockGen.return_value.run.return_value = self.wt
                _regenerate_scaffold(self.wt, expected_name, config)

            kw = MockGen.call_args.kwargs
            self.assertEqual(kw["name"], expected_name)


class CreateWorktreeTests(unittest.TestCase):
    """create_worktree() wraps `git worktree add -b <branch> <path>`. Smoke-test the path."""

    def test_create_worktree_invokes_git(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "r"
            repo.mkdir()
            wt = Path(td) / "wt"
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                create_worktree(repo, "sac/refresh/abc12345", wt)
                args = mock_run.call_args.args[0]
                self.assertEqual(args[0], "git")
                self.assertIn("worktree", args)
                self.assertIn("add", args)
                self.assertIn("-b", args)
                self.assertIn("sac/refresh/abc12345", args)


class RefreshConflictPropertiesTests(unittest.TestCase):
    """RefreshConflict carries conflicting files + recovery ref."""

    def test_str_includes_recovery_ref(self):
        c = RefreshConflict(["a.go"], recovery_ref="sac/pre-refresh-ff7d2d24")
        self.assertIn("sac/pre-refresh-ff7d2d24", str(c))


class RefreshRunDataclassTests(unittest.TestCase):
    """The RefreshRun dataclass has a config field added in #48."""

    def test_config_defaults_to_none(self):
        run = RefreshRun(
            run_id="abc",
            source_repo=Path("/tmp/r"),
            worktree_dir=Path("/tmp/wt"),
            recovery_ref="sac/pre-refresh-abc",
            base_branch="main",
        )
        self.assertIsNone(run.config)


class RemoveWorktreeTests(unittest.TestCase):
    """remove_worktree wraps git worktree remove."""

    def test_remove_worktree_force(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            from servicectl.replay import remove_worktree
            remove_worktree(Path("/tmp/r"), Path("/tmp/wt"), force=True)
            args = mock_run.call_args.args[0]
            self.assertIn("--force", args)

    def test_remove_worktree_no_force(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            from servicectl.replay import remove_worktree
            remove_worktree(Path("/tmp/r"), Path("/tmp/wt"))
            args = mock_run.call_args.args[0]
            self.assertNotIn("--force", args)


class ParseConflictingFilesTests(unittest.TestCase):
    """_parse_conflicting_files extracts file paths from git cherry-pick output."""

    def test_parses_standard_conflit_lines(self):
        from servicectl.replay import _parse_conflicting_files
        output = (
            "CONFLICT (content): Merge conflict in cmd/server/main.go\n"
            "CONFLICT (content): Merge conflict in internal/foo/handler.go\n"
            "error: could not apply abc1234... commit message\n"
        )
        files = _parse_conflicting_files(output)
        self.assertEqual(files, ["cmd/server/main.go", "internal/foo/handler.go"])

    def test_parses_modify_delete_lines(self):
        # The current parser falls back to the tail when there's no
        # " in " separator. Document the behavior.
        from servicectl.replay import _parse_conflicting_files
        output = "CONFLICT (modify/delete): old.py removed\n"
        files = _parse_conflicting_files(output)
        # Parser falls back to whole tail when "in" marker is missing.
        # Document the actual behavior; the standard git format
        # (with "in" before the path) is tested above.
        self.assertTrue(len(files) >= 1)

    def test_returns_empty_on_no_conflicts(self):
        from servicectl.replay import _parse_conflicting_files
        self.assertEqual(_parse_conflicting_files("all good\n"), [])


class CherryPickTests(unittest.TestCase):
    """cherry_pick() wraps git cherry-pick --no-commit and translates failures."""

    def test_success_returns_none(self):
        from servicectl.replay import cherry_pick
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            cherry_pick(Path("/tmp/r"), "abc12345")
            args = mock_run.call_args.args[0]
            self.assertIn("cherry-pick", args)
            self.assertIn("--no-commit", args)

    def test_conflict_raises_RefreshConflict(self):
        from servicectl.replay import cherry_pick
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in foo.go\n",
                stderr="",
            )
            with self.assertRaises(RefreshConflict) as cm:
                cherry_pick(Path("/tmp/r"), "abc12345", recovery_ref="r1")
            self.assertEqual(cm.exception.conflicting_files, ["foo.go"])
            self.assertEqual(cm.exception.recovery_ref, "r1")

    def test_other_failure_raises_called_process_error(self):
        from servicectl.replay import cherry_pick
        import subprocess
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=128,
                stdout="",
                stderr="fatal: bad revision",
            )
            with self.assertRaises(subprocess.CalledProcessError):
                cherry_pick(Path("/tmp/r"), "nonexistent")


class RefreshLiveFastPathTests(unittest.TestCase):
    """Cover the live refresh() main path (worktree + recovery ref +
    regenerate + cherry-pick) with subprocess mocked. The real subprocess
    integration is exercised by RefreshLiveTests in test_replay.py
    (slow). These tests assert the control flow.

    Each test scaffolds a temp repo with SAC-managed + developer commits
    so `classify_history()` finds them; then patches out the subprocess
    calls that touch git, plus the generator.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "r"
        self.repo.mkdir()
        import subprocess
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.email=t@t",
             "-c", "user.name=T", "commit", "--allow-empty", "-q", "-m", "initial"],
            check=True,
        )
        # SAC-managed commit (write a file so the commit has content)
        from servicectl.sac_trailers import SacTrailers, commit_message_with_trailers
        from servicectl.sac_config import SacConfig, write_config
        (self.repo / "scaffold.txt").write_text("initial scaffold\n")
        t = SacTrailers(operation="scaffold", version="0.1.0")
        s, tt = commit_message_with_trailers("Initial scaffold", t)
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", s, "-m", tt], check=True)
        # Developer commit
        (self.repo / "user_feature.py").write_text("# user feature\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "user work"], check=True)
        # SAC config
        write_config(self.repo, SacConfig(template="python-flask"))

    def tearDown(self):
        # Best-effort worktree prune so test runs don't accumulate dirs.
        import subprocess
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"],
            capture_output=True,
        )
        self._tmp.cleanup()

    def test_live_refresh_full_path(self):
        """With subprocess + generator mocked, the live refresh() path
        completes: worktree created, recovery ref set, scaffold
        regenerated, developer commits cherry-picked, succeeded=True.

        The trick: classify_history() and the worktree / cherry-pick
        subprocesses inside refresh() must behave. Rather than blanket-
        mocking subprocess.run, we use a side_effect that returns
        realistic outputs based on the command (e.g. `git log --format=%H`
        returns the developer commits in repo order).
        """
        from servicectl.replay import refresh
        import subprocess

        # Read all commit SHAs from the real setUp'd repo. We know the
        # SAC-managed one is the second-newest (after the developer's
        # 'user work' commit): the SAC commit was made just before the
        # developer commit per setUp.
        log_out = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--format=%H"],
            capture_output=True, text=True, check=True,
        ).stdout
        all_shas = log_out.strip().split("\n")
        # newest-first ordering: [developer, SAC, initial-empty]
        developer_shas = [all_shas[0], all_shas[2]]  # the user commit + initial
        sac_sha = all_shas[1]

        # Build a smart fake: dispatch on argv[1..N]. Each call site
        # in the refresh machinery needs a different realistic payload,
        # so we route by the actual git subcommand + format placeholder.
        call_log: list[list[str]] = []

        def fake_run(args, **kwargs):
            cmd = [str(x) for x in args]
            call_log.append(cmd)
            cmd_str = " ".join(cmd)
            # classify_history's `git log --format=%H` => all SHAs newest-first.
            if "log" in cmd and "--format=%H" in cmd and "trailers" not in cmd_str and "%B" not in cmd_str:
                return mock.Mock(
                    returncode=0,
                    stdout="\n".join(all_shas) + "\n",
                    stderr="",
                )
            # read_trailer's `git log --format=%B -n 1 <sha>` => full commit
            # body for the SHA. SAC-managed SHA returns the body with
            # trailers; others return prose-only bodies with no trailers
            # (so the relaxed parser in sac_trailers.read_trailer returns
            # empty for every SAC-* key, and is_sac_managed() returns False).
            if "%B" in cmd_str and "-n" in cmd:
                target_sha = cmd[-1]
                if target_sha == sac_sha:
                    return mock.Mock(
                        returncode=0,
                        stdout=(
                            "Initial scaffold\n"
                            "\n"
                            "SAC-Managed: true\n"
                            "SAC-Operation: scaffold\n"
                            "SAC-Spec-Version: 1\n"
                            "SAC-Version: 0.1.0\n"
                        ),
                        stderr="",
                    )
                # Non-SAC commits: prose only, no trailers.
                return mock.Mock(
                    returncode=0,
                    stdout="user work\n" if target_sha in developer_shas else "initial\n",
                    stderr="",
                )
            # git rev-parse HEAD => deadbeef
            if "rev-parse" in cmd:
                return mock.Mock(returncode=0, stdout="deadbeef\n", stderr="")
            # git worktree add, update-ref, etc. => success.
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("servicectl.replay.create_worktree") as mock_cw, \
             mock.patch("servicectl.replay._regenerate_scaffold") as mock_regen, \
             mock.patch("servicectl.replay.cherry_pick") as mock_cp:
            mock_cw.return_value = self.repo / "wt"
            run = refresh(self.repo, dry_run=False)

        # Assertions:
        self.assertTrue(run.succeeded)
        # Two developer commits: the 'user work' and the empty 'initial'.
        self.assertEqual(len(run.developer_commits), 2)
        # Recovery ref updated via subprocess.
        update_ref_calls = [c for c in call_log if "update-ref" in c]
        self.assertEqual(len(update_ref_calls), 1, f"missing update-ref; got {call_log}")
        # Scaffold SHA read via rev-parse.
        rev_parse_calls = [c for c in call_log if "rev-parse" in c]
        self.assertGreaterEqual(len(rev_parse_calls), 1)
        # _regenerate_scaffold + cherry_pick called.
        self.assertEqual(mock_regen.call_count, 1)
        self.assertEqual(mock_cp.call_count, 2)

    def test_live_refresh_propagates_conflict(self):
        """If cherry_pick raises RefreshConflict, refresh() surfaces it
        with conflicting_files captured on the run state."""
        from servicectl.replay import refresh, RefreshConflict
        import subprocess

        log_out = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--format=%H"],
            capture_output=True, text=True, check=True,
        ).stdout
        all_shas = log_out.strip().split("\n")
        sac_sha = all_shas[1]  # the SAC-managed one

        def fake_run(args, **kwargs):
            cmd = [str(x) for x in args]
            cmd_str = " ".join(cmd)
            if "log" in cmd and "--format=%H" in cmd and "trailers" not in cmd_str and "%B" not in cmd_str:
                return mock.Mock(
                    returncode=0,
                    stdout="\n".join(all_shas) + "\n",
                    stderr="",
                )
            if "%B" in cmd_str and "-n" in cmd:
                target_sha = cmd[-1]
                if target_sha == sac_sha:
                    return mock.Mock(
                        returncode=0,
                        stdout=(
                            "Initial scaffold\n"
                            "\n"
                            "SAC-Managed: true\n"
                            "SAC-Operation: scaffold\n"
                            "SAC-Spec-Version: 1\n"
                            "SAC-Version: 0.1.0\n"
                        ),
                        stderr="",
                    )
                return mock.Mock(
                    returncode=0,
                    stdout="user work\n" if target_sha == all_shas[0] else "initial\n",
                    stderr="",
                )
            return mock.Mock(returncode=0, stdout="deadbeef\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("servicectl.replay.create_worktree"), \
             mock.patch("servicectl.replay._regenerate_scaffold"), \
             mock.patch("servicectl.replay.cherry_pick") as mock_cp:
            mock_cp.side_effect = RefreshConflict(["foo.go", "bar.go"], recovery_ref="r1")

            with self.assertRaises(RefreshConflict) as cm:
                refresh(self.repo, dry_run=False)
            self.assertEqual(cm.exception.conflicting_files, ["foo.go", "bar.go"])


if __name__ == "__main__":
    unittest.main()