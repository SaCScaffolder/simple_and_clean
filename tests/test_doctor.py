"""Smoke tests for the doctor subcommand.

Runs against the scaffolded services in sample-scaffolds/ — exercises the
real checks (file presence, Dockerfile multi-stage, USER directive,
Azure overlay files) end-to-end.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from servicectl.doctor import (
    DoctorReport,
    render_json,
    render_text,
    run_checks,
)


# Real scaffolded dispatch service from the wrapper's test-samples dir.
# Used for end-to-end doctor checks against a fresh local-deployed scaffold.
SAMPLE_LOCAL_SERVICE = (
    Path("C:/Users/henry/source/repos/sac/simple_and_clean_test_samples")
    / "dispatch-admin-api"
)


def test_sample_service_passes_baseline_checks():
    """A freshly scaffolded local-deployed service should pass all hard checks."""
    if not SAMPLE_LOCAL_SERVICE.exists():
        pytest.skip(
            "SAMPLE_LOCAL_SERVICE fixture missing — expected at "
            "C:/Users/henry/source/repos/sac/simple_and_clean_test_samples/dispatch-admin-api"
        )
    report = run_checks(SAMPLE_LOCAL_SERVICE)
    # No errors on a fresh scaffold (warnings/info may be present).
    assert not report.has_errors, (
        f"Expected no errors on fresh local scaffold, got: "
        f"{[(c.name, c.message) for c in report.checks if not c.passed and c.severity == 'error']}"
    )


def test_report_summary_includes_counts():
    """The render functions should include a summary line."""
    if not SAMPLE_LOCAL_SERVICE.exists():
        pytest.skip(
            "SAMPLE_LOCAL_SERVICE fixture missing — expected at "
            "C:/Users/henry/source/repos/sac/simple_and_clean_test_samples/dispatch-admin-api"
        )
    report = run_checks(SAMPLE_LOCAL_SERVICE)
    text = render_text(report)
    assert "summary:" in text
    assert "passed" in text


def test_json_output_is_valid():
    """--json should produce parseable JSON with the expected keys."""
    if not SAMPLE_LOCAL_SERVICE.exists():
        pytest.skip(
            "SAMPLE_LOCAL_SERVICE fixture missing — expected at "
            "C:/Users/henry/source/repos/sac/simple_and_clean_test_samples/dispatch-admin-api"
        )
    report = run_checks(SAMPLE_LOCAL_SERVICE)
    out = json.loads(render_json(report))
    assert "path" in out
    assert "deploy_target" in out
    assert "exit_code" in out
    assert "summary" in out
    assert "checks" in out
    assert isinstance(out["checks"], list)


def test_missing_dockerfile_is_error():
    """A directory without a Dockerfile should produce an error-level check."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Create the minimum to avoid being a totally empty dir, but no Dockerfile.
        (tmp_path / "README.md").write_text("# test\n")
        (tmp_path / ".gitleaks.toml").write_text("title = \"empty\"\n")
        report = run_checks(tmp_path)
        dockerfile_checks = [c for c in report.checks if c.name == "file:Dockerfile"]
        assert len(dockerfile_checks) == 1
        assert not dockerfile_checks[0].passed
        assert dockerfile_checks[0].severity == "error"


def test_single_stage_dockerfile_is_error():
    """A Dockerfile with only one FROM instruction should fail multi-stage check."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "CMD [\"python\", \"app.py\"]\n"
        )
        report = run_checks(tmp_path)
        ms_checks = [c for c in report.checks if c.name == "dockerfile:multi-stage"]
        assert len(ms_checks) == 1
        assert not ms_checks[0].passed
        assert ms_checks[0].severity == "error"


def test_root_user_dockerfile_is_warning():
    """A Dockerfile that runs as root should fail the non-root-user check."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "Dockerfile").write_text(
            "FROM python:3.12-slim AS builder\n"
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "USER root\n"
            "CMD [\"python\", \"app.py\"]\n"
        )
        report = run_checks(tmp_path)
        user_checks = [c for c in report.checks if c.name == "dockerfile:non-root-user"]
        assert len(user_checks) == 1
        assert not user_checks[0].passed
        assert user_checks[0].severity == "warn"


