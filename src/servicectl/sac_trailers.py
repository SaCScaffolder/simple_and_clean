"""SAC-Managed trailer system.

This module is the foundation of the Modified Fixture Re-Scaffolding feature
(issue #44 / sub-tasks #45-#48). It establishes a small, opinionated
git-trailer schema that every servicectl operation tags its commits with,
so a later `servicectl refresh` can distinguish SAC-managed commits from
user/developer commits when replaying history onto a fresh scaffold.

Trailer schema
--------------

Every commit produced by a servicectl operation carries:

    SAC-Managed: <true|false>
    SAC-Operation: <scaffold|modify|update>
    SAC-Spec-Version: <int>
    SAC-Version: <semver>

  - SAC-Managed=true   -- the commit was produced by servicectl. The
                          refresh engine filters these out and rebuilds
                          them from the latest spec.
  - SAC-Managed=false  -- the commit is from the user/developer and the
                          refresh engine cherry-picks it onto the new
                          scaffold.
  - SAC-Operation      -- which kind of SAC action produced this commit.
                          Used for diagnostic output ("this update came
                          from a modify invocation, not a scaffold").
  - SAC-Spec-Version   -- which major spec version the commit was made
                          under. Lets us evolve the schema and run
                          refreshes against the right generation.
  - SAC-Version        -- exact servicectl release that produced the
                          commit. Useful for re-running refreshes with
                          a pinned version.

The trailers are written by servicectl itself; user/developer commits
should not have these trailers (they're SAC's namespace). The classifier
relies on trailer *absence* to mean "developer."
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


# Current schema version. Bump when the trailer schema changes in a way
# that's not backwards-compatible with older commits (e.g. removing or
# renaming a trailer). Read commits' SAC-Spec-Version to detect
# legacy / re-version-required commits during refresh.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SacTrailers:
    """The four trailers servicectl writes on every managed commit."""

    operation: str  # "scaffold" | "modify" | "update"
    spec_version: int = SCHEMA_VERSION
    version: str = ""  # servicectl release, e.g. "0.1.0"
    managed: bool = True

    def render(self) -> str:
        """Return the trailer block as a string, suitable for a git commit body.

        Format:
            SAC-Managed: true
            SAC-Operation: scaffold
            SAC-Spec-Version: 1
            SAC-Version: 0.1.0

        Each trailer on its own line. Caller is responsible for the
        blank line separating the trailer block from the commit subject.
        """
        return "\n".join(
            [
                f"SAC-Managed: {'true' if self.managed else 'false'}",
                f"SAC-Operation: {self.operation}",
                f"SAC-Spec-Version: {self.spec_version}",
                f"SAC-Version: {self.version}",
            ]
        )


def commit_message_with_trailers(subject: str, trailers: SacTrailers) -> list[str]:
    """Build a git commit -m argument list with subject + trailer block.

    git commit treats each -m as a paragraph separated by blank lines.
    Subject is the first paragraph; the trailer block is the second,
    which is exactly the format git log --format=%(trailers) parses.

    Returns two strings: the subject and a string with the trailers
    separated by newlines (no extra blank line; git adds the blank
    line between paragraphs automatically).

    Example:
        >>> commit_message_with_trailers(
        ...     "Initial scaffold from servicectl",
        ...     SacTrailers(operation="scaffold", version="0.1.0"),
        ... )
        ["Initial scaffold from servicectl",
         "SAC-Managed: true\\nSAC-Operation: scaffold\\n..."]
    """
    return [subject, trailers.render()]


# ----------------------------- Reading trailers -----------------------------


def read_trailer(repo: Path, sha: str, key: str) -> str:
    """Read a single SAC trailer value from a commit.

    Uses git log --format=%(trailers:key=<KEY>,valueonly) which prints
    the value (with no key prefix) or empty if the trailer is absent.

    The format placeholders have to be Git-fetched from the local repo,
    so callers must pass a real git working directory.
    """
    placeholder = f"%(trailers:key={key},valueonly)"
    result = subprocess.run(
        ["git", "-C", str(repo), "log", f"--format={placeholder}", "-n", "1", sha],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def read_trailers(repo: Path, sha: str) -> dict[str, str]:
    """Read all SAC trailers from a single commit.

    Returns a dict mapping TRAILER-NAME (uppercase, hyphenated) to value.
    Only SAC-* trailers are returned; unrelated trailers like
    Signed-off-by, Co-authored-by, etc. are ignored.
    """
    keys = ["SAC-Managed", "SAC-Operation", "SAC-Spec-Version", "SAC-Version"]
    return {k: read_trailer(repo, sha, k) for k in keys}


def is_sac_managed(repo: Path, sha: str) -> bool:
    """True iff this commit's SAC-Managed trailer is exactly 'true'.

    Untagged commits (the user/developer case) are NOT SAC-managed,
    even if they have other SAC-* trailers (defensive: SAC-Version
    alone doesn't mean SAC).
    """
    return read_trailer(repo, sha, "SAC-Managed").lower() == "true"


def _ancestors_of(repo: Path, sha: str) -> set[str]:
    """Return the set of SHAs reachable from `sha` (i.e., `sha` and its ancestors).

    Uses `git rev-list` (not log) so we get the full reachability set.
    Empty set on empty stdout (which shouldn't happen for a valid SHA
    in a valid repo).
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-list", sha],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line.strip()}


def classify_history(
    repo: str,
    sac_base_commit: str | None = None,
) -> dict[str, list[str]]:
    """Walk git history newest-first and bucket commits by SAC vs developer.

    Returns:
        {"sac": [<sha>, ...], "developer": [<sha>, ...]}

    Order is preserved within each bucket (newest first). Used by
    servicectl refresh to know which commits to filter out and
    which to cherry-pick.

    Uses git log rather than git rev-list so we get the parent chain
    in one shell-out. --first-parent is intentionally NOT used: we
    want every commit, including those reachable only via merge.

    Legacy-migration mode:
        If `sac_base_commit` is set (a SHA), commits reachable from
        that SHA (i.e., the SHA itself and its ancestors) are treated
        as SAC-managed regardless of whether they carry the trailer.
        Anything newer than `sac_base_commit` falls through to the
        normal trailer-based classification. Use this for repos that
        were scaffolded before SAC trailers shipped and haven't been
        retroactively tagged.

    Pre-condition: repo is a git working directory. Raises
    subprocess.CalledProcessError if it isn't.

    Raises:
        ValueError if sac_base_commit is set but the SHA can't be
        resolved in `repo` (likely a typo or an unrelated repo).
    """
    repo_path = Path(repo)

    # Resolve the legacy anchor up front so a bad SHA fails fast with a
    # clear message rather than half-classifying everything.
    sac_ancestors: set[str] = set()
    if sac_base_commit:
        # Resolve the SHA first so we get a clean error on a typo.
        resolved = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", sac_base_commit + "^{commit}"],
            capture_output=True,
            text=True,
        )
        if resolved.returncode != 0:
            raise ValueError(
                f"SAC_BASE_COMMIT={sac_base_commit!r} does not resolve in {repo}: "
                f"{resolved.stderr.strip() or resolved.stdout.strip()}"
            )
        resolved_sha = resolved.stdout.strip()
        sac_ancestors = _ancestors_of(repo_path, resolved_sha)

    result = subprocess.run(
        ["git", "-C", repo, "log", "--format=%H"],
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, list[str]] = {"sac": [], "developer": []}
    for line in result.stdout.splitlines():
        sha = line.strip()
        if not sha:
            continue
        if sha in sac_ancestors:
            bucket = "sac"
        elif is_sac_managed(repo_path, sha):
            bucket = "sac"
        else:
            bucket = "developer"
        out[bucket].append(sha)
    return out


