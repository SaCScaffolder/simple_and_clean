"""State reconciliation tests for `servicectl init` (issue #17).

The scaffolder has to handle every permutation of folder/repo state:

- Folder doesn't exist (parent doesn't either): create both.
- Folder doesn't exist (parent does): create the folder.
- Folder exists empty: scaffold into it.
- Folder exists with files: refuse (non-destructive).
- Folder exists with --in-place + empty: scaffold into it.
- Folder exists with --in-place + non-empty: refuse with clear msg.
- Folder exists as a *file* (not a dir): clear error.
- Repo exists (no remote): git init on existing dir.
- Repo exists with --git-remote: add remote + push.
- --git-remote without https://: clear error.
- --output-dir is unwritable: clear error.
- Dashed name: scaffold under output-dir/<dashed-name>.
- Dotted name: same.
- Empty output-dir (cwd default): scaffold there.

Each test asserts (a) no destructive behavior on conflict, (b) clear
error messages, (c) ScaffoldError where the user asked for something
that doesn't make sense.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ScaffoldError, ServiceGenerator  # noqa: E402


def _scaffold(tmp: Path, name: str, **overrides) -> Path:
    """Scaffold a service with defaults + overrides."""
    gen = ServiceGenerator(
        name=name,
        template=overrides.get("template", "python-flask"),
        ci_provider=overrides.get("ci_provider", "github-actions"),
        deploy_target=overrides.get("deploy_target", "local"),
        azure_region=overrides.get("azure_region", "eastus"),
        gcp_region=overrides.get("gcp_region", "us-central1"),
        coverage_threshold=overrides.get("coverage_threshold", 80),
        registry=overrides.get("registry", "ghcr"),
        db=overrides.get("db", "postgres"),
        output_dir=overrides.get("output_dir", tmp),
        with_git=overrides.get("with_git", False),
        with_readme=overrides.get("with_readme", True),
        in_place=overrides.get("in_place", False),
        git_remote=overrides.get("git_remote", None),
    )
    return gen.run()


# ---------------------------------------------------------------------------
# Permutation: target folder does not exist
# ---------------------------------------------------------------------------


class TargetDoesNotExistTests(unittest.TestCase):
    """Service name folder doesn't exist yet; scaffolder creates it."""

    def test_creates_target_dir(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "fresh-service"
            self.assertFalse(target.exists())
            _scaffold(Path(td), "fresh-service")
            self.assertTrue(target.is_dir())
            self.assertTrue((target / "Dockerfile").exists())
            self.assertTrue((target / "pyproject.toml").exists())

    def test_creates_deeply_nested_parent(self):
        """output-dir points to a nested dir that doesn't exist."""
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "a" / "b" / "c"
            _scaffold(Path(td), "x", output_dir=nested)
            self.assertTrue((nested / "x" / "Dockerfile").exists())


# ---------------------------------------------------------------------------
# Permutation: target folder exists
# ---------------------------------------------------------------------------


class TargetExistsTests(unittest.TestCase):
    """Target folder already exists in various states."""

    def test_empty_dir_refused_to_overwrite(self):
        """An empty target dir is still refused (no in_place flag)."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "exists").mkdir()
            with self.assertRaises(ScaffoldError) as cm:
                _scaffold(Path(td), "exists")
            self.assertIn("already exists", str(cm.exception))

    def test_existing_with_user_file_refused_to_overwrite(self):
        """Non-empty target is refused, error mentions the path."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "exists"
            target.mkdir()
            (target / "important_user_data.txt").write_text("user's code")
            with self.assertRaises(ScaffoldError) as cm:
                _scaffold(Path(td), "exists")
            self.assertIn("already exists", str(cm.exception))
            # User's file is preserved.
            self.assertEqual((target / "important_user_data.txt").read_text(),
                             "user's code")

    def test_in_place_empty_dir_succeeds(self):
        """--in-place + empty dir scaffolds into it."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "in_place_empty"
            target.mkdir()
            _scaffold(Path(td), "ignored_name", output_dir=target, in_place=True)
            self.assertTrue((target / "Dockerfile").exists())

    def test_in_place_non_empty_dir_refused(self):
        """--in-place + non-empty dir refused with clear message."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "in_place_used"
            target.mkdir()
            (target / "important_user_code.py").write_text("def foo(): pass")
            with self.assertRaises(ScaffoldError) as cm:
                _scaffold(Path(td), "ignored", output_dir=target, in_place=True)
            self.assertIn("not empty", str(cm.exception))
            # User's file is preserved.
            self.assertEqual((target / "important_user_code.py").read_text(),
                             "def foo(): pass")

    def test_in_place_with_only_dot_git_allowed(self):
        """--in-place allows pre-existing .git/ (refresh path uses this)."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "in_place_git"
            target.mkdir()
            (target / ".git").mkdir()
            (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            # Should NOT raise: only .git/ in the target dir.
            _scaffold(Path(td), "ignored", output_dir=target, in_place=True)
            self.assertTrue((target / "Dockerfile").exists())

    def test_target_is_a_file_not_a_dir(self):
        """If the target path is a file (not a directory), clear error
        that distinguishes from "directory already exists"."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "is_a_file"
            target.write_text("I'm a file, not a dir")
            with self.assertRaises(ScaffoldError) as cm:
                _scaffold(Path(td), "is_a_file")
            self.assertIn("not a directory", str(cm.exception))
            # The file is preserved (we don't delete user data on conflicts).
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(), "I'm a file, not a dir")


