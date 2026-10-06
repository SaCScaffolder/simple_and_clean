"""Cherry-pick replay engine for servicectl refresh.

This is the engine that powers `servicectl refresh <repo>` -- the
feature that lets a developer evolve simple_and_clean without losing
the application-specific commits they've made on top of an old
scaffold. See SAC_MODIFIED_FIXTURE_REFRESH.md for the full design.

Flow (driven by servicectl/cli.py's `refresh` command):

    classify_history(repo)         # already in sac_trailers.py
            |
            v
    create_worktree(refresh_run_id)   # isolated git worktree on a fresh branch
            |
            v
    generate fresh scaffold (via ServiceGenerator.run) -- creates SAC-managed commits
            |
            v
    for each developer commit SHA in chronological order:
        git cherry-pick <sha>
        on conflict:
            abort, report list of conflicting files,
            keep recovery ref so user can recover
            raise RefreshConflict
            |
            v
    on full success:
        doctor + tests pass -> promote worktree to a stable branch
        drop recovery ref
    on any failure:
        leave recovery ref in place, surface diagnostics to user

The module is intentionally stateful about the run-id and recovery
ref name so callers can inspect them after a refresh attempt.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .sac_trailers import classify_history, is_sac_managed
from .sac_config import SacConfig, read_config, resolve_sac_base_commit, CONFIG_FILENAME


# ----- errors -----


class RefreshError(Exception):
    """Base class for refresh-time failures."""


class RefreshConflict(RefreshError):
    """A developer commit did not cherry-pick cleanly onto the fresh scaffold.

    The refresh leaves the worktree in a conflicted state and keeps a
    recovery ref so the user can resolve manually. The exception
    `conflicting_files` is the parsed list from `git cherry-pick` output.
    """

    def __init__(self, conflicting_files: list[str], recovery_ref: str):
        self.conflicting_files = conflicting_files
        self.recovery_ref = recovery_ref
        super().__init__(
            f"cherry-pick conflict on {len(conflicting_files)} file(s); "
            f"recovery ref: {recovery_ref}"
        )


class RefreshNotScaffoldedError(RefreshError):
    """The target repo has no SAC-managed commits to refresh against.

    Either the repo was never scaffolded by servicectl, or its history
    is all developer commits. Run `servicectl init` on a fresh
    directory, then `servicectl refresh --all-modified` after each
    scaffold edit.
    """


class RefreshConfigMissing(RefreshError):
    """The repo's `.servicectl.json` (the SAC config file) is missing.

    Refresh needs to know which template, deploy target, registry, etc.
    produced the service to regenerate the scaffold against the latest
    simple_and_clean release. Without the config it would have to guess,
    and a wrong guess would silently change the service.

    Mitigation: regenerate from a known template/flags via `servicectl
    init --from-config <path>` and commit the resulting `.servicectl.json`.
    Or pass `--from-config` to `servicectl refresh` pointing at a
    manually-curated JSON config.
    """


# ----- run state -----


@dataclass
class RefreshRun:
    """State for one refresh attempt.

    Fields:
        run_id          -- short random ID used to name the worktree branch
                         and recovery ref.
        source_repo     -- the user's modified repo (the one being refreshed).
        worktree_dir    -- path to the temp worktree where the refresh happens.
        recovery_ref    -- name of the ref users can run `git checkout` to
                         to recover the pre-refresh state.
        base_branch     -- branch the worktree branched off of; defaults to
                         source_repo's current branch.
        scaffold_sha    -- SHA of the latest scaffold commit on the worktree
                         (the initial commit of the fresh scaffold).
        developer_commits -- list of SHAs in chronological (newest-first)
                         order that the engine replays.
        conflicts       -- populated if RefreshConflict is raised.
        config          -- the SacConfig used to regenerate the scaffold
                         (None in dry-run if no .servicectl.json was found,
                         though RefreshConfigMissing will fire first in
                         a real refresh attempt).
        succeeded      -- True iff all developer commits replayed cleanly
                         and doctor/tests passed.
    """

    run_id: str
    source_repo: Path
    worktree_dir: Path
    recovery_ref: str
    base_branch: str
    scaffold_sha: str = ""
    conflicts: list[str] = field(default_factory=list)
    succeeded: bool = False
    developer_commits: list[str] = field(default_factory=list)
    config: SacConfig | None = None


def new_run_id() -> str:
    """Generate a short unique ID for naming the worktree / recovery ref.

    Eight hex chars = ~4B values; collision is rare enough that we
    don't bother checking. If two refreshes run concurrently and the
    IDs collide, the second one will see the first's recovery ref and
    refuse to start -- see refresh() for the check.
    """
    return uuid.uuid4().hex[:8]


def current_branch(repo: Path) -> str:
    """Return the current branch name. Errors out if HEAD is detached."""
    result = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ----- worktree -----


def create_worktree(
    source_repo: Path,
    branch_name: str,
    worktree_dir: Path,
) -> Path:
    """Create a detached-ish worktree at worktree_dir on a new branch.

    The worktree branches off source_repo's current HEAD so we have the
    developer's commits available.

    git worktree add -b <branch> <path> <start> creates a new branch at <start>
    and a working directory at <path> checked out to that branch.
    """
    subprocess.run(
        [
            "git", "-C", str(source_repo),
            "worktree", "add", "-b", branch_name, str(worktree_dir),
        ],
        check=True, capture_output=True,
    )
    return worktree_dir


def remove_worktree(repo: Path, worktree_dir: Path, force: bool = False) -> None:
    """Remove the worktree. No-op if it doesn't exist.

    Pass force=True to remove even when the worktree has uncommitted
    changes (the refresh has dropped this state already, but be sure).
    """
    args = ["git", "-C", str(repo), "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_dir))
    subprocess.run(args, capture_output=True)  # not check=True: missing is fine


# ----- cherry-pick -----


_CHERRY_PICK_CONFLICT_MARKERS = ("CONFLICT", "conflict")


def _parse_conflicting_files(output: str) -> list[str]:
    """Pull the list of conflicting files from `git cherry-pick` output.

    git cherry-pick output on conflict looks like:
        CONFLICT (content): Merge conflict in cmd/server/main.go
        CONFLICT (content): Merge conflict in internal/foo/handler.go
        error: could not apply ...

    We extract everything after "CONFLICT (content): Merge conflict in "
    or "CONFLICT (modify/delete|...): ..." on each line. Falls back to
    a regex on the line if the format is non-standard.
    """
    conflicts: list[str] = []
    for line in output.splitlines():
        for marker in _CHERRY_PICK_CONFLICT_MARKERS:
            if marker in line:
                # crude parse: take whatever comes after the marker
                idx = line.find(marker)
                tail = line[idx + len(marker):].lstrip(" ():")
                # tail looks like "Merge conflict in <path>" or "modify/delete in <path>"
                if " in " in tail:
                    conflicts.append(tail.split(" in ", 1)[1].strip())
                else:
                    conflicts.append(tail)
                break
    return conflicts


def cherry_pick(repo: Path, sha: str, recovery_ref: str = "") -> None:
    """Cherry-pick <sha> onto repo's current branch. Raises RefreshConflict
    if the pick fails with conflicts; raises subprocess.CalledProcessError
    for any other error (e.g. SHA not found, no parent).

    recovery_ref is included on the RefreshConflict so callers don't
    have to plumb it through.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "cherry-pick", "--no-commit", sha],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return
    # Distinguish conflict from other failures by scanning output.
    conflicting = _parse_conflicting_files(result.stdout + "\n" + result.stderr)
    if conflicting:
        raise RefreshConflict(conflicting, recovery_ref=recovery_ref)
    # Some other failure (missing SHA, etc.). Let the caller see the raw output.
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


