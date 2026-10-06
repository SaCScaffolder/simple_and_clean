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


if __name__ == "__main__":
    unittest.main()