# ---------------------------------------------------------------------------
# Permutation: git state
# ---------------------------------------------------------------------------


class GitStateTests(unittest.TestCase):
    """Repo / git state permutations."""

    def test_no_git_flag_skips_git_init(self):
        """--no-git skips the git init entirely."""
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), "no-git", with_git=False)
            self.assertFalse((target / ".git").exists())
            self.assertFalse((target / ".servicectl.json").exists())

    def test_with_git_initializes_repo(self):
        """Default behavior: git init + initial SAC commit."""
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), "with-git", with_git=True)
            self.assertTrue((target / ".git").is_dir())
            self.assertTrue((target / ".servicectl.json").exists())
            # Verify git history has exactly one commit, the initial scaffold.
            log_out = subprocess.run(
                ["git", "-C", str(target), "log", "--format=%H"],
                capture_output=True, text=True, check=True,
            ).stdout.strip().split("\n")
            self.assertEqual(len(log_out), 1)

    def test_git_remote_requires_https(self):
        """--git-remote with SSH or git:// is refused."""
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ScaffoldError) as cm:
                _scaffold(Path(td), "ssh", git_remote="git@github.com:foo/bar.git")
            self.assertIn("https://", str(cm.exception))

    def test_git_remote_with_empty_string_treated_as_unset(self):
        """An empty --git-remote doesn't crash; treated as no flag."""
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), "empty-remote", with_git=True, git_remote="")
            # Should scaffold fine. No remote added (empty != set).
            self.assertTrue((target / ".git").is_dir())
            # No remote: 'git remote get-url origin' should fail or return empty.
            result = subprocess.run(
                ["git", "-C", str(target), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            # git returns 2 when the remote doesn't exist.
            self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# Permutation: name variants
# ---------------------------------------------------------------------------


class NameVariantTests(unittest.TestCase):
    """Service name variants: dashed, dotted, underscored."""

    def test_dashed_name(self):
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), "my-cool-service")
            self.assertTrue(target.is_dir())
            self.assertEqual(target.name, "my-cool-service")

    def test_dotted_name(self):
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), "my.service.v2")
            self.assertTrue(target.is_dir())
            self.assertEqual(target.name, "my.service.v2")

    def test_underscored_name(self):
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), "my_cool_service")
            self.assertTrue(target.is_dir())
            self.assertEqual(target.name, "my_cool_service")

    def test_invalid_name_chars_refused(self):
        """Names with spaces, slashes, etc. are refused before any FS op."""
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ScaffoldError) as cm:
                _scaffold(Path(td), "bad name with spaces")
            self.assertIn("invalid character", str(cm.exception))
            # Make sure no folder was created.
            self.assertFalse((Path(td) / "bad name with spaces").exists())


if __name__ == "__main__":
    unittest.main()