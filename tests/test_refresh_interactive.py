"""Tests for the interactive prompt path in `servicectl refresh`.

Background
----------
`servicectl refresh` reads `.servicectl.json` from the target repo
and regenerates the scaffold against the current `simple_and_clean`.
The "I came here to flip one field" UX is the same one used by
`servicectl init`: a TTY gets walked through the deploy-impacting
fields with the current value as the default for each, so changing
`registry: ghcr` to `registry: gar` is a single keystroke away
without re-typing the other six fields.

This file pins that contract:
  1. `--no-input` skips the prompts and uses the on-disk config as-is.
  2. Non-TTY stdin (CI) without `--no-input` also skips the prompts
     (no silent hangs).
  3. `SERVICECTL_NO_INPUT=1` is a backdoor for CI scripts that can't
     pass `--no-input` (e.g. wrappers).
  4. The prompt walks through template / deploy / region / project /
     registry / db with the current value as the default for each.
  5. If anything changed, `.servicectl.json` is written back to disk
     before `replay_refresh()` runs.
  6. If nothing changed, the prompt exits cleanly and no write happens.
  7. Picking `gcp-*` deploy target re-prompts for region + project ID;
     picking `azure` re-prompts for region; picking `local` skips both.
  8. Picking `gar` registry prints the resolved GAR image path so the
     user can see where the next CI run will publish (the
     "I-just-learned-this" GCP exposure detail).
  9. The `service_name`, `ci_provider`, and `coverage_threshold`
     fields are preserved as-is (not prompted; changing them has
     different blast-radius).

All tests use `CliRunner` with `input=` to simulate stdin.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from servicectl.cli import (
    _prompt_refresh_overrides,
    _summarize_refresh_changes,
    _stdin_is_tty,
    refresh,
)
from servicectl.replay import RefreshRun
from servicectl.sac_config import SacConfig, read_config


# A baseline SacConfig that mirrors what `init` would write for a
# go-webapi service. The tests start from this and tweak what they
# need (e.g. set registry=ghcr, then prompt-change to gar).
BASELINE_CONFIG = SacConfig(
    schema_version=1,
    service_name="ala_service_gcp_test",
    template="go-webapi",
    ci_provider="github-actions",
    deploy_target="gcp-cloud-run",
    azure_region="eastus",
    gcp_region="us-central1",
    gcp_project_id="sacscaffolder",
    coverage_threshold=80,
    registry="ghcr",
    db="postgres",
    sac_version="0.1.0",
    sac_base_commit=None,
)


def _make_refresh_repo(tmp: Path) -> Path:
    """Create a fake scaffolded repo with a .servicectl.json on disk.

    We need a real `.servicectl.json` because `refresh` reads it via
    `read_config()` before any prompt fires. We don't need a real git
    history because we mock `replay_refresh()` (the real git dance is
    tested in `test_replay.py`).
    """
    repo = tmp / "service"
    repo.mkdir()
    config_path = repo / ".servicectl.json"
    config_path.write_text(json.dumps(BASELINE_CONFIG.to_dict(), indent=2), encoding="utf-8")
    return repo


def _stub_replay(*, succeeded: bool = True, scaffold_sha: str = "deadbeef"):
    """Return a context manager stack that stubs out replay_refresh().

    The stub returns a minimal RefreshRun so the post-prompt code
    path (dry-run / confirmation / success panel) doesn't blow up.

    `succeeded` defaults to True so the success panel fires; the
    real git dance is tested in `test_replay.py` and the
    failure paths in `test_refresh.py`. These tests focus on the
    prompt logic, not the replay outcome.
    """
    def _make_run(repo: Path, dry_run: bool = False) -> RefreshRun:
        return RefreshRun(
            run_id="test1234",
            source_repo=repo,
            worktree_dir=repo / ".worktree",
            recovery_ref="refs/sac/pre-refresh-test1234",
            base_branch="main",
            branch_name="sac/refresh/test1234",
            scaffold_sha=scaffold_sha,
            conflicts=[],
            succeeded=succeeded,
            developer_commits=[],
            config=BASELINE_CONFIG,
        )

    return patch("servicectl.replay.refresh", side_effect=_make_run)


def _run_refresh_with_input(
    *,
    repo: Path,
    stdin_input: str,
    args: list[str] | None = None,
    env: dict | None = None,
    no_input: bool = False,
    yes: bool = True,  # skip the final y/n prompt by default; tests for that flow can set False
    no_merge: bool = True,  # skip the post-refresh merge prompt by default; tests for that flow can set False
):
    """Invoke `servicectl refresh` against `repo` simulating stdin.

    `stdin_input` is the literal text the user would type; newlines
    separate answers. The TTY check is patched to True when we want
    the prompt path; left alone (False in CliRunner) when testing
    non-interactive.
    """
    runner = CliRunner()
    full_env = {"PATH": ""}
    if env:
        full_env.update(env)
    final_args = list(args or []) + [str(repo)]
    if no_input:
        final_args.append("--no-input")
    if yes:
        final_args.append("--yes")
    if no_merge:
        final_args.append("--no-auto-merge")

    with _stub_replay(), \
         patch("subprocess.run") as mock_subprocess:
        mock_subprocess.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        result = runner.invoke(refresh, final_args, env=full_env, input=stdin_input, catch_exceptions=False)
    return result


# ---------------------------------------------------------------------------
# Direct unit tests of the helper, no CLI plumbing. Pin the contract that the
# rest of the suite relies on.
# ---------------------------------------------------------------------------

class TestHelperBasics:
    """The _prompt_refresh_overrides helper itself, exercised directly.

    These bypass Click entirely so a regression in the helper is
    caught before it shows up as a CLI-level failure.
    """

    def test_returns_same_config_when_user_accepts_all_defaults(self):
        """Pressing Enter through every prompt returns a config equal
        to the input (dataclass equality, not just field-by-field)."""
        input_str = "\n\n\n\n\n\n"  # template, deploy, region, project, registry, db
        with patch("servicectl.cli._stdin_is_tty", return_value=True), \
             patch("click.prompt", side_effect=lambda *a, **kw: kw.get("default", "")) as mock_prompt:
            new_config = _prompt_refresh_overrides(config=BASELINE_CONFIG)
        # Each interesting field was prompted (template, deploy_target, region/project, registry, db)
        assert mock_prompt.call_count >= 5
        assert new_config == BASELINE_CONFIG

    def test_returns_new_config_when_user_changes_registry(self):
        """Type `gar` at the registry prompt and the helper returns a
        config with registry=gar but every other field equal to the
        input (preserve-as-default contract)."""
        answers = iter([
            BASELINE_CONFIG.template,         # template: accept default
            BASELINE_CONFIG.deploy_target,    # deploy: accept default
            BASELINE_CONFIG.gcp_region,       # gcp region: accept default
            BASELINE_CONFIG.gcp_project_id,   # gcp project id: accept default
            "gar",                            # registry: change!
            BASELINE_CONFIG.db,               # db: accept default
        ])
        with patch("servicectl.cli._stdin_is_tty", return_value=True), \
             patch("click.prompt", side_effect=lambda *a, **kw: next(answers)):
            new_config = _prompt_refresh_overrides(config=BASELINE_CONFIG)
        assert new_config.registry == "gar"
        assert new_config.template == BASELINE_CONFIG.template
        assert new_config.deploy_target == BASELINE_CONFIG.deploy_target
        assert new_config.gcp_region == BASELINE_CONFIG.gcp_region
        assert new_config.gcp_project_id == BASELINE_CONFIG.gcp_project_id
        assert new_config.db == BASELINE_CONFIG.db
        # Untouched fields preserved verbatim
        assert new_config.service_name == BASELINE_CONFIG.service_name
        assert new_config.ci_provider == BASELINE_CONFIG.ci_provider
        assert new_config.coverage_threshold == BASELINE_CONFIG.coverage_threshold

    def test_preserves_unprompted_fields(self):
        """ci_provider, coverage_threshold, service_name, sac_version,
        sac_base_commit are NOT prompted; they pass through verbatim
        even if the user accepts every default."""
        baseline = dataclasses.replace(
            BASELINE_CONFIG,
            ci_provider="azure-devops",
            coverage_threshold=85,
            service_name="my-custom-name",
            sac_version="0.2.0",
            sac_base_commit="abc123",
        )
        input_str = "\n\n\n\n\n\n"
        with patch("servicectl.cli._stdin_is_tty", return_value=True), \
             patch("click.prompt", side_effect=lambda *a, **kw: kw.get("default", "")):
            new_config = _prompt_refresh_overrides(config=baseline)
        assert new_config.ci_provider == "azure-devops"
        assert new_config.coverage_threshold == 85
        assert new_config.service_name == "my-custom-name"
        assert new_config.sac_version == "0.2.0"
        assert new_config.sac_base_commit == "abc123"


# ---------------------------------------------------------------------------
# Tests of the summary helper.
# ---------------------------------------------------------------------------

class TestSummarizeChanges:
    """The _summarize_refresh_changes helper renders a change list."""

    def test_no_changes_returns_empty_list(self):
        assert _summarize_refresh_changes(BASELINE_CONFIG, BASELINE_CONFIG) == []

    def test_one_change_returns_one_line(self):
        after = dataclasses.replace(BASELINE_CONFIG, registry="gar")
        changes = _summarize_refresh_changes(BASELINE_CONFIG, after)
        assert len(changes) == 1
        assert "registry" in changes[0]
        assert "gar" in changes[0]

    def test_multiple_changes_returned_in_order(self):
        after = dataclasses.replace(
            BASELINE_CONFIG,
            registry="gar",
            deploy_target="gcp-gke-autopilot",
            template="python-flask",
        )
        changes = _summarize_refresh_changes(BASELINE_CONFIG, after)
        assert len(changes) == 3
        # Order is the field order in the helper (template, deploy, ...)
        assert "template" in changes[0]
        assert "deploy target" in changes[1]
        assert "registry" in changes[2]


# ---------------------------------------------------------------------------
# CLI-level tests of the --no-input flag, the SERVICECTL_NO_INPUT env var,
# the non-TTY fallback, and the prompt flow.
# ---------------------------------------------------------------------------

class TestNoInputFlag:
    """The --no-input flag must skip the prompt and use on-disk config."""

    def test_no_input_uses_on_disk_config(self):
        """With --no-input, refresh runs against the .servicectl.json
        on disk. No prompts, no write-back, no diff."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            result = _run_refresh_with_input(repo=repo, stdin_input="", no_input=True)
            assert result.exit_code == 0, f"Output:\n{result.output}\nException: {result.exception}"
            # Read on-disk config BEFORE the tempdir is cleaned up.
            on_disk = read_config(repo)
        assert on_disk.registry == "ghcr"  # unchanged
        assert on_disk == BASELINE_CONFIG

    def test_no_input_with_missing_config_still_surfaces_error(self):
        """If .servicectl.json is missing, the prompt can't help --
        the user gets the same error they got before this feature
        existed (RefreshConfigMissing). We don't use the replay stub
        here because we want the real replay.refresh() to surface
        its real error."""
        from servicectl.replay import RefreshConfigMissing
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "no-config"
            repo.mkdir()
            runner = CliRunner()
            # Patch replay.refresh to raise the real error.
            with patch("servicectl.replay.refresh", side_effect=RefreshConfigMissing(f"{repo / '.servicectl.json'} not found.")):
                result = runner.invoke(refresh, [str(repo), "--no-input", "--yes", "--no-auto-merge"], catch_exceptions=False)
        # The CLI converts RefreshConfigMissing to a friendly panel
        # and exits 2.
        assert result.exit_code == 2
        assert "RefreshConfigMissing" in result.output or "not found" in result.output or ".servicectl.json" in result.output


