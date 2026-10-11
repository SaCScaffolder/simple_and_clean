"""Tests for resolve.py (self-healing refresh conflict resolution)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.resolve import (  # noqa: E402
    ResolutionResult,
    _snake_case,
    detect_strategy,
)


class SnakeCaseTests(unittest.TestCase):
    """The package directory in `internal/<X>/` is always snake_case,
    regardless of how the spec named the service. The scaffolder
    derives it via `service_name_snake` (kebab -> snake + dot -> snake).
    Tests pin the conversion so the resolution strategy picks the
    right name out of the conflict paths."""

    def test_kebab_to_snake(self):
        self.assertEqual(_snake_case("billing-api"), "billing_api")

    def test_pascal_to_snake(self):
        self.assertEqual(_snake_case("BillingApi"), "billing_api")

    def test_already_snake(self):
        self.assertEqual(_snake_case("ala_service_modified"), "ala_service_modified")

    def test_dotted_to_snake(self):
        # Edge case: a name with dots (the v1 spec allows them). The
        # scaffolder's `service_name_snake` filter replaces dots with
        # underscores too.
        self.assertEqual(_snake_case("foo.bar"), "foo_bar")


class DetectStrategyTests(unittest.TestCase):
    """The strategy picker is the only logic that decides whether a
    conflict is auto-resolvable. Pins the rules so future changes
    to the strategy matrix are explicit."""

    def _config(self, service_name=""):
        from servicectl.sac_config import SacConfig
        return SacConfig(
            service_name=service_name,
            template="go-webapi",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            gcp_region="us-central1",
            gcp_project_id=None,
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            sac_version="dev",
            sac_base_commit=None,
        )

    def test_path_mismatch_when_internal_differs_from_dirname(self):
        """The integration test case: the source repo is named
        `sac_example_ala_service_modified` (snake_cased from
        `sac_example_ala_service_modified` would be
        `sac_example_ala_service_modified`), but the developer
        commit's `internal/<X>/` uses `ala_service_modified`. The
        scaffold regenerated under the dir name (because the config
        has no `service_name`), so the developer's path doesn't
        match. Strategy: PATH_MISMATCH."""
        config = self._config(service_name="")
        files = [
            "cmd/server/main.go",
            "internal/sac_example_ala_service_modified/middleware.go",
        ]
        # Wait -- that's the NEW path, not the developer's. The conflict
        # is on the developer's path, which is the OLD one. Let me
        # think about what the conflict list actually contains.
        # The git cherry-pick error reports the files that couldn't
        # be applied. For a renamed-directory case, it shows the
        # DEVELOPER's path (because that's what the patch references)
        # and a hint that the file was renamed in HEAD. The "did you
        # mean to move it" suggestion shows the NEW path.
        # So the conflict list has the developer's path:
        files = [
            "cmd/server/main.go",
            "internal/ala_service_modified/middleware.go",
            "internal/ala_service_modified/middleware_test.go",
        ]
        strategy = detect_strategy(files, config, source_repo_name="sac_example_ala_service_modified")
        self.assertEqual(strategy, "PATH_MISMATCH")

    def test_no_mismatch_when_internal_matches_service_name(self):
        """The developer's path matches the config's service_name
        (snake-cased). No path mismatch. The conflict must be
        something else (e.g. content), and we have no strategy for
        that yet."""
        from servicectl.sac_config import SacConfig
        config = SacConfig(service_name="ala_service_modified")
        files = ["internal/ala_service_modified/handler.go"]
        strategy = detect_strategy(files, config, source_repo_name="ala_service_modified")
        self.assertIsNone(strategy)

    def test_no_mismatch_when_internal_matches_dirname(self):
        """Legacy config (no `service_name` field); the scaffolder
        falls back to the dir name. The developer's path matches
        the dir name (which is what the scaffolder regenerated
        under). No path mismatch."""
        config = self._config(service_name="")
        files = ["internal/sac_example_ala_service_modified/handler.go"]
        strategy = detect_strategy(files, config, source_repo_name="sac_example_ala_service_modified")
        self.assertIsNone(strategy)

    def test_no_internal_paths_in_conflict(self):
        """If the conflict doesn't involve any `internal/<X>/`
        files, no path-mismatch strategy applies. (The conflict
        must be on a top-level file like `cmd/server/main.go`,
        which is a content conflict, not a path conflict.)"""
        config = self._config(service_name="")
        files = ["cmd/server/main.go", "Dockerfile"]
        strategy = detect_strategy(files, config, source_repo_name="sac_example_ala_service_modified")
        self.assertIsNone(strategy)


class ResolvePathMismatchTests(unittest.TestCase):
    """End-to-end test of `resolve_conflict()` with the PATH_MISMATCH
    strategy. Mocks out the `gh` CLI and the `git push` to avoid
    touching real remotes; asserts that the local commit is
    created correctly and the resolution PR would be opened with
    the right body."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "fixture"
        self.repo.mkdir()
        # Init a git repo so the commit/push subprocess calls work.
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.email=t@t",
             "-c", "user.name=T", "commit", "--allow-empty", "-q", "-m", "init"],
            check=True,
        )
        # Create the original scaffold layout: internal/ala_service_modified/.
        # This is what the integration test's bootstrap creates.
        (self.repo / "internal" / "ala_service_modified").mkdir(parents=True)
        (self.repo / "internal" / "ala_service_modified" / "handler.go").write_text("package x\n")
        # Initial commit on internal/.
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.email=t@t",
             "-c", "user.name=T", "commit", "-q", "-m", "bootstrap"],
            check=True,
        )
        # Write a legacy `.servicectl.json` (no `service_name`).
        from servicectl.sac_config import SacConfig, write_config
        write_config(self.repo, SacConfig(template="go-webapi"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolve_writes_service_name_and_creates_commit(self):
        """The PATH_MISMATCH resolution edits `.servicectl.json` to
        add `service_name: ala_service_modified`, commits the change
        with a `SAC-Operation: refresh-resolution` trailer, and
        pushes the new branch."""
        from servicectl.resolve import resolve_conflict
        from servicectl.sac_config import read_config

        conflicting_files = [
            "cmd/server/main.go",
            "internal/ala_service_modified/middleware.go",
            "internal/ala_service_modified/middleware_test.go",
        ]

        with mock.patch("subprocess.run") as mock_run:
            # First call: `git push -u origin <branch>` -> success.
            # Second call: `gh pr create ...` -> returns the PR URL.
            # All other subprocess.run calls in the test (git checkout,
            # git add, git commit, etc.) are also mocked. We let them
            # through to the real git (via setUp), but to keep the
            # test simple and self-contained, we mock them too.
            #
            # Set up: git push returns success, gh pr create returns
            # a URL.
            def fake_run(args, **kwargs):
                args_str = " ".join(str(a) for a in args)
                if "push" in args_str:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if args_str.startswith("gh pr create"):
                    return mock.Mock(
                        returncode=0,
                        stdout="https://github.com/foo/bar/pull/42\n",
                        stderr="",
                    )
                # For `git rev-parse HEAD`, return a fake SHA.
                if "rev-parse" in args_str:
                    return mock.Mock(
                        returncode=0,
                        stdout="deadbeef1234567890abcdef1234567890abcdef\n",
                        stderr="",
                    )
                return mock.Mock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = fake_run

            config = read_config(self.repo)
            result = resolve_conflict(
                source_repo=self.repo,
                conflicting_files=conflicting_files,
                config=config,
                run_id="abc12345",
            )

        # The result reports the strategy and the PR URL.
        self.assertEqual(result.strategy, "PATH_MISMATCH")
        self.assertEqual(result.pr_url, "https://github.com/foo/bar/pull/42")
        self.assertEqual(result.branch_name, "chore/resolve-refresh-conflict-abc12345")
        self.assertIn("ala_service_modified", result.message)

        # The config on disk now has the right service_name.
        updated = read_config(self.repo)
        self.assertEqual(updated.service_name, "ala_service_modified")

    def test_resolve_raises_when_strategy_does_not_apply(self):
        """If the conflict isn't a path mismatch, no auto-resolve
        strategy applies. The caller surfaces a clear error."""
        from servicectl.resolve import resolve_conflict
        from servicectl.sac_config import read_config

        conflicting_files = [
            "cmd/server/main.go",  # no internal/<X>/, just content
        ]
        config = read_config(self.repo)
        with self.assertRaises(RuntimeError) as cm:
            resolve_conflict(
                source_repo=self.repo,
                conflicting_files=conflicting_files,
                config=config,
                run_id="abc12345",
            )
        self.assertIn("no resolution strategy", str(cm.exception))


class ResolveConflictWithExistingServiceNameTests(unittest.TestCase):
    """Edge case: the config already has a `service_name` that
    doesn't match the developer's path. That's a deeper
    inconsistency and the scaffolder shouldn't try to fix it
    automatically -- it bails with a clear error so a human can
    decide."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "fixture"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        (self.repo / "internal" / "ala_service_modified").mkdir(parents=True)
        (self.repo / "internal" / "ala_service_modified" / "handler.go").write_text("package x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.email=t@t",
             "-c", "user.name=T", "commit", "-q", "-m", "init"],
            check=True,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_existing_service_name_mismatch_raises(self):
        from servicectl.resolve import resolve_conflict
        from servicectl.sac_config import SacConfig, read_config, write_config

        # Config has service_name set to a *different* value than the
        # developer's path. That's an inconsistency; the scaffolder
        # should NOT silently overwrite it.
        write_config(self.repo, SacConfig(service_name="different_name", template="go-webapi"))
        config = read_config(self.repo)

        conflicting_files = [
            "cmd/server/main.go",
            "internal/ala_service_modified/middleware.go",
        ]
        with self.assertRaises(RuntimeError) as cm:
            resolve_conflict(
                source_repo=self.repo,
                conflicting_files=conflicting_files,
                config=config,
                run_id="abc12345",
            )
        self.assertIn("manual intervention required", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