def test_multistage_nonroot_dockerfile_passes():
    """A well-formed multi-stage Dockerfile should pass both Docker checks."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "Dockerfile").write_text(
            "FROM python:3.12-slim AS builder\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN pip install --user /app\n"
            "\n"
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "COPY --from=builder /root/.local /root/.local\n"
            "USER appuser\n"
            "CMD [\"python\", \"app.py\"]\n"
        )
        report = run_checks(tmp_path)
        for name in ("dockerfile:multi-stage", "dockerfile:non-root-user"):
            check = next((c for c in report.checks if c.name == name), None)
            assert check is not None, f"missing check: {name}"
            assert check.passed, f"check {name} should pass: {check.message}"


def test_exit_code_for_clean_report():
    """A report with no failures should have exit_code 0."""
    report = DoctorReport(path="/tmp/fake", checks=[])
    assert report.exit_code() == 0
    assert not report.has_errors
    assert not report.has_warnings


def test_exit_code_for_errors():
    """A report with errors should have exit_code 2."""
    report = DoctorReport(
        path="/tmp/fake",
        checks=[
            # One passed, one errored.
        ],
    )
    from servicectl.doctor import Check, SEVERITY_ERROR
    report.checks.append(Check(
        name="test:foo",
        severity=SEVERITY_ERROR,
        passed=False,
        message="broken",
    ))
    report.checks.append(Check(
        name="test:bar",
        severity=SEVERITY_ERROR,
        passed=True,
        message="ok",
    ))
    assert report.exit_code() == 2
    assert report.has_errors


def test_pyproject_threshold_detected():
    """A pyproject.toml with --cov-fail-under should satisfy the threshold check."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\n'
            'addopts = "--cov=app --cov-fail-under=80"\n'
        )
        from servicectl.doctor import _coverage_threshold
        found, value, msg = _coverage_threshold(tmp_path)
        assert found
        assert value == 80
        assert "pyproject.toml" in msg


def test_package_json_threshold_detected():
    """A package.json with jest coverageThreshold should satisfy the threshold check."""
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "package.json").write_text(_json.dumps({
            "name": "x",
            "jest": {
                "coverageThreshold": {
                    "global": {"lines": 85}
                }
            }
        }))
        from servicectl.doctor import _coverage_threshold
        found, value, msg = _coverage_threshold(tmp_path)
        assert found
        assert value == 85
        assert "package.json" in msg


def test_dotnet_ci_threshold_detected():
    """A CI workflow with /p:Threshold=NN should satisfy the threshold check."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ci_dir = tmp_path / ".github" / "workflows"
        ci_dir.mkdir(parents=True)
        (ci_dir / "ci.yml").write_text(
            "- run: dotnet test /p:Threshold=80\n"
        )
        from servicectl.doctor import _coverage_threshold
        found, value, msg = _coverage_threshold(tmp_path)
        assert found
        assert value == 80
        assert "ci.yml" in msg


def test_no_threshold_anywhere_is_info_failure():
    """A project with no threshold anywhere should fail the check with a clear message."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # No pyproject.toml, no package.json, no CI workflow.
        from servicectl.doctor import _coverage_threshold
        found, value, msg = _coverage_threshold(tmp_path)
        assert not found
        assert value is None
        assert "pyproject.toml" in msg and "package.json" in msg


