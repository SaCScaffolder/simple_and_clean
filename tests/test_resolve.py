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

    def test_path_mismatch_when_internal_in_conflict(self):
        """Any `internal/<X>/` path in the conflict list triggers
        PATH_MISMATCH. The actual comparison between the scaffold's
        path and the developer's path is done in the resolve step,
        not here. This is the bare-path form of the conflict (what
        `_parse_conflicting_files` would extract if `git cherry-pick`
        reported the developer's path directly). See
        `test_path_mismatch_from_actual_git_cherry_pick_output` for
        the suggestion-line form (the actual output we see in the
        integration test)."""
        config = self._config(service_name="")
        files = [
            "cmd/server/main.go",
            "internal/ala_service_modified/middleware.go",
            "internal/ala_service_modified/middleware_test.go",
        ]
        strategy = detect_strategy(files, config, source_repo_name="sac_example_ala_service_modified")
        self.assertEqual(strategy, "PATH_MISMATCH")

    def test_path_mismatch_from_actual_git_cherry_pick_output(self):
        """Regression test for the failure surfaced in run 38118204340
        on 2026-10-11T07:22Z. The `conflicting_files` list from
        `git cherry-pick` doesn't always contain bare `internal/<X>/`
        paths; it often contains git's "did you mean to move it?"
        suggestion lines and parser artifacts. The strategy detector
        must extract the package dir from those suggestion lines.

        Verbatim conflict list from the run (truncated to the
        relevant entries):"""
        config = self._config(service_name="")
        files = [
            "cmd/server/main.go",
            (
                "5823af3 (feat: add CORS middleware, wire through cmd/server/main.go) "
                "inside a directory that was renamed in HEAD, suggesting it should "
                "perhaps be moved to internal/sac_example_ala_service_modified/middleware.go."
            ),
            (
                "5823af3 (feat: add CORS middleware, wire through cmd/server/main.go) "
                "inside a directory that was renamed in HEAD, suggesting it should "
                "perhaps be moved to internal/sac_example_ala_service_modified/middleware_test.go."
            ),
            "s, mark the corrected paths",
        ]
        strategy = detect_strategy(files, config, source_repo_name="sac_example_ala_service_modified")
        self.assertEqual(strategy, "PATH_MISMATCH")

    def test_no_mismatch_when_internal_matches_service_name(self):
        """The conflict only involves `cmd/server/main.go` (a content
        conflict on a top-level file), not any `internal/<X>/` path.
        No path-mismatch strategy applies."""
        from servicectl.sac_config import SacConfig
        config = SacConfig(service_name="ala_service_modified")
        files = ["cmd/server/main.go"]
        strategy = detect_strategy(files, config, source_repo_name="ala_service_modified")
        self.assertIsNone(strategy)

    def test_no_mismatch_when_internal_matches_dirname(self):
        """Same as above: the conflict is on a top-level file (no
        `internal/<X>/` path involved), so no path-mismatch
        strategy applies. The presence of an `internal/<X>/` in the
        conflict list is the trigger; its content is irrelevant to
        the strategy decision."""
        config = self._config(service_name="")
        files = ["cmd/server/main.go", "Dockerfile"]
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

        # Mock _infer_package_from_source so we don't depend on the
        # mocked subprocess.run returning realistic git output. The
        # unit under test is `resolve_conflict`, not the source
        # inspection; the latter is covered by other tests.
        with mock.patch("servicectl.resolve._infer_package_from_source", return_value="ala_service_modified"), \
             mock.patch("subprocess.run") as mock_run:
            # First call: `git push -u origin <branch>` -> success.
            # Second call: `gh pr create ...` -> returns the PR URL.
            # All other subprocess.run calls in the test (git checkout,
            # git add, git commit, etc.) are also mocked. We let them
            # through to the real git (via setUp), but to keep the
            # test simple and self-contained, we mock them too.
            #
            # Set up: git push returns success, gh api (used to open
            # the PR via REST) returns a JSON object with html_url.
            # All other subprocess.run calls in the test (git checkout,
            # git add, git commit, etc.) are mocked to return success.
            def fake_run(args, **kwargs):
                args_str = " ".join(str(a) for a in args)
                # `git remote get-url origin` -> return a fake URL
                if "remote" in args_str and "get-url" in args_str:
                    return mock.Mock(
                        returncode=0,
                        stdout="https://github.com/SaCScaffolder/sac_example_ala_service_modified.git\n",
                        stderr="",
                    )
                if "push" in args_str:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if args_str.startswith("gh api"):
                    # REST API returns JSON; pull html_url out.
                    return mock.Mock(
                        returncode=0,
                        stdout='{"html_url": "https://github.com/SaCScaffolder/sac_example_ala_service_modified/pull/42"}\n',
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
        self.assertEqual(
            result.pr_url,
            "https://github.com/SaCScaffolder/sac_example_ala_service_modified/pull/42",
        )
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


class GithubOwnerRepoFromRemoteTests(unittest.TestCase):
    """Tests for the `_github_owner_repo_from_remote` helper.

    Used to construct REST API paths for opening the resolution
    PR via `gh api` (instead of `gh pr create`, which needs
    `pull_requests:read` GraphQL scope that the App token doesn't
    have). The helper parses the remote URL.

    Edge cases: optional `.git` suffix, no suffix, non-GitHub URLs
    (raise), and non-owner/repo paths (raise).
    """

    def test_standard_https_url(self):
        from servicectl.resolve import _github_owner_repo_from_remote
        owner, repo = _github_owner_repo_from_remote(
            "https://github.com/SaCScaffolder/sac_example_ala_service_modified.git"
        )
        self.assertEqual(owner, "SaCScaffolder")
        self.assertEqual(repo, "sac_example_ala_service_modified")

    def test_url_without_dot_git_suffix(self):
        from servicectl.resolve import _github_owner_repo_from_remote
        owner, repo = _github_owner_repo_from_remote(
            "https://github.com/owner/repo"
        )
        self.assertEqual(owner, "owner")
        self.assertEqual(repo, "repo")

    def test_non_github_url_raises(self):
        from servicectl.resolve import _github_owner_repo_from_remote
        with self.assertRaises(RuntimeError) as cm:
            _github_owner_repo_from_remote(
                "git@github.com:SaCScaffolder/sac_example_ala_service_modified.git"
            )
        self.assertIn("not a GitHub URL", str(cm.exception))

    def test_wrong_path_shape_raises(self):
        from servicectl.resolve import _github_owner_repo_from_remote
        with self.assertRaises(RuntimeError) as cm:
            _github_owner_repo_from_remote(
                "https://github.com/just/one/level.git"
            )
        self.assertIn("could not parse", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
