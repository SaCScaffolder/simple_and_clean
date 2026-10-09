"""Tests for SAC trailer system (#45).

Exercises SacTrailers, commit_message_with_trailers, and read_trailer
against a real git repo. The git operations are real (not mocked) so
the trailer format matches what servicectl will actually write.

Coverage goal: 80%+ on src/servicectl/sac_trailers.py.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from servicectl.sac_trailers import (
    SCHEMA_VERSION,
    SacTrailers,
    classify_history,
    commit_message_with_trailers,
    is_sac_managed,
    read_trailer,
    read_trailers,
)


def _commit(repo: Path, message_lines: list[str], file_content: str = "x") -> str:
    """Helper: write a file, return the resulting commit SHA."""
    testfile = repo / "test.txt"
    testfile.write_text(file_content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    cmd = ["git", "-C", str(repo), "commit", "-q"]
    for line in message_lines:
        cmd += ["-m", line]
    subprocess.run(cmd, check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(tmp: Path) -> Path:
    """Helper: init a repo with one empty commit so HEAD exists."""
    repo = tmp / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.com",
         "-c", "user.name=Test", "commit", "--allow-empty", "-q", "-m", "initial"],
        check=True,
    )
    return repo


class SacTrailersRenderTests(unittest.TestCase):
    """Unit tests for the data class itself."""

    def test_default_construction(self):
        t = SacTrailers(operation="scaffold")
        self.assertTrue(t.managed)
        self.assertEqual(t.operation, "scaffold")
        self.assertEqual(t.spec_version, SCHEMA_VERSION)
        self.assertEqual(t.version, "")

    def test_render_emits_all_four_trailers(self):
        t = SacTrailers(operation="scaffold", version="0.1.0")
        rendered = t.render()
        self.assertIn("SAC-Managed: true", rendered)
        self.assertIn("SAC-Operation: scaffold", rendered)
        self.assertIn(f"SAC-Spec-Version: {SCHEMA_VERSION}", rendered)
        self.assertIn("SAC-Version: 0.1.0", rendered)

    def test_render_managed_false(self):
        t = SacTrailers(operation="modify", managed=False)
        rendered = t.render()
        self.assertIn("SAC-Managed: false", rendered)

    def test_render_is_blocked(self):
        """Trailer block has 4 lines (one per trailer)."""
        t = SacTrailers(operation="scaffold")
        rendered = t.render()
        self.assertEqual(rendered.count("\n"), 3)
        self.assertEqual(len(rendered.splitlines()), 4)


class CommitMessageHelpersTests(unittest.TestCase):
    """Unit tests for commit_message_with_trailers."""

    def test_returns_two_string_paragraphs(self):
        t = SacTrailers(operation="scaffold", version="0.1.0")
        parts = commit_message_with_trailers("Initial scaffold", t)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "Initial scaffold")
        self.assertIn("SAC-Operation: scaffold", parts[1])


class GitIntegrationTests(unittest.TestCase):
    """End-to-end tests against a real git repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_commit_with_trailers_round_trips(self):
        """A commit with SAC trailers reports them via read_trailers."""
        t = SacTrailers(operation="scaffold", version="0.1.0")
        subject, trailers = commit_message_with_trailers("Initial scaffold from servicectl", t)
        sha = _commit(self.repo, [subject, trailers], file_content="hello\n")

        self.assertTrue(is_sac_managed(self.repo, sha))
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Managed"), "true")
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Operation"), "scaffold")
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Spec-Version"), str(SCHEMA_VERSION))
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Version"), "0.1.0")

    def test_unrelated_trailers_are_ignored(self):
        """Commits without SAC-* trailers are NOT SAC-managed."""
        sha = _commit(self.repo, ["Just a normal commit"], file_content="x")

        self.assertFalse(is_sac_managed(self.repo, sha))
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Managed"), "")
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Operation"), "")

    def test_commit_with_only_other_trailer_is_not_sac_managed(self):
        """Defensive: a commit with only SAC-Version (no SAC-Managed) is developer."""
        sha = _commit(self.repo, ["oops", "SAC-Version: 0.1.0"], file_content="x")

        self.assertFalse(is_sac_managed(self.repo, sha))

    def test_read_trailers_returns_full_dict(self):
        """read_trailers returns all four SAC keys, possibly empty."""
        t = SacTrailers(operation="modify", version="0.2.0", spec_version=2)
        subject, trailers = commit_message_with_trailers("modify", t)
        sha = _commit(self.repo, [subject, trailers], file_content="y")

        result = read_trailers(self.repo, sha)
        self.assertEqual(result["SAC-Managed"], "true")
        self.assertEqual(result["SAC-Operation"], "modify")
        self.assertEqual(result["SAC-Spec-Version"], "2")
        self.assertEqual(result["SAC-Version"], "0.2.0")