def test_pyproject_takes_priority_over_ci_workflow():
    """If both pyproject and CI workflow have a threshold, pyproject wins."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "pyproject.toml").write_text(
            'addopts = "--cov-fail-under=80"\n'
        )
        ci_dir = tmp_path / ".github" / "workflows"
        ci_dir.mkdir(parents=True)
        (ci_dir / "ci.yml").write_text(
            "- run: dotnet test /p:Threshold=99\n"  # would never match for a python project, but sanity check
        )
        from servicectl.doctor import _coverage_threshold
        found, value, msg = _coverage_threshold(tmp_path)
        assert found
        assert value == 80
        assert "pyproject.toml" in msg


def test_pause_skips_when_not_tty():
    """The --pause logic should only engage when stdin AND stdout are TTYs.

    When invoked from a piped/captured context (CI, test harness), the
    pause prompt must be skipped so the command doesn't hang.
    """
    import subprocess
    # `python -m servicectl doctor --pause` with stdin closed should exit
    # cleanly without waiting for a keypress.
    result = subprocess.run(
        ["python", "-m", "servicectl", "doctor", ".", "--pause"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
    )
    # Should exit with 0 or 1 (warnings) since the servicectl repo itself
    # isn't a scaffolded service. The point: it didn't hang waiting for input.
    assert result.returncode in (0, 1, 2)
    # The 'Press any key' message should NOT appear in piped output.
    assert "Press any key" not in result.stdout


# ---------------------------------------------------------------------------
# Go-specific doctor checks (run only when go.mod exists in the service dir).
# ---------------------------------------------------------------------------

def _make_minimal_go_service(tmp_path: Path) -> Path:
    """Build a minimal scaffolded Go service under tmp_path. Returns the path."""
    # Multi-stage Dockerfile (so the existing checks pass).
    (tmp_path / "Dockerfile").write_text(
        "FROM golang:1.22-bookworm AS build\n"
        "WORKDIR /src\n"
        "COPY . .\n"
        "RUN go build -o /out/app ./cmd/server\n"
        "\n"
        "FROM gcr.io/distroless/static-debian12:nonroot\n"
        "COPY --from=build /out/app /app\n"
        "USER nonroot:nonroot\n"
        "ENTRYPOINT [\"/app\"]\n"
    )
    # go.mod with a non-placeholder module path so the placeholder check passes.
    (tmp_path / "go.mod").write_text(
        "module example.com/awesome-service\n"
        "\n"
        "go 1.22\n"
    )
    # cmd/server/main.go (the conventional entrypoint).
    cmd = tmp_path / "cmd" / "server"
    cmd.mkdir(parents=True)
    (cmd / "main.go").write_text("package main\n")
    # internal/<service>/ with one .go file and one _test.go file.
    internal = tmp_path / "internal" / "awesome-service"
    internal.mkdir(parents=True)
    (internal / "server.go").write_text("package awesome_service\n")
    (internal / "server_test.go").write_text("package awesome_service\n")
    # CI workflow with `-race`.
    ci_dir = tmp_path / ".github" / "workflows"
    ci_dir.mkdir(parents=True)
    (ci_dir / "ci.yml").write_text(
        "- run: go test ./... -race -coverprofile=coverage.out\n"
    )
    return tmp_path


def test_go_checks_only_run_when_gomod_present():
    """A non-Go service (no go.mod) should not produce any `go:*` checks."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Minimal Python-style scaffold (Dockerfile, no go.mod).
        (tmp_path / "Dockerfile").write_text(
            "FROM python:3.12-slim AS build\n"
            "FROM python:3.12-slim\n"
            "USER appuser\n"
        )
        report = run_checks(tmp_path)
        go_checks = [c for c in report.checks if c.name.startswith("go:")]
        assert go_checks == [], f"unexpected Go checks on a non-Go service: {[c.name for c in go_checks]}"


def test_go_checks_run_on_go_service():
    """A well-formed Go service should produce passing go:* checks."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _make_minimal_go_service(Path(tmp))
        report = run_checks(tmp_path)
        go_checks = [c for c in report.checks if c.name.startswith("go:")]
        # Sanity: at least the seven checks we documented should be present.
        expected = {
            "go:cmd-server-exists",
            "go:modfile",
            "go:modfile-go-version",
            "go:modfile-module-path",
            "go:has-internal-package",
            "go:has-tests",
            "go:ci-uses-race",
            "go:no-vendor-dir",
        }
        actual = {c.name for c in go_checks}
        assert expected.issubset(actual), (
            f"missing Go checks: {expected - actual}"
        )
        # All checks should pass on the well-formed service.
        failures = [c for c in go_checks if not c.passed]
        assert not failures, (
            f"unexpected failures on well-formed Go service: "
            f"{[(c.name, c.message) for c in failures]}"
        )


def test_go_modfile_missing_is_error():
    """A service with cmd/server/main.go but no go.mod should fail go:modfile."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cmd = tmp_path / "cmd" / "server"
        cmd.mkdir(parents=True)
        (cmd / "main.go").write_text("package main\n")
        report = run_checks(tmp_path)
        check = next(c for c in report.checks if c.name == "go:modfile")
        assert not check.passed
        assert check.severity == "error"


