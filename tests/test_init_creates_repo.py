"""Regression test for: `servicectl init --repo=OWNER/NAME` should
create the GitHub repo as part of init (not require the user to
pre-create the empty repo on github.com and pass --git-remote).

The principle this test pins: the integration test workflow in
SaCScaffolder/sac is a "faithful recreation of the user experience"
(see the comment on `init` in `cli.py`). Whatever a human does with
`servicectl init` to scaffold a service, the workflow must be able to
do with the same single command. Pre-creating the repo separately
breaks that symmetry.

This test mocks the GitHub API so it doesn't actually create a repo.
It verifies:
  1. init POSTs to /orgs/{owner}/repos with the right payload
  2. init then runs `git remote add origin <clone_url>` and `git push -u origin main`
  3. The success panel mentions the created GitHub URL
  4. The auth-fallback chain works: --repo-token > $GH_TOKEN > `gh auth token`
  5. Bad inputs (--repo + --git-remote, --repo + --no-git, --repo-private
     without --repo, malformed --repo) fail with clear messages
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from servicectl.cli import init


# Fake API response that matches the GitHub create-repo response schema.
FAKE_REPO_RESPONSE = {
    "id": 123456789,
    "node_id": "R_kgDOBfgh2Q",
    "name": "my-service",
    "full_name": "SaCScaffolder/my-service",
    "private": False,
    "html_url": "https://github.com/SaCScaffolder/my-service",
    "clone_url": "https://github.com/SaCScaffolder/my-service.git",
    "ssh_url": "git@github.com:SaCScaffolder/my-service.git",
    "default_branch": "main",
    "created_at": "2026-10-12T16:00:00Z",
    "updated_at": "2026-10-12T16:00:00Z",
}


def _run_init(
    *,
    args: list[str],
    env: dict | None = None,
    mock_create=None,
) -> tuple:
    """Invoke `servicectl init` with mocks for the GitHub API and
    subprocess (so we don't actually push to GitHub from CI).

    Returns (click_result, mock_create_or_none, mock_subprocess_or_none).
    Pass an explicit `mock_create` if you want to inspect the call.
    """
    runner = CliRunner()
    full_env = {"PATH": os.environ.get("PATH", "")}
    if env:
        full_env.update(env)
    out = Path(tempfile.mkdtemp()) / "rendered"
    out.mkdir()
    final_args = list(args) + ["--output-dir", str(out)]
    ctx_managers = []
    try:
        if mock_create is None:
            cm = patch("servicectl.github_api.create_org_repo", autospec=True)
        else:
            cm = patch("servicectl.github_api.create_org_repo", mock_create, autospec=True)
        mock_create_obj = cm.__enter__()
        mock_create_obj.return_value = FAKE_REPO_RESPONSE
        ctx_managers.append(cm)

        cm2 = patch("subprocess.run")
        mock_subprocess = cm2.__enter__()
        mock_subprocess.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        ctx_managers.append(cm2)

        # Mock the git-daemon wait so tests don't hit the real
        # GitHub API. Default to "already visible" (returns True)
        # so the happy path doesn't surface a spurious warning.
        cm3 = patch("servicectl.github_api.wait_for_git_daemon", return_value=True)
        cm3.__enter__()
        ctx_managers.append(cm3)

        result = runner.invoke(init, final_args, env=full_env, catch_exceptions=False)
        return result, mock_create_obj, mock_subprocess
    finally:
        for cm in reversed(ctx_managers):
            cm.__exit__(None, None, None)


def test_init_creates_repo_calls_api_with_correct_payload():
    """`--repo` triggers a POST to /orgs/{owner}/repos with the right body."""
    result, mock_create, _ = _run_init(
        args=["my-service", "--template=go-webapi", "--repo", "SaCScaffolder/my-service"],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, (
        f"init exited non-zero. Output:\n{result.output}\nException: {result.exception}"
    )
    # The mock recorded exactly one call to create_org_repo.
    assert mock_create.call_count == 1
    call = mock_create.call_args
    # Positional + keyword args: create_org_repo(owner, name, *, token, ...)
    # The cli does: create_org_repo(owner_str, name_str, token=token, private=...)
    assert call.args[0] == "SaCScaffolder"
    assert call.args[1] == "my-service"
    assert call.kwargs["private"] is False  # public by default
    # The CLI does not pass auto_init or description by default; the
    # helper's defaults (both False) take over. We don't assert those
    # here because the CLI surface for them may change; what matters
    # is that private=False is honored (i.e. --repo-private isn't accidentally
    # defaulting to True).


def test_init_creates_repo_resolves_token_from_gh_token_env():
    """The auth fallback uses $GH_TOKEN when --repo-token is not set."""
    result, mock_create, _ = _run_init(
        args=["my-service", "--template=go-webapi", "--repo", "SaCScaffolder/my-service"],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, result.output
    assert mock_create.call_args.kwargs["token"] == "ghp_test_token_for_unit_test_only_xxxxx"


def test_init_creates_repo_prefers_repo_token_flag_over_env():
    """--repo-token takes precedence over $GH_TOKEN."""
    result, mock_create, _ = _run_init(
        args=[
            "my-service",
            "--template=go-webapi",
            "--repo", "SaCScaffolder/my-service",
            "--repo-token", "ghp_flag_token_wins",
        ],
        env={"GH_TOKEN": "ghp_env_token_loses"},
    )
    assert result.exit_code == 0, result.output
    assert mock_create.call_args.kwargs["token"] == "ghp_flag_token_wins"


def test_init_creates_repo_pushes_to_newly_created_clone_url():
    """After the API returns the new repo's clone_url, init runs
    `git remote add origin <clone_url>` and `git push -u origin main`.
    """
    result, _, mock_subprocess = _run_init(
        args=["my-service", "--template=go-webapi", "--repo", "SaCScaffolder/my-service"],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, f"init exited non-zero. Output:\n{result.output}\nException: {result.exception}"
    # Collect every subprocess.run call's arg list
    all_calls = [c.args[0] for c in mock_subprocess.call_args_list if c.args]
    # Find the git remote add and git push calls
    remote_adds = [c for c in all_calls if c[:3] == ["git", "-C", str(Path(c[2]).resolve())[4:]] or (len(c) >= 4 and c[2].endswith("my-service") and "remote" in c and "add" in c and "origin" in c)]
    # Simpler: check raw arg strings
    arg_strs = [" ".join(map(str, c)) for c in all_calls]
    assert any("remote add origin" in s and "SaCScaffolder/my-service.git" in s for s in arg_strs), (
        f"expected `git remote add origin ...SaCScaffolder/my-service.git`, got:\n{arg_strs}"
    )
    assert any("push" in s and "-u" in s and "origin" in s and "main" in s for s in arg_strs), (
        f"expected `git push -u origin main`, got:\n{arg_strs}"
    )


def test_init_creates_repo_fails_loud_when_no_token():
    """If no token is available, init must fail with a clear remediation
    message (not a stack trace)."""
    from unittest.mock import MagicMock
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "rendered"
        out.mkdir()
        # We mock the generator so the local scaffold + git steps don't
        # try to actually run (and don't need git on PATH). This isolates
        # the test to the auth-resolution path.
        with patch("servicectl.cli.ServiceGenerator") as mock_gen_cls, \
             patch("servicectl.github_api.get_auth_token_from_gh_cli", return_value=None), \
             patch.dict(os.environ, {}, clear=True):
            mock_gen = MagicMock()
            mock_gen.run.return_value = out / "my-service"
            mock_gen_cls.return_value = mock_gen
            result = runner.invoke(
                init,
                [
                    "my-service",
                    "--template=go-webapi",
                    "--output-dir", str(out),
                    "--repo", "SaCScaffolder/my-service",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 2, f"expected exit 2, got {result.exit_code}\n{result.output}"
        assert "no GitHub token available" in result.output, (
            f"expected the 'no GitHub token available' remediation message, got:\n{result.output}"
        )
        assert "--repo-token" in result.output
        assert "GH_TOKEN" in result.output
        assert "gh auth login" in result.output


def test_init_repo_and_git_remote_are_mutually_exclusive():
    """--repo and --git-remote cannot both be set (they describe two
    different ways of getting a remote: create it vs use an existing
    one)."""
    result, _, _ = _run_init(
        args=[
            "my-service",
            "--template=go-webapi",
            "--repo", "SaCScaffolder/my-service",
            "--git-remote", "https://github.com/SaCScaffolder/my-service.git",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_init_repo_requires_git():
    """--repo needs to run git commands (remote add, push), so it must
    not be combined with --no-git."""
    result, _, _ = _run_init(
        args=[
            "my-service",
            "--template=go-webapi",
            "--repo", "SaCScaffolder/my-service",
            "--no-git",
        ],
    )
    assert result.exit_code == 2
    assert "--repo requires git" in result.output


def test_init_repo_private_without_repo_is_rejected():
    """--repo-private is only meaningful with --repo."""
    result, _, _ = _run_init(
        args=["my-service", "--template=go-webapi", "--repo-private"],
    )
    assert result.exit_code == 2
    assert "--repo-private" in result.output and "--repo" in result.output


def test_init_repo_format_must_be_owner_slash_name():
    """--repo must be OWNER/NAME. Bare 'name' or '/name' or 'owner/' are errors."""
    for bad in ["my-service", "/my-service", "SaCScaffolder/"]:
        result, _, _ = _run_init(
            args=["my-service", "--template=go-webapi", "--repo", bad],
            env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
        )
        assert result.exit_code == 2, f"--repo={bad!r} should be rejected but exited {result.exit_code}"


def test_init_repo_success_panel_includes_github_url():
    """The success panel should show the GitHub URL when --repo was used,
    so the user can click through to confirm the repo was created."""
    result, _, _ = _run_init(
        args=["my-service", "--template=go-webapi", "--repo", "SaCScaffolder/my-service"],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, result.output
    assert "https://github.com/SaCScaffolder/my-service" in result.output, (
        f"expected the success panel to include the GitHub URL, got:\n{result.output}"
    )


def test_init_repo_panel_omits_manual_remote_add_hint():
    """When --repo is used, the 'Next steps' hint should NOT tell the
    user to manually `git remote add origin` (we already did that for
    them, and pushed)."""
    result, _, _ = _run_init(
        args=["my-service", "--template=go-webapi", "--repo", "SaCScaffolder/my-service"],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, result.output
    assert "git remote add origin <your-repo-url>" not in result.output, (
        f"--repo should suppress the manual remote-add hint. Output:\n{result.output}"
    )


def test_init_repo_private_flag_makes_repo_private():
    """--repo-private sets private=True in the API call."""
    result, mock_create, _ = _run_init(
        args=[
            "my-service",
            "--template=go-webapi",
            "--repo", "SaCScaffolder/my-service",
            "--repo-private",
        ],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, result.output
    assert mock_create.call_args.kwargs["private"] is True


def test_init_repo_no_push_without_repo_is_rejected():
    """--repo-no-push is only meaningful with --repo (it's the
    suppression flag for the only push that --repo does)."""
    result, _, _ = _run_init(
        args=["my-service", "--template=go-webapi", "--repo-no-push"],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 2, f"expected exit 2, got {result.exit_code}"
    assert "--repo-no-push is only meaningful with --repo" in result.output


def test_init_repo_no_push_skips_git_push():
    """When --repo-no-push is set, init creates the repo and
    adds it as `origin` but does NOT push. The integration test
    workflow path uses this so it can cherry-pick fixture
    commits on top of the SAC baseline before pushing manually.
    """
    result, _, mock_subprocess = _run_init(
        args=[
            "my-service",
            "--template=go-webapi",
            "--repo", "SaCScaffolder/my-service",
            "--repo-no-push",
        ],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, result.output
    # Verify a remote add was called.
    arg_strs = [" ".join(map(str, c.args[0])) for c in mock_subprocess.call_args_list if c.args]
    assert any("remote add origin" in s for s in arg_strs), (
        f"expected `git remote add origin` to be called, got:\n{arg_strs}"
    )
    # Verify no push was called.
    assert not any("push" in s and "origin" in s for s in arg_strs), (
        f"expected NO `git push` to be called, got:\n{arg_strs}"
    )
    # Verify the success panel signals the skip.
    assert "skipped" in result.output, (
        f"expected the skip notice in the success output, got:\n{result.output}"
    )


def test_init_creates_repo_warns_when_git_daemon_times_out():
    """If the git daemon never becomes visible, init surfaces a
    warning (not a fatal error) and proceeds with the push anyway.
    The push may fail, but the warning is the operator's signal
    that something is up."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "rendered"
        out.mkdir()
        with patch("servicectl.github_api.create_org_repo") as mock_create, \
             patch("subprocess.run") as mock_subprocess, \
             patch("servicectl.github_api.wait_for_git_daemon", return_value=False):
            mock_create.return_value = FAKE_REPO_RESPONSE
            mock_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            result = runner.invoke(
                init,
                [
                    "my-service", "--template=go-webapi",
                    "--repo", "SaCScaffolder/my-service",
                    "--output-dir", str(out),
                ],
                env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx",
                     "PATH": os.environ.get("PATH", "")},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "git daemon still not visible" in result.output, (
            f"expected a warning about the git daemon, got:\n{result.output}"
        )


def test_init_creates_repo_writes_askpass_when_no_env_askpass():
    """When GIT_ASKPASS isn't set, init writes a temporary askpass
    script and uses it to authenticate the push. The script is
    cleaned up after. This is the CI-runner path: no credential
    helper, no TTY, the only way to authenticate `git push` is
    ASKPASS. The integration test workflow relies on this behavior
    (it does not set GIT_ASKPASS in the env for the init step)."""
    result, _, mock_subprocess = _run_init(
        args=["my-service", "--template=go-webapi", "--repo", "SaCScaffolder/my-service"],
        env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx"},
    )
    assert result.exit_code == 0, result.output
    # Find the git push call and inspect its env kwarg.
    push_calls = [
        c for c in mock_subprocess.call_args_list
        if c.args and c.args[0] and c.args[0][0:3] == ["git", "-C", c.args[0][2]]
        and "push" in c.args[0]
    ]
    assert len(push_calls) == 1, f"expected exactly one push call, got: {push_calls}"
    push_env = push_calls[0].kwargs.get("env", {})
    assert "GIT_ASKPASS" in push_env, (
        f"expected GIT_ASKPASS in push env, got: {push_env}"
    )
    askpass_path = push_env["GIT_ASKPASS"]
    # The file existed during the push and was deleted after.
    # (We can't assert the file existed at push time because
    # the test runs after the cleanup. We assert the path is
    # cleaned up.)
    assert not Path(askpass_path).exists(), (
        f"askpass file was not cleaned up: {askpass_path}"
    )


def test_init_creates_repo_respects_existing_git_askpass():
    """If GIT_ASKPASS is already in the env, init uses it and does
    NOT write its own. This is the local-dev case where the user
    has configured their own credential helper as a GIT_ASKPASS
    script (e.g. a 1Password wrapper)."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "rendered"
        out.mkdir()
        # The CLI checks os.environ for GIT_ASKPASS (not the click
        # runner's injected env), so we set it via monkeypatch in
        # the test process's env. This models the real-world
        # scenario where the user has GIT_ASKPASS exported in
        # their shell.
        custom_askpass = "/usr/local/bin/my-credential-helper.sh"
        with patch.dict(os.environ, {"GIT_ASKPASS": custom_askpass}, clear=False), \
             patch("servicectl.github_api.create_org_repo") as mock_create, \
             patch("subprocess.run") as mock_subprocess, \
             patch("servicectl.github_api.wait_for_git_daemon", return_value=True):
            mock_create.return_value = FAKE_REPO_RESPONSE
            mock_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            result = runner.invoke(
                init,
                [
                    "my-service", "--template=go-webapi",
                    "--repo", "SaCScaffolder/my-service",
                    "--output-dir", str(out),
                ],
                env={"GH_TOKEN": "ghp_test_token_for_unit_test_only_xxxxx",
                     "PATH": os.environ.get("PATH", "")},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        push_calls = [
            c for c in mock_subprocess.call_args_list
            if c.args and c.args[0] and c.args[0][0:3] == ["git", "-C", c.args[0][2]]
            and "push" in c.args[0]
        ]
        assert len(push_calls) == 1
        push_env = push_calls[0].kwargs.get("env", {})
        assert push_env.get("GIT_ASKPASS") == custom_askpass, (
            f"expected existing GIT_ASKPASS to be preserved, got: {push_env.get('GIT_ASKPASS')!r}"
        )