# ----- the main refresh flow -----


def refresh(source_repo: Path, dry_run: bool = False) -> RefreshRun:
    """Run a refresh against `source_repo`.

    Steps:
        1. Identify the developer commit list (chronological, newest-first).
        2. Read `.servicectl.json` to recover the scaffold inputs (template,
           deploy target, registry, etc.). RefreshConfigMissing if absent.
        3. Create a worktree on a fresh branch off source_repo's current HEAD.
        4. Wipe the worktree contents (except .git/) and regenerate the
           scaffold using ServiceGenerator.run(in_place=True) against the
           current simple_and_clean release. This is the bit that picks
           up new templates, deploy overlays, hooks, etc.
        5. Cherry-pick each developer commit in chronological order.
           On conflict: abort the refresh, leave recovery ref intact,
           raise with conflicting_files + recovery_ref.
        6. On success, mark succeeded=True. The CLI is responsible for
           deciding whether to fast-forward source_repo or just report
           the branch as ready to merge.

    If dry_run=True, build the RefreshRun with the developer-commits
    list and a synthesized scaffold_sha (HEAD of source_repo's current
    branch) but don't actually create a worktree or cherry-pick. The
    config is still read so the dry-run report can show what would be
    regenerated.

    Raises:
        RefreshNotScaffoldedError if the repo has no SAC-managed commits.
        RefreshConfigMissing if .servicectl.json is missing.
        RefreshConflict on cherry-pick conflict.
    """
    # First, peek at the SAC config for the legacy anchor. We don't fail
    # if it's missing -- the legacy anchor is optional. We do need the
    # SAC_BASE_COMMIT field (or env var) before we can classify correctly.
    sac_base_commit: str | None = None
    config: SacConfig | None = None
    try:
        config = read_config(source_repo)
        sac_base_commit = resolve_sac_base_commit(config)
    except FileNotFoundError:
        # No .servicectl.json. We can still try classification with no
        # legacy anchor; if the repo carries SAC trailers natively,
        # we proceed (and the missing-config error fires below). If it
        # doesn't, we report RefreshNotScaffoldedError first.
        pass

    # Classify history. Errors from a bad SAC_BASE_COMMIT SHA surface
    # as RefreshError so the CLI can convert them to a clean message.
    try:
        history = classify_history(source_repo, sac_base_commit=sac_base_commit)
    except ValueError as e:
        raise RefreshError(str(e)) from e
    if not history["sac"]:
        raise RefreshNotScaffoldedError(
            f"{source_repo} has no SAC-managed commits; "
            "was it scaffolded by servicectl?"
        )
    developer_commits = history["developer"]

    # Now confirm the config is present. We don't bail above because a
    # repo with SAC trailers but no .servicectl.json should still
    # report "not scaffolded" before "missing config."
    if config is None:
        raise RefreshConfigMissing(
            f"{source_repo / CONFIG_FILENAME} not found. "
            "Refresh needs the SAC config to know which template/flags "
            "produced this service. Re-scaffold via `servicectl init --from-config`, "
            "or pass `--from-config <path>` to refresh."
        )

    run_id = new_run_id()
    branch_name = f"sac/refresh/{run_id}"
    recovery_ref = f"refs/sac/pre-refresh-{run_id}"
    base_branch = current_branch(source_repo)

    run = RefreshRun(
        run_id=run_id,
        source_repo=source_repo,
        worktree_dir=source_repo.parent / f"sac-refresh-{run_id}",
        recovery_ref=recovery_ref,
        base_branch=base_branch,
        developer_commits=developer_commits,
        config=config,
    )

    if dry_run:
        # Synthesize a scaffold SHA for the dry-run plan output.
        # Use HEAD's SHA -- the dry-run report can show the
        # developer's commits are "would be replayed onto HEAD".
        head_sha = subprocess.run(
            ["git", "-C", str(source_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        run.scaffold_sha = head_sha
        return run

    # Real refresh.
    create_worktree(source_repo, branch_name, run.worktree_dir)
    # Save the recovery ref pointing at the worktree's tip BEFORE any
    # replay happens, so the user can always `git checkout sac/pre-refresh-<id>`
    # to recover.
    subprocess.run(
        ["git", "-C", str(source_repo), "update-ref", recovery_ref, base_branch],
        check=True,
    )

    try:
        # Regenerate the scaffold inside the worktree. Wipe all
        # existing files except .git/ (which carries the worktree's
        # git metadata), then run ServiceGenerator.run(in_place=True)
        # so the fresh template tree lands at run.worktree_dir.
        _regenerate_scaffold(run.worktree_dir, config)

        # Read the new scaffold commit's SHA for the run state.
        new_head = subprocess.run(
            ["git", "-C", str(run.worktree_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        run.scaffold_sha = new_head

        for sha in developer_commits:
            try:
                cherry_pick(run.worktree_dir, sha, recovery_ref=recovery_ref)
            except RefreshConflict as c:
                run.conflicts = c.conflicting_files
                raise
        run.succeeded = True
    finally:
        # Note: we DO NOT remove the worktree here even on success --
        # the CLI decides whether to merge/fast-forward or surface the
        # branch to the user. Only on error do we leave it; the caller
        # is responsible for cleanup.
        pass

    return run


def _regenerate_scaffold(worktree_dir: Path, config: SacConfig) -> None:
    """Wipe worktree_dir contents (except .git/) and re-scaffold it.

    Import-local to avoid a circular import: generator.py imports from
    sac_trailers (which is fine) but the reverse isn't safe in some
    test setups that patch the package. Keeping this here means the
    import is lazy and only triggers when refresh() actually runs.

    ServiceGenerator with --in-place writes the scaffold tree directly
    into output_dir (no <name> subfolder). We pass the worktree itself
    as output_dir so the regenerated files land inside the worktree,
    not its parent.
    """
    # Import lazily so generator -> sac_trailers -> replay -> generator
    # doesn't form a cycle at module-load time.
    from .generator import ServiceGenerator

    # Wipe everything except .git/. shutil.rmtree would nuke git too.
    for entry in list(worktree_dir.iterdir()):
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    # Pull the service name from the worktree basename; the regenerated
    # scaffold needs to know "billing-api" not "sac-refresh-abc12345".
    # We do this by passing a fresh output_dir (parent of the worktree)
    # and using --in-place, so the result lands at worktree_dir itself.
    ServiceGenerator(
        name=worktree_dir.name,
        output_dir=worktree_dir,
        in_place=True,
        with_git=True,
        with_readme=True,
        **config.to_generator_kwargs(),
    ).run()