class ClassifyHistoryTests(unittest.TestCase):
    """Tests for classify_history on a mixed-history repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_classify_empty_repo_after_initial(self):
        """Repo with only the init commit (no SAC trailers) classifies it as developer."""
        result = classify_history(self.repo)
        # init commit is from setUp, not tagged
        self.assertEqual(len(result["developer"]), 1)
        self.assertEqual(len(result["sac"]), 0)

    def test_classify_mixed_history(self):
        """SAC commits land in sac bucket, plain commits in developer."""
        # Plain commit (developer)
        _commit(self.repo, ["user feature work"], file_content="a")

        # SAC-managed commit
        t = SacTrailers(operation="scaffold", version="0.1.0")
        subject, trailers = commit_message_with_trailers("Initial scaffold from servicectl", t)
        _commit(self.repo, [subject, trailers], file_content="b")

        # Another plain commit
        _commit(self.repo, ["more user feature"], file_content="c")

        result = classify_history(self.repo)
        self.assertEqual(len(result["sac"]), 1)
        self.assertEqual(len(result["developer"]), 3)  # init + 2 user commits

    def test_classify_preserves_order(self):
        """Within each bucket, newest-first order is preserved."""
        t = SacTrailers(operation="scaffold", version="0.1.0")
        subject, trailers = commit_message_with_trailers("first SAC", t)
        sha_1 = _commit(self.repo, [subject, trailers], file_content="x1")

        t = SacTrailers(operation="scaffold", version="0.1.0")
        subject, trailers = commit_message_with_trailers("second SAC", t)
        sha_2 = _commit(self.repo, [subject, trailers], file_content="x2")

        result = classify_history(self.repo)
        # Newest first
        self.assertEqual(result["sac"][0], sha_2)
        self.assertEqual(result["sac"][1], sha_1)


class ClassifyHistoryLegacyTests(unittest.TestCase):
    """Tests for classify_history(repo, sac_base_commit=...).

    Legacy migration: repos scaffolded before SAC trailers shipped
    have no SAC-Managed trailer in their history. Setting
    `sac_base_commit` to the SHA of the original scaffold commit (or
    any ancestor thereof) tells `classify_history` to treat everything
    at or older than that SHA as SAC-managed. Newer commits fall
    through to normal trailer-based classification.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _history(self):
        """Return all commit SHAs newest-first, so we can name anchors."""
        log_out = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--format=%H"],
            capture_output=True, text=True, check=True,
        ).stdout
        return log_out.strip().split("\n")

    def test_anchor_treats_older_commits_as_sac(self):
        """With sac_base_commit set to the initial commit, that commit
        classifies as SAC even without trailers. Newer developer commits
        stay developer."""
        # Add a developer commit on top of the initial commit.
        _commit(self.repo, ["user feature"], file_content="user work\n")
        all_shas = self._history()
        # all_shas: [developer, initial] (newest first)
        # Use the initial commit (oldest) as the SAC anchor.
        initial_sha = all_shas[-1]

        result = classify_history(self.repo, sac_base_commit=initial_sha)

        # The initial commit is SAC via the anchor.
        # The developer commit is developer (newer than the anchor).
        self.assertIn(initial_sha, result["sac"])
        self.assertNotIn(initial_sha, result["developer"])
        self.assertEqual(len(result["developer"]), 1)
        self.assertEqual(len(result["sac"]), 1)

    def test_anchor_includes_ancestors(self):
        """All ancestors of the anchor (including the anchor itself)
        are SAC. Anything strictly newer is developer."""
        # Build a 3-commit history: initial, SAC-tagged, user.
        t = SacTrailers(operation="scaffold", version="0.1.0")
        s, tt = commit_message_with_trailers("Initial scaffold", t)
        sac_sha = _commit(self.repo, [s, tt], file_content="scaffold\n")
        user_sha = _commit(self.repo, ["user feature"], file_content="user\n")

        # Anchor on the SAC-tagged commit (so its predecessor the
        # initial commit is also SAC, but the user commit is not).
        result = classify_history(self.repo, sac_base_commit=sac_sha)

        self.assertIn(sac_sha, result["sac"])
        self.assertIn(user_sha, result["developer"])
        self.assertNotIn(user_sha, result["sac"])
        # 2 SAC (initial + sac_sha), 1 developer (user_sha).
        self.assertEqual(len(result["sac"]), 2)
        self.assertEqual(len(result["developer"]), 1)

    def test_anchor_with_short_sha(self):
        """git short SHAs work — we resolve via rev-parse first."""
        all_shas = self._history()
        initial_sha = all_shas[-1]
        # Truncate to a short SHA (7 chars is git's default short).
        short_sha = initial_sha[:7]

        result = classify_history(self.repo, sac_base_commit=short_sha)
        self.assertIn(initial_sha, result["sac"])

    def test_anchor_with_branch_name(self):
        """Branch names resolve via rev-parse too."""
        # The initial commit is on main by default in _init_repo.
        result = classify_history(self.repo, sac_base_commit="main")
        # main's tip is the most recent commit (a user commit we added
        # above). It's strictly newer than initial, so it should be
        # developer. The initial commit itself is an ancestor of main
        # and should be SAC.
        all_shas = self._history()
        initial_sha = all_shas[-1]
        self.assertIn(initial_sha, result["sac"])

    def test_invalid_anchor_raises_valueerror(self):
        """A SHA that doesn't exist raises ValueError with a clear message."""
        with self.assertRaises(ValueError) as cm:
            classify_history(self.repo, sac_base_commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.assertIn("does not resolve", str(cm.exception))

    def test_anchor_unset_falls_through_to_trailer(self):
        """Without sac_base_commit, only SAC trailers mark commits.
        This is the legacy behavior; the anchor is purely additive."""
        t = SacTrailers(operation="scaffold", version="0.1.0")
        s, tt = commit_message_with_trailers("Initial scaffold", t)
        sac_sha = _commit(self.repo, [s, tt], file_content="scaffold\n")
        _commit(self.repo, ["user feature"], file_content="user\n")
        _commit(self.repo, ["another user feature"], file_content="user2\n")

        result = classify_history(self.repo)
        self.assertEqual(len(result["sac"]), 1)
        self.assertEqual(len(result["developer"]), 3)
        self.assertEqual(result["sac"][0], sac_sha)

    def test_anchor_on_unrelated_sha_raises(self):
        """A SHA from a different repo raises. We resolve via the local
        repo's rev-parse; a foreign SHA would fail."""
        # All-zero SHA is valid format but doesn't exist anywhere.
        with self.assertRaises(ValueError):
            classify_history(self.repo, sac_base_commit="0000000000000000000000000000000000000000")


class BlankLineTrailerRegressionTests(unittest.TestCase):
    """Regression: commits whose trailers are separated by blank lines
    (the form produced when a writer mistakenly uses one `-m` arg
    per trailer instead of bundling them into a single `-m` arg
    with embedded newlines) must still classify as SAC-managed.

    Background: `git log --format=%(trailers:key=X,valueonly)` is
    strict -- a blank line between trailers is treated as a
    paragraph break, so the trailers are silently unparseable and
    `is_sac_managed` returns False. This bit us in the
    integration-test-modified workflow's bootstrap step, where
    one prior revision used `git commit -m subject -m SAC-Managed: true
    -m SAC-Operation: bootstrap ...` and produced commits the
    refresh engine could not recognize as SAC. The fix is in
    `read_trailer`: replace the strict git format placeholder with
    `git log --format=%B` plus a relaxed in-process parser.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_with_broken_trailers(self) -> str:
        """The exact form that triggered the regression: one -m per trailer,
        which produces blank lines between trailers in the message body.
        """
        testfile = self.repo / "test.txt"
        testfile.write_text("hello\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.repo), "commit", "-q",
                "-m", "Bootstrap: scaffold service from simple_and_clean",
                "-m", "SAC-Managed: true",
                "-m", "SAC-Operation: bootstrap",
                "-m", "SAC-Spec-Version: 1",
                "-m", "SAC-Version: abc1234",
            ],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_broken_form_classifies_as_sac_managed(self):
        """is_sac_managed returns True even for the broken trailer form."""
        sha = self._commit_with_broken_trailers()
        self.assertTrue(is_sac_managed(self.repo, sha))

    def test_broken_form_reads_each_trailer(self):
        """read_trailer returns the correct value for every SAC key,
        even when the trailers are separated by blank lines."""
        sha = self._commit_with_broken_trailers()
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Managed"), "true")
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Operation"), "bootstrap")
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Spec-Version"), "1")
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Version"), "abc1234")

    def test_broken_form_round_trips_through_classify_history(self):
        """classify_history buckets the broken-form commit into 'sac'."""
        sha = self._commit_with_broken_trailers()
        result = classify_history(self.repo)
        # Init commit (untagged) is developer; broken-form SAC commit is sac.
        self.assertIn(sha, result["sac"])
        self.assertNotIn(sha, result["developer"])
        self.assertEqual(len(result["sac"]), 1)

    def test_strict_form_still_works(self):
        """The relaxed parser still handles the strict (correct) form
        produced by `commit_message_with_trailers`. No regression on
        the happy path."""
        t = SacTrailers(operation="scaffold", version="0.1.0")
        subject, trailers = commit_message_with_trailers("Initial scaffold", t)
        sha = _commit(self.repo, [subject, trailers], file_content="y")

        self.assertTrue(is_sac_managed(self.repo, sha))
        self.assertEqual(read_trailer(self.repo, sha, "SAC-Operation"), "scaffold")


if __name__ == "__main__":
    unittest.main()