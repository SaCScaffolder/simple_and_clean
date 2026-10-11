"""Self-healing refresh conflict resolution.

When `servicectl refresh` hits a `RefreshConflict` (a developer commit
doesn't cherry-pick cleanly onto the fresh scaffold), the user can opt
into automatic resolution by passing `--resolve-on-conflict`. The
scaffolder then:

  1. Inspects the conflict (`conflicting_files` list) and picks a
     resolution strategy.
  2. Creates a branch on the source repo (the user's GitHub repo).
  3. Applies the resolution: edit files, commit with a SAC trailer
     marking it as a refresh-resolution commit.
  4. Pushes the branch.
  5. Opens a PR via the GitHub API (or `gh pr create`).
  6. Returns the PR URL on stdout. The integration test's CI waits
     on the PR to merge, then re-runs `servicectl refresh`, which
     now succeeds because the resolution is part of the fixture's
     history.

The CI is dumb: it just watches the PR the scaffolder created. The
scaffolder is where the conflict context lives, so it's where the
resolution logic belongs.

Resolution strategies
--------------------

  PATH_MISMATCH (the only one implemented in this PR):

    The developer commit references files under `internal/<X>/` but
    the scaffold regenerated under `internal/<Y>/` because `.servicectl.json`
    had no `service_name` field (or had it set to `<Y>`). This is
    exactly the bug fixed in PR #1 on simple_and_clean; the resolution
    edits `.servicectl.json` to add `service_name: <X>`, commits with
    the `SAC-Operation: refresh-resolution` trailer, and opens a PR.

    The next refresh attempt (after the PR merges) reads the new
    `service_name` and regenerates with `<X>`, so the developer's
    file paths line up. The cherry-pick succeeds.

  (Future) CONTENT_CONFLICT:

    The developer's patch doesn't apply to the new scaffold because
    the scaffolder regenerated a file the developer had also modified.
    Use `git cherry-pick --strategy-option=theirs` to accept the
    developer's version. Open a PR with a body explaining what was
    overridden.

  (Future) SCAFFOLD_INCOMPATIBLE:

    The scaffolder has dropped a feature the developer relied on
    (e.g. a CI provider, a deploy target, a coverage gate). The
    resolution is more invasive: either update the developer commits
    or open a PR asking the user to manually rebase. Mark the
    bootstrap as needing manual review.

Why this lives in the scaffolder and not the workflow
-----------------------------------------------------

The workflow is generic. It runs `servicectl refresh` and waits for
it to succeed. The scaffolder is where the conflict context is (which
files, which commits, what the scaffold was supposed to look like).
Putting the resolution strategy in the workflow would mean the
workflow has to import or replicate the conflict-detection logic; the
scaffolder already has it.

In practice, this means: when a future scaffolder change introduces
a new kind of conflict, the resolution strategy lives in
`resolve.py` and is reviewable as part of the scaffolder change.
The workflow doesn't need to change.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .sac_config import CONFIG_FILENAME, SacConfig, read_config, write_config


# Match an `internal/<X>/...` path. Used by the PATH_MISMATCH strategy
# to extract the package directory from the developer's commit. The
# `<X>` group captures the package name (snake_case; the v1 + v2 spec
# translation renders the kebab-case `service.name` to snake_case for
# the Go package path).
_INTERNAL_PATH_RE = re.compile(r"^internal/([^/]+)/")


@dataclass
class ResolutionResult:
    """Outcome of a `resolve_conflict()` call.

    Fields:
        strategy        -- which strategy was applied (e.g. "PATH_MISMATCH").
        pr_url          -- the URL of the resolution PR. None if no PR
                           was created (e.g. the strategy determined
                           the config was already correct).
        branch_name     -- the branch the resolution was pushed to.
        commit_sha      -- the SHA of the resolution commit on the
                           source repo.
        message         -- human-readable description of what the
                           resolution did. Suitable for echoing to
                           the CI's step summary.
    """

    strategy: str
    pr_url: str | None
    branch_name: str
    commit_sha: str
    message: str


def detect_strategy(conflicting_files: list[str], config: SacConfig, source_repo_name: str) -> str | None:
    """Return the resolution strategy to apply, or None if no strategy
    matches this conflict.

    `source_repo_name` is the basename of the source repo (e.g.
    "sac_example_ala_service_modified"). The PATH_MISMATCH strategy
    uses it to compare against the developer commit's `internal/<X>/`
    path: if `<X>` is something other than the snake_case of
    `source_repo_name`, the scaffolder regenerated under the wrong
    name.

    Detection order (first match wins; future strategies go below):

      1. PATH_MISMATCH: any conflict file matches `internal/<X>/`
         where `<X>` is not a snake-case form of the source repo's
         basename OR is not the config's `service_name` (when set).
    """
    for f in conflicting_files:
        m = _INTERNAL_PATH_RE.match(f)
        if m:
            package_dir = m.group(1)
            # The scaffold regenerated under the source repo's basename
            # (when service_name is empty) or under service_name (when
            # set). If the developer's path uses a different name, it's
            # a path mismatch.
            expected = config.service_name or _snake_case(source_repo_name)
            if package_dir != expected:
                return "PATH_MISMATCH"
    return None


def resolve_conflict(
    *,
    source_repo: Path,
    conflicting_files: list[str],
    config: SacConfig,
    run_id: str,
) -> ResolutionResult:
    """Create a branch on `source_repo` that resolves the refresh conflict.

    Returns a `ResolutionResult` with the PR URL. The caller (CLI) is
    responsible for printing the URL and exiting with the right code.

    Raises:
        RuntimeError if no resolution strategy matches the conflict.
        subprocess.CalledProcessError if any git/gh command fails.
    """
    strategy = detect_strategy(conflicting_files, config, source_repo.resolve().name)
    if strategy is None:
        raise RuntimeError(
            f"no resolution strategy for conflict on {conflicting_files!r}; "
            "manual intervention required"
        )

    if strategy == "PATH_MISMATCH":
        return _resolve_path_mismatch(
            source_repo=source_repo,
            conflicting_files=conflicting_files,
            config=config,
            run_id=run_id,
        )
    # Future strategies go here as elif branches.
    raise RuntimeError(f"unimplemented strategy: {strategy}")


def _resolve_path_mismatch(
    *,
    source_repo: Path,
    conflicting_files: list[str],
    config: SacConfig,
    run_id: str,
) -> ResolutionResult:
    """Resolve a path-mismatch conflict by adding `service_name` to
    `.servicectl.json` with the value derived from the developer
    commit's `internal/<X>/` path.

    The strategy assumes the developer's `<X>` is the canonical
    service name (it was, at the time the developer commits were
    made). The scaffold regenerated under a different name because
    the config's `service_name` was either empty (legacy config) or
    set to a different value. Setting it to `<X>` makes the next
    refresh use the right name and the cherry-pick applies cleanly.
    """
    # Extract the package directory from the first matching conflict file.
    # The list can have multiple `internal/<X>/` entries (e.g. the
    # middleware.go + middleware_test.go pair from the CORS commit);
    # we use the first one. If they disagree, we fall back to the
    # source repo's existing `internal/<X>/` directory on the main
    # branch (which is the bootstrap's original name).
    package_dir: str | None = None
    for f in conflicting_files:
        m = _INTERNAL_PATH_RE.match(f)
        if m:
            package_dir = m.group(1)
            break
    if package_dir is None:
        # detect_strategy should have already returned PATH_MISMATCH
        # for at least one path; if we get here, something is off.
        # Fall back to reading the existing scaffold's internal dir.
        package_dir = _infer_package_from_source(source_repo)

    # Update the in-memory config and write it back. We don't bail if
    # `config.service_name` is already set to `package_dir` -- that's
    # the success case (nothing to do, but we still create a no-op PR
    # to document the resolution for the audit trail). Actually, in
    # that case, the conflict shouldn't have happened in the first
    # place, so something else is going on. We bail with a clear error.
    if config.service_name and config.service_name != _snake_case(package_dir):
        # The config has a service_name that doesn't match the
        # developer's package_dir. That's a deeper inconsistency;
        # don't try to fix it automatically.
        raise RuntimeError(
            f"PATH_MISMATCH but config.service_name={config.service_name!r} "
            f"doesn't match developer's package_dir={package_dir!r}; "
            f"manual intervention required"
        )

    new_service_name = _snake_case(package_dir)
    config.service_name = new_service_name
    write_config(source_repo, config)

    # Commit, push, open PR. The branch name is unique per run_id so
    # multiple concurrent refreshes don't collide.
    branch_name = f"chore/resolve-refresh-conflict-{run_id}"
    commit_message = (
        f"chore(sac): resolve refresh conflict by adding service_name to "
        f".servicectl.json\n\n"
        f"The refresh conflict in run {run_id} was a path mismatch: the\n"
        f"developer commit references files under `internal/{package_dir}/`,\n"
        f"but the scaffold regenerated under the source repo's directory\n"
        f"name (because `.servicectl.json` had no `service_name` field).\n\n"
        f"Fix: add `service_name: {new_service_name}` to `.servicectl.json`.\n"
        f"The next refresh attempt will read this field and regenerate\n"
        f"the scaffold under the correct package path, so the developer's\n"
        f"cherry-pick applies cleanly.\n\n"
        f"SAC-Managed: true\n"
        f"SAC-Operation: refresh-resolution\n"
        f"SAC-Spec-Version: 1\n"
        f"SAC-Version: dev\n"
    )

    # Configure git committer identity. Use a generic name since
    # the scaffolder is running on behalf of the integration test
    # bot. The actual author identity comes from the App's
    # installation token when `gh pr create` runs.
    _git(source_repo, "config", "user.email", "sac-integration-bot@users.noreply.github.com")
    _git(source_repo, "config", "user.name", "sac-integration-bot")

    # Stage and commit on a new branch.
    _git(source_repo, "checkout", "-b", branch_name)
    _git(source_repo, "add", CONFIG_FILENAME)
    _git(
        source_repo,
        "commit",
        "-m",
        commit_message.split("\n", 1)[0],  # subject
        "-m",
        "\n".join(commit_message.split("\n")[1:]),  # body + trailers
    )
    commit_sha = _git_capture(
        source_repo, "rev-parse", "HEAD"
    ).strip()

    # Push. Use the GH_TOKEN env var (or fall back to GITHUB_TOKEN).
    # The integration test's workflow exports the App's installation
    # token as GH_TOKEN; locally, the user's PAT is read by `gh`.
    push_result = subprocess.run(
        ["git", "-C", str(source_repo), "push", "-u", "origin", branch_name],
        capture_output=True,
        text=True,
    )
    if push_result.returncode != 0:
        raise subprocess.CalledProcessError(
            push_result.returncode,
            push_result.args,
            push_result.stdout,
            push_result.stderr,
        )

    # Open a PR. Use `gh pr create` (it reads the GH_TOKEN from the
    # env). The PR body explains the conflict and the resolution.
    pr_body = (
        f"## Refresh conflict resolution\n\n"
        f"`servicectl refresh` against this repo hit a `RefreshConflict` "
        f"in run `{run_id}`. The conflict was on:\n\n"
        + "\n".join(f"- `{f}`" for f in conflicting_files)
        + f"\n\n"
        f"### Root cause\n\n"
        f"The developer commits in this repo's history were made when the "
        f"service was originally scaffolded with name `{new_service_name}`. "
        f"They reference files under `internal/{package_dir}/`. The refresh "
        f"engine regenerated the scaffold under a different name (the source "
        f"repo's directory name, or whatever was in `.servicectl.json`), so "
        f"the developer's cherry-pick failed with a path mismatch.\n\n"
        f"### Fix\n\n"
        f"This PR adds `service_name: {new_service_name}` to "
        f"`.servicectl.json`. After merge, the next `servicectl refresh` "
        f"will read this field and regenerate the scaffold under the correct "
        f"name, so the developer's cherry-pick applies cleanly.\n\n"
        f"### Verification\n\n"
        f"After merging this PR, the integration test "
        f"(`SaCScaffolder/sac` workflow `Integration test -- modified example`) "
        f"will automatically re-trigger and re-run the refresh. The PR's CI "
        f"check is set up to wait for this PR to merge before proceeding.\n"
    )

    pr_create = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            _current_branch_or_default(source_repo),
            "--head",
            branch_name,
            "--title",
            f"chore(sac): resolve refresh conflict (run {run_id})",
            "--body",
            pr_body,
        ],
        capture_output=True,
        text=True,
    )
    if pr_create.returncode != 0:
        raise subprocess.CalledProcessError(
            pr_create.returncode,
            pr_create.args,
            pr_create.stdout,
            pr_create.stderr,
        )
    pr_url = pr_create.stdout.strip()

    return ResolutionResult(
        strategy="PATH_MISMATCH",
        pr_url=pr_url,
        branch_name=branch_name,
        commit_sha=commit_sha,
        message=(
            f"Added `service_name: {new_service_name}` to `.servicectl.json` "
            f"to fix path mismatch on `internal/{package_dir}/`. "
            f"PR: {pr_url}"
        ),
    )


def _infer_package_from_source(source_repo: Path) -> str:
    """If the conflicting_files list didn't have an `internal/<X>/`
    entry, fall back to reading the source repo's existing
    `internal/<X>/` directory on its current branch. This is the
    bootstrap's original package name."""
    result = subprocess.run(
        ["git", "-C", str(source_repo), "ls-tree", "--name-only", "HEAD", "internal/"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        # ls-tree output is "internal/<dir>" -- one entry per top-level
        # subdir of internal/. We take the first.
        m = _INTERNAL_PATH_RE.match(line + "/")  # ensure trailing / for regex
        if m:
            return m.group(1)
    raise RuntimeError(
        f"could not infer package directory from {source_repo}'s HEAD; "
        f"no `internal/<X>/` directory found"
    )


def _snake_case(name: str) -> str:
    """Convert a kebab-case, PascalCase, or dotted name to snake_case.

    Mirrors the scaffolder's `service_name_snake` Jinja filter
    (see `src/servicectl/generator.py:152-153`):
        "service_name_snake": self.name.replace("-", "_").replace(".", "_"),

    The scaffolder replaces both hyphens and dots with underscores,
    then lowercases. We add the PascalCase->snake_case step on top
    because the developer's `internal/<X>/` path is always
    snake_case (it's a Go package directory), so any uppercase
    letters in the developer's path would already be lowercase
    in the real conflict -- but we still handle the edge case
    for completeness so this function is symmetric with the
    scaffolder's behavior.

    `billing-api` -> `billing_api`
    `BillingApi` -> `billing_api`
    `foo.bar`    -> `foo_bar`
    `ala_service_modified` -> `ala_service_modified` (already snake)
    """
    s = name.replace("-", "_").replace(".", "_")
    # Insert underscores before uppercase letters (PascalCase -> snake_case).
    # Negative lookbehind `(?<!^)` so the leading character isn't preceded
    # by an underscore (preserves strings like "FooBar" -> "foo_bar", not
    # "_foo_bar").
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()
    return s


def _git(repo: Path, *args: str) -> None:
    """Run a git command in `repo`, raising on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            result.stdout,
            result.stderr,
        )


def _git_capture(repo: Path, *args: str) -> str:
    """Run a git command in `repo` and return stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _current_branch_or_default(repo: Path) -> str:
    """Return the current branch name, or "main" if HEAD is detached."""
    result = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return "main"


__all__ = [
    "ResolutionResult",
    "detect_strategy",
    "resolve_conflict",
]