def test_go_old_version_is_warning():
    """A go.mod declaring `go 1.20` should fail go:modfile-go-version as a warning."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _make_minimal_go_service(Path(tmp))
        (tmp_path / "go.mod").write_text(
            "module example.com/awesome-service\n"
            "\n"
            "go 1.20\n"
        )
        report = run_checks(tmp_path)
        check = next(c for c in report.checks if c.name == "go:modfile-go-version")
        assert not check.passed
        assert check.severity == "warn"
        assert "go 1.20" in check.message


def test_go_placeholder_module_path_is_info_failure():
    """The scaffold's `henryorsborn/<service>` placeholder should be flagged as info."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _make_minimal_go_service(Path(tmp))
        (tmp_path / "go.mod").write_text(
            "module github.com/henryorsborn/awesome-service\n"
            "\n"
            "go 1.22\n"
        )
        report = run_checks(tmp_path)
        check = next(c for c in report.checks if c.name == "go:modfile-module-path")
        assert not check.passed
        assert check.severity == "info"


def test_go_no_internal_package_is_warning():
    """A Go service with an empty internal/ should fail go:has-internal-package."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _make_minimal_go_service(Path(tmp))
        # Replace the populated internal package with an empty directory.
        import shutil
        shutil.rmtree(tmp_path / "internal")
        (tmp_path / "internal").mkdir()
        report = run_checks(tmp_path)
        check = next(c for c in report.checks if c.name == "go:has-internal-package")
        assert not check.passed
        assert check.severity == "warn"


def test_go_no_tests_is_warning():
    """A Go service with no *_test.go files should fail go:has-tests."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _make_minimal_go_service(Path(tmp))
        # Drop the test file we added in _make_minimal_go_service.
        (tmp_path / "internal" / "awesome-service" / "server_test.go").unlink()
        report = run_checks(tmp_path)
        check = next(c for c in report.checks if c.name == "go:has-tests")
        assert not check.passed
        assert check.severity == "warn"


def test_go_no_race_in_ci_is_info_failure():
    """A CI workflow without `-race` should fail go:ci-uses-race as info."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _make_minimal_go_service(Path(tmp))
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
            "- run: go test ./...\n"  # no -race
        )
        report = run_checks(tmp_path)
        check = next(c for c in report.checks if c.name == "go:ci-uses-race")
        assert not check.passed
        assert check.severity == "info"


def test_go_vendor_dir_is_info_failure():
    """A vendor/ directory should fail go:no-vendor-dir as info."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _make_minimal_go_service(Path(tmp))
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "modules.txt").write_text("# vendored\n")
        report = run_checks(tmp_path)
        check = next(c for c in report.checks if c.name == "go:no-vendor-dir")
        assert not check.passed
        assert check.severity == "info"


def test_parse_go_minor():
    """The version parser should handle 1.22, 1.22.0, and bad input gracefully."""
    from servicectl.doctor import _parse_go_minor
    assert _parse_go_minor("1.22") == 22
    assert _parse_go_minor("1.22.0") == 22
    assert _parse_go_minor("1.23.4") == 23
    assert _parse_go_minor("garbage") == 0
    assert _parse_go_minor("") == 0


if __name__ == "__main__":
    # Allow running without pytest: `python tests/test_doctor.py`
    failures = 0
    for name in dir():
        if name.startswith("test_") and callable(locals()[name]):
            try:
                locals()[name]()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    if failures:
        sys.exit(1)
    print(f"\nAll tests passed.")

