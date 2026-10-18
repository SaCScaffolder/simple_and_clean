"""Tests for the go-webapi template's CI workflow and Dockerfile,
specifically the go.mod / go.sum setup steps.

Scope (PR 5 follow-up):

- The scaffolded `.github/workflows/ci.yml` must run `go mod tidy`
  before `go vet`, not `go mod download`. `go mod download` only
  writes the `/go.mod h1:...` entries to go.sum, not the content
  hashes, and does NOT add the `// indirect` lines for transitive
  deps. With a freshly-scaffolded go.mod and no go.sum, this
  leaves `go vet` complaining about "missing go.sum entry" for
  every transitive dep. The bug surfaced on PR #139's integration
  test after the pgx dep was added in PR 5.

- The scaffolded `Dockerfile` must run `go mod tidy` (not
  `go mod download`) in the build stage, for the same reason.

Tests render the scaffold via the real `ServiceGenerator.run()`
codepath and grep the rendered files for the right command.

Run with: pytest tests/test_go_webapi_ci_workflow.py -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402


@pytest.fixture
def scaffold_target(request):
    """Yield a Path for the scaffold output. Cleaned up after."""
    tmp = Path(tempfile.mkdtemp(prefix="servicectl-test-"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _scaffold(scaffold_target: Path, db: str = "postgres") -> Path:
    gen = ServiceGenerator(
        name="demo",
        template="go-webapi",
        ci_provider="github-actions",
        deploy_target="gcp-gke-autopilot",
        azure_region="eastus",
        coverage_threshold=80,
        registry="gar",
        db=db,
        output_dir=scaffold_target,
        with_git=False,
        with_readme=True,
        gcp_region="us-central1",
        gcp_project_id="sacscaffolder",
    )
    return gen.run()


class TestCiWorkflowUsesGoModTidy:
    """The scaffolded ci.yml uses `go mod tidy` before `go vet`."""

    def test_ci_yml_exists(self, scaffold_target):
        t = _scaffold(scaffold_target)
        assert (t / ".github" / "workflows" / "ci.yml").exists()

    def test_ci_yml_runs_go_mod_tidy(self, scaffold_target):
        """The CI workflow must run `go mod tidy` to populate both
        the indirect go.mod entries and the content hashes in
        go.sum. `go mod download` is insufficient."""
        t = _scaffold(scaffold_target)
        text = (t / ".github" / "workflows" / "ci.yml").read_text()
        assert "go mod tidy" in text

    def test_ci_yml_does_not_use_go_mod_download_alone(self, scaffold_target):
        """Regression guard for PR #139's bug. The CI must NOT
        use `go mod download` as the only setup step (without
        `all` or without a prior `go mod tidy`)."""
        t = _scaffold(scaffold_target)
        text = (t / ".github" / "workflows" / "ci.yml").read_text()
        # `go mod download` may appear in comments, but it must not
        # be the actual `run:` for the setup step.
        run_lines = [
            line.strip() for line in text.splitlines()
            if line.strip().startswith("run:") and "go mod" in line
        ]
        assert run_lines, "expected at least one `run:` line with `go mod`"
        for line in run_lines:
            # The `run:` must invoke `go mod tidy`, not `go mod download`.
            assert "go mod tidy" in line or "go mod download all" in line, (
                f"CI uses `{line}` which is insufficient for a fresh "
                f"go.mod. Use `go mod tidy` instead. (See PR #139's "
                f"integration test failure for the symptom.)"
            )
            assert "go mod download\n" not in line, (
                f"CI uses bare `go mod download` in `{line}`. Use "
                f"`go mod tidy` (or `go mod download all`) instead."
            )


class TestDockerfileUsesGoModTidy:
    """The scaffolded Dockerfile uses `go mod tidy` in the build stage."""

    def test_dockerfile_exists(self, scaffold_target):
        t = _scaffold(scaffold_target)
        assert (t / "Dockerfile").exists()

    def test_dockerfile_runs_go_mod_tidy(self, scaffold_target):
        """The Dockerfile's build stage must run `go mod tidy` to
        populate go.sum and the indirect go.mod entries before
        `go build`. Same regression guard as the CI workflow."""
        t = _scaffold(scaffold_target)
        text = (t / "Dockerfile").read_text()
        # Look for the RUN line in the build stage.
        run_lines = [
            line.strip() for line in text.splitlines()
            if line.strip().startswith("RUN") and "go mod" in line
        ]
        assert run_lines, "expected at least one RUN with `go mod`"
        for line in run_lines:
            assert "go mod tidy" in line, (
                f"Dockerfile uses `{line}` which is insufficient for "
                f"a fresh go.mod. Use `go mod tidy` instead."
            )

    def test_dockerfile_copies_go_sum_glob(self, scaffold_target):
        """The `COPY go.mod go.sum* ./` pattern is intentional: it
        matches either go.sum (if it exists) or no file (if it
        doesn't), and `go mod tidy` then creates it. This
        regression guard prevents a future edit from breaking
        the optional-copy pattern."""
        t = _scaffold(scaffold_target)
        text = (t / "Dockerfile").read_text()
        assert "go.sum*" in text

    def test_dockerfile_runs_go_mod_tidy_after_source_copy(self, scaffold_target):
        """`go mod tidy` must run AFTER the source is copied,
        not before. Otherwise the blank import of the pgx stdlib
        driver in cmd/server/main.go (`_ "github.com/jackc/pgx/v5/stdlib"`)
        isn't visible to `go mod tidy`, the stdlib subpackage
        doesn't make it into go.mod, and the subsequent `go build`
        fails with "no required module provides package
        github.com/jackc/pgx/v5/stdlib".

        Regression guard for the first end-to-end run of the
        postgres scaffold (2026-10-18): the Dockerfile's
        `RUN go mod tidy` ran before `COPY cmd` / `COPY internal`,
        so the source wasn't visible to tidy."""
        t = _scaffold(scaffold_target, db="postgres")
        text = (t / "Dockerfile").read_text()
        # Find the line numbers of `RUN go mod tidy` and the
        # first `COPY cmd` or `COPY internal` line after it.
        tidy_idx = text.find("RUN go mod tidy")
        assert tidy_idx > 0, "expected `RUN go mod tidy` in Dockerfile"
        after = text[tidy_idx:]
        # The first COPY cmd/ COPY internal line must come BEFORE
        # the RUN go mod tidy line. We check by looking at the
        # position of `RUN go mod tidy` in the full text vs. the
        # position of the source COPY lines.
        cmd_copy_idx = text.find("COPY cmd ./cmd")
        internal_copy_idx = text.find("COPY internal ./internal")
        # Use the first matching COPY (whichever comes first).
        source_copy_idx = min(
            i for i in (cmd_copy_idx, internal_copy_idx) if i > 0
        )
        assert source_copy_idx < tidy_idx, (
            "Dockerfile: `RUN go mod tidy` must run AFTER the source is "
            "copied (so blank imports of pgx/v5/stdlib are visible to tidy). "
            f"Found source COPY at byte {source_copy_idx} but `RUN go mod tidy` "
            f"at byte {tidy_idx}."
        )