class TestNonTTYBehavior:
    """Without --no-input but in a non-TTY context, refresh must not hang."""

    def test_piped_stdin_uses_on_disk_config(self):
        """In CliRunner stdin is a StringIO (not a TTY), so the prompt
        path is skipped. The on-disk config is used as-is."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            # Note: no --no-input flag, but CliRunner's stdin is non-TTY
            result = _run_refresh_with_input(repo=repo, stdin_input="")
            assert result.exit_code == 0
            on_disk = read_config(repo)
        assert on_disk.registry == "ghcr"  # unchanged

    def test_servicectl_no_input_env_var_disables_prompts(self):
        """SERVICECTL_NO_INPUT=1 is the CI-script backdoor. It
        behaves identically to --no-input."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            with patch.dict("os.environ", {"SERVICECTL_NO_INPUT": "1"}):
                result = _run_refresh_with_input(repo=repo, stdin_input="")
            assert result.exit_code == 0
            on_disk = read_config(repo)
        assert on_disk.registry == "ghcr"  # unchanged


class TestPromptFlow:
    """When stdin IS a TTY and --no-input is NOT set, prompts should fire."""

    def test_accept_all_defaults_writes_no_change(self):
        """Pressing Enter through every prompt should result in no
        write to .servicectl.json."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            # TTY patch + 6 Enter presses (template, deploy, region,
            # project, registry, db -- all 6 are prompted for the
            # baseline GCP config; azure_region is NOT prompted because
            # deploy_target is gcp-cloud-run).
            input_str = "\n\n\n\n\n\n"
            with patch("servicectl.cli._stdin_is_tty", return_value=True):
                result = _run_refresh_with_input(repo=repo, stdin_input=input_str, yes=True)
            assert result.exit_code == 0, f"Output:\n{result.output}\nException: {result.exception}"
            on_disk = read_config(repo)
            assert on_disk == BASELINE_CONFIG

    def test_change_registry_writes_new_value(self):
        """The canonical use case: user accepts everything except
        registry, which they change from `ghcr` to `gar`. The new
        value should be written to .servicectl.json before
        replay_refresh() runs."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            # 1. template (accept), 2. deploy (accept), 3. gcp_region
            # (accept), 4. gcp_project_id (accept), 5. registry
            # (CHANGE to gar), 6. db (accept).
            input_str = "\n\n\n\ngar\n\n"
            with patch("servicectl.cli._stdin_is_tty", return_value=True):
                result = _run_refresh_with_input(repo=repo, stdin_input=input_str, yes=True)
            assert result.exit_code == 0, f"Output:\n{result.output}\nException: {result.exception}"
            on_disk = read_config(repo)
            assert on_disk.registry == "gar"
            # Everything else preserved
            assert on_disk.template == BASELINE_CONFIG.template
            assert on_disk.deploy_target == BASELINE_CONFIG.deploy_target
            assert on_disk.gcp_region == BASELINE_CONFIG.gcp_region
            assert on_disk.gcp_project_id == BASELINE_CONFIG.gcp_project_id
            assert on_disk.db == BASELINE_CONFIG.db

    def test_change_deploy_to_azure_prompts_for_region_not_project(self):
        """Switching from gcp-cloud-run to azure should:
          - prompt for azure_region (new)
          - NOT prompt for gcp_region / gcp_project_id (no longer relevant)
          - write the new deploy_target + azure_region to disk
        """
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            # 1. template (accept), 2. deploy (CHANGE to azure),
            # 3. azure_region (override to westus2), 4. registry
            # (accept), 5. db (accept). gcp_region and gcp_project_id
            # are NOT prompted because deploy=azure.
            input_str = "\nazure\nwestus2\n\n\n"
            with patch("servicectl.cli._stdin_is_tty", return_value=True):
                result = _run_refresh_with_input(repo=repo, stdin_input=input_str, yes=True)
            assert result.exit_code == 0, f"Output:\n{result.output}\nException: {result.exception}"
            on_disk = read_config(repo)
            assert on_disk.deploy_target == "azure"
            assert on_disk.azure_region == "westus2"
            # gcp_* preserved as-is (untouched, even if stale)
            assert on_disk.gcp_region == BASELINE_CONFIG.gcp_region
            assert on_disk.gcp_project_id == BASELINE_CONFIG.gcp_project_id

    def test_change_deploy_to_gcp_prompts_for_region_and_project(self):
        """Switching from azure to gcp-cloud-run should:
          - NOT prompt for azure_region (no longer relevant)
          - prompt for gcp_region + gcp_project_id (with current
            values as defaults, even if the current values are
            stale/empty for an azure config)
        """
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            # Rewrite the on-disk config to look like an azure service
            # (no gcp project id set) so the prompt is non-trivial.
            azure_config = dataclasses.replace(
                BASELINE_CONFIG,
                deploy_target="azure",
                azure_region="westus2",
                gcp_project_id=None,
            )
            (repo / ".servicectl.json").write_text(
                json.dumps(azure_config.to_dict(), indent=2), encoding="utf-8"
            )
            # 1. template (accept), 2. deploy (CHANGE to gcp-cloud-run),
            # 3. gcp_region (accept default), 4. gcp_project_id
            # (override), 5. registry (accept), 6. db (accept).
            input_str = "\ngcp-cloud-run\n\nmy-new-gcp-proj\n\n\n"
            with patch("servicectl.cli._stdin_is_tty", return_value=True):
                result = _run_refresh_with_input(repo=repo, stdin_input=input_str, yes=True)
            assert result.exit_code == 0, f"Output:\n{result.output}\nException: {result.exception}"
            on_disk = read_config(repo)
            assert on_disk.deploy_target == "gcp-cloud-run"
            assert on_disk.gcp_project_id == "my-new-gcp-proj"

    def test_gar_registry_prints_resolved_image_path(self):
        """The GCP-exposure touch: when the user picks `gar`, the
        prompt should print the resolved GAR image path so the user
        can see where the next CI run will publish."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            input_str = "\n\n\n\ngar\n\n"  # change registry to gar
            with patch("servicectl.cli._stdin_is_tty", return_value=True):
                result = _run_refresh_with_input(repo=repo, stdin_input=input_str, yes=True)
            assert result.exit_code == 0
            # The output should mention the GAR image path
            assert "us-central1-docker.pkg.dev" in result.output
            assert "sacscaffolder" in result.output  # the gcp_project_id
            assert "ala_service_gcp_test" in result.output  # the service_name


# ---------------------------------------------------------------------------
# Tests that the prompt does NOT fire in non-interactive contexts.
# ---------------------------------------------------------------------------

class TestPromptSkipped:
    """In CI / non-TTY contexts, the prompt path must not fire even if
    stdin happens to be readable (e.g. when the user pipes `yes` into
    the command)."""

    def test_prompt_skipped_when_not_a_tty(self):
        """The TTY check is `_stdin_is_tty()` which returns False in
        CliRunner by default. If the user pipes a `gar\n` into
        `servicectl refresh` it must NOT be silently consumed -- the
        on-disk config wins, not the pipe."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            input_str = "\n\n\n\ngar\n\n"  # the kind of input a user might pipe
            # Do NOT patch _stdin_is_tty -- leave it at CliRunner's
            # default of False.
            result = _run_refresh_with_input(repo=repo, stdin_input=input_str)
            assert result.exit_code == 0
            on_disk = read_config(repo)
        # The piped `gar` was NOT consumed; the on-disk config wins.
        assert on_disk.registry == "ghcr"


