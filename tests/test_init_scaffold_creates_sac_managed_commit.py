"""Regression test for issue #44 / #45: the scaffold's initial commit
must carry SAC-Managed trailers so `servicectl refresh` can identify it
later.

Before #45 landed, the initial commit was a plain message with no
trailers. A future `servicectl refresh` would then misclassify SAC's
own scaffold commit as a developer commit and try to cherry-pick it
onto the new scaffold -- a useless loop. This test ensures the
trailers are present.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from servicectl.generator import ServiceGenerator


def test_scaffold_initial_commit_carries_sac_trailers():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "rendered"
        out.mkdir()
        ServiceGenerator(
            name="trailer-check", template="go-webapi", ci_provider="github-actions",
            deploy_target="local", azure_region="eastus", coverage_threshold=80,
            registry="ghcr", db="postgres", output_dir=out,
            with_git=True, with_readme=False,
        ).run()

        target = out / "trailer-check"
        # Read the full commit message (subject + body) of HEAD.
        msg = subprocess.run(
            ["git", "-C", str(target), "log", "--format=%B", "-n", "1"],
            capture_output=True, text=True, check=True,
        ).stdout

        assert "SAC-Managed: true" in msg, (
            f"Initial scaffold commit is missing SAC-Managed: true trailer. "
            f"Refresh will misclassify this commit as developer. Got:\n{msg}"
        )
        assert "SAC-Operation: scaffold" in msg
        assert "SAC-Spec-Version: 1" in msg