# ---------------------------------------------------------------------------
# Tests for the post-refresh auto-merge / --no-merge / --auto-merge flow.
# ---------------------------------------------------------------------------

class TestAutoMerge:
    """After a successful refresh, the CLI offers to merge the worktree
    branch into the base branch. The decision tree (see
    `_offer_post_refresh_merge` in cli.py):

      --auto-merge:  do the merge + cleanup, no prompt
      --no-merge:    print the manual commands, no prompt
      default (TTY): ask y/N
      default (non-TTY or --no-input): print the manual commands

    We don't shell out to a real git repo in these tests; we stub
    `subprocess.run` to verify the right git commands are issued in the
    right order, and the right panels are printed in each branch.
    """

    def _stub_replay_with_branch(self):
        """Like _stub_replay, but the stubbed RefreshRun carries
        branch_name='sac/refresh/test1234' so the merge helper knows
        what to do."""
        from servicectl.replay import RefreshRun
        def _make_run(repo: Path, dry_run: bool = False) -> RefreshRun:
            return RefreshRun(
                run_id="test1234",
                source_repo=repo,
                worktree_dir=repo / ".worktree",
                recovery_ref="refs/sac/pre-refresh-test1234",
                base_branch="main",
                branch_name="sac/refresh/test1234",
                scaffold_sha="deadbeef",
                conflicts=[],
                succeeded=True,
                developer_commits=[],
                config=BASELINE_CONFIG,
            )
        return patch("servicectl.replay.refresh", side_effect=_make_run)

    def test_no_merge_prints_manual_commands(self):
        """`--no-merge` (the default in the test helper) skips the
        merge entirely and prints the manual-commands panel."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            result = _run_refresh_with_input(repo=repo, stdin_input="")
            assert result.exit_code == 0
            # The manual-commands panel is in the output
            assert "git merge --ff-only sac/refresh/test1234" in result.output
            assert "git worktree remove" in result.output
            assert "git branch -d sac/refresh/test1234" in result.output

    def test_auto_merge_runs_git_commands_in_order(self):
        """`--auto-merge` issues git merge, git worktree remove, and
        git branch -d, in that order, with returncode 0 on each."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))
            call_log = []

            def fake_subprocess_run(args, **kwargs):
                # Record the first 3 positional args (the command list)
                # and the returncode we'll fake.
                call_log.append({
                    "args": list(args),
                    "returncode": 0,
                    "stderr": b"",
                    "stdout": b"",
                })
                # Return a CompletedProcess-shaped object
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout=b"", stderr=b"",
                )

            runner = CliRunner()
            with self._stub_replay_with_branch(), \
                 patch("subprocess.run", side_effect=fake_subprocess_run):
                result = runner.invoke(
                    refresh,
                    [str(repo), "--auto-merge", "--yes"],
                    env={"PATH": ""},
                    input="",
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, f"Output:\n{result.output}"

            # We expect at least 3 subprocess calls: the merge, the
            # worktree remove, and the branch delete.
            # (Note: _merge_and_cleanup_worktree calls subprocess.run
            # 3 times. Tests may also issue subprocess calls through
            # other paths; we filter to the git ones.)
            git_calls = [c for c in call_log if c["args"] and c["args"][0] == "git"]
            assert len(git_calls) >= 3, f"expected >= 3 git calls, got {len(git_calls)}: {git_calls}"

            # 1st: git merge --ff-only sac/refresh/test1234
            assert git_calls[0]["args"][1:3] == ["-C", str(repo)]
            assert git_calls[0]["args"][3] == "merge"
            assert git_calls[0]["args"][4] == "--ff-only"
            assert git_calls[0]["args"][5] == "sac/refresh/test1234"

            # 2nd: git worktree remove <worktree> --force
            assert git_calls[1]["args"][3] == "worktree"
            assert git_calls[1]["args"][4] == "remove"
            assert git_calls[1]["args"][6] == "--force"

            # 3rd: git branch -d sac/refresh/test1234
            assert git_calls[2]["args"][3] == "branch"
            assert git_calls[2]["args"][4] == "-d"
            assert git_calls[2]["args"][5] == "sac/refresh/test1234"

    def test_auto_merge_merge_failure_preserves_worktree(self):
        """If the fast-forward merge fails (returncode != 0), the
        helper reports the error and leaves the worktree alone."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))

            def fake_subprocess_run(args, **kwargs):
                # First call is the merge; fail it.
                if args and args[0] == "git" and "merge" in args:
                    return subprocess.CompletedProcess(
                        args=list(args),
                        returncode=1,
                        stdout=b"",
                        stderr=b"fatal: not a fast-forward",
                    )
                # Subsequent calls (worktree remove, branch delete)
                # should NOT happen if merge failed.
                raise AssertionError(
                    f"unexpected subprocess call after merge failure: {args}"
                )

            runner = CliRunner()
            with self._stub_replay_with_branch(), \
                 patch("subprocess.run", side_effect=fake_subprocess_run):
                result = runner.invoke(
                    refresh,
                    [str(repo), "--auto-merge", "--yes"],
                    env={"PATH": ""},
                    input="",
                    catch_exceptions=False,
                )
            assert result.exit_code == 0
            assert "merge failed" in result.output
            assert "not a fast-forward" in result.output
            # No subprocess call beyond the failed merge: the panel
            # reports the failure and tells the user the worktree is
            # preserved.
            assert "worktree is preserved at" in result.output
            assert "sac/refresh/test1234" in result.output

    def test_default_yes_responds_to_y(self):
        """Default UX (TTY, no flag): the helper asks y/N. If the
        user types 'y', the merge runs."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))

            def fake_subprocess_run(args, **kwargs):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout=b"", stderr=b"",
                )

            # Patch click.prompt to return BASELINE_CONFIG's current
            # value for each field. This bypasses click's retry-on-
            # bad-input behavior so the walkthrough consumes zero
            # stdin lines; the only consumer is the post-refresh
            # `input("y/N")` call inside _offer_post_refresh_merge.
            from unittest.mock import MagicMock
            answers = iter([
                BASELINE_CONFIG.template,
                BASELINE_CONFIG.deploy_target,
                BASELINE_CONFIG.gcp_region,
                BASELINE_CONFIG.gcp_project_id,
                BASELINE_CONFIG.registry,
                BASELINE_CONFIG.db,
            ])
            patched_prompt = MagicMock(side_effect=lambda *a, **kw: next(answers))

            runner = CliRunner()
            with self._stub_replay_with_branch(), \
                 patch("subprocess.run", side_effect=fake_subprocess_run), \
                 patch("servicectl.cli._stdin_is_tty", return_value=True), \
                 patch("click.prompt", patched_prompt):
                result = runner.invoke(
                    refresh,
                    [str(repo)],  # no flags; full default UX
                    env={"PATH": ""},
                    input="y\ny\n",  # pre-refresh confirm + post-refresh merge
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, f"Output:\n{result.output}"
            # The y/N merge question was asked
            assert "Would you like me to merge" in result.output
            # The merge ran ("All done." is printed only on full success)
            assert "All done." in result.output

    def test_default_n_responds_to_n_prints_manual(self):
        """Default UX (TTY, no flag): the user types 'n' to the merge
        prompt, and the manual-commands panel is shown instead of
        running the merge."""
        with tempfile.TemporaryDirectory() as td:
            repo = _make_refresh_repo(Path(td))

            def fake_subprocess_run(args, **kwargs):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout=b"", stderr=b"",
                )

            from unittest.mock import MagicMock
            answers = iter([
                BASELINE_CONFIG.template,
                BASELINE_CONFIG.deploy_target,
                BASELINE_CONFIG.gcp_region,
                BASELINE_CONFIG.gcp_project_id,
                BASELINE_CONFIG.registry,
                BASELINE_CONFIG.db,
            ])
            patched_prompt = MagicMock(side_effect=lambda *a, **kw: next(answers))

            runner = CliRunner()
            with self._stub_replay_with_branch(), \
                 patch("subprocess.run", side_effect=fake_subprocess_run), \
                 patch("servicectl.cli._stdin_is_tty", return_value=True), \
                 patch("click.prompt", patched_prompt):
                result = runner.invoke(
                    refresh,
                    [str(repo)],  # default UX
                    env={"PATH": ""},
                    input="y\nn\n",  # y to proceed, n to merge
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, f"Output:\n{result.output}"
            assert "Would you like me to merge" in result.output
            # The manual-commands panel is shown
            assert "git merge --ff-only sac/refresh/test1234" in result.output
            # The "All done." success line should NOT appear (we
            # printed manual commands, not ran the merge)
            assert "All done." not in result.output
