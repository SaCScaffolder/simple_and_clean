"""Doctor — validate an existing scaffolded service against servicectl standards.

Catches drift from the generated baseline: missing files, single-stage
Dockerfiles, removed gitleaks config, dropped bicepparam files, etc.

Designed to run locally as a one-shot check, or in CI as a gate.

Usage:
    servicectl doctor [PATH]
    servicectl doctor my-service --strict
    servicectl doctor my-service --json
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable


# Severity levels: error = must-fix, warn = should-fix, info = nice-to-have.
SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"

# Exit codes: 0 = clean, 1 = warnings only, 2 = errors.
EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERROR = 2


@dataclass
class Check:
    """One rule result. `passed=False` means the rule fired."""
    name: str
    severity: str
    passed: bool
    message: str
    fix: str = ""  # Short hint on how to remediate if not passed.


@dataclass
class DoctorReport:
    """Aggregate of all checks against one service directory."""
    path: str
    checks: list[Check] = field(default_factory=list)
    deploy_target: str = "unknown"  # Best-effort inference from files present.

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == SEVERITY_ERROR)

    @property
    def has_warnings(self) -> bool:
        return any(not c.passed for c in self.checks if c.severity == SEVERITY_WARN)

    @property
    def has_errors(self) -> bool:
        return any(not c.passed for c in self.checks if c.severity == SEVERITY_ERROR)

    def exit_code(self) -> int:
        if self.has_errors:
            return EXIT_ERROR
        if self.has_warnings:
            return EXIT_WARN
        return EXIT_OK

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "deploy_target": self.deploy_target,
            "exit_code": self.exit_code(),
            "summary": {
                "passed": sum(1 for c in self.checks if c.passed),
                "errors": sum(1 for c in self.checks if not c.passed and c.severity == SEVERITY_ERROR),
                "warnings": sum(1 for c in self.checks if not c.passed and c.severity == SEVERITY_WARN),
                "info": sum(1 for c in self.checks if not c.passed and c.severity == SEVERITY_INFO),
            },
            "checks": [asdict(c) for c in self.checks],
        }


def _infer_deploy_target(path: Path) -> str:
    """Best-effort guess of which deploy target the service was scaffolded with."""
    if (path / "infra" / "main.bicep").exists():
        return "azure"
    if (path / "Dockerfile").exists():
        return "local"
    return "unknown"


def _file_text(path: Path) -> str:
    """Read a file as text, returning empty string on any failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _dockerfile_is_multistage(path: Path) -> tuple[bool, str]:
    """A multi-stage Dockerfile has more than one `FROM` instruction."""
    if not path.exists():
        return False, "Dockerfile missing"
    text = _file_text(path)
    from_count = len(re.findall(r"^\s*FROM\s+", text, flags=re.MULTILINE | re.IGNORECASE))
    if from_count < 2:
        return False, f"only {from_count} FROM instruction(s); need ≥2 for multi-stage build"
    return True, f"{from_count} FROM instructions (multi-stage confirmed)"


def _dockerfile_runs_nonroot(path: Path) -> tuple[bool, str]:
    """A secure-by-default Dockerfile sets USER to a non-root user."""
    if not path.exists():
        return False, "Dockerfile missing"
    text = _file_text(path)
    if not re.search(r"^\s*USER\s+\S+", text, flags=re.MULTILINE | re.IGNORECASE):
        return False, "no USER directive — container will run as root"
    if re.search(r"^\s*USER\s+root\s*$", text, flags=re.MULTILINE | re.IGNORECASE):
        return False, "USER is set to 'root' — should be a non-root user"
    return True, "USER directive present and non-root"


def _coverage_threshold(path: Path) -> tuple[bool, int | None, str]:
    """Try to read the coverage threshold from the generated project.

    Where we look depends on the template, because each tool stack has a
    different idiomatic place to declare a coverage gate:

      - python-flask:  `pyproject.toml` -> `tool.pytest.ini_options.addopts`
                       containing `--cov-fail-under=NN` (set via {{ coverage_threshold }}).
      - node-express:  `package.json` -> `jest.coverageThreshold.global.lines`
                       (set via {{ coverage_threshold }}).
      - dotnet-webapi: dotnet has no project-level threshold config; the gate
                       lives in the CI workflow as `/p:Threshold=NN`.

    Returns (found, value, message).
    """
    pyproject = path / "pyproject.toml"
    if pyproject.exists():
        text = _file_text(pyproject)
        m = re.search(r"--cov-fail-under=(\d+)", text)
        if m:
            return True, int(m.group(1)), f"coverage threshold = {m.group(1)}% (from pyproject.toml)"

    pkg_json = path / "package.json"
    if pkg_json.exists():
        try:
            import json as _json
            data = _json.loads(_file_text(pkg_json))
            threshold = (
                data.get("jest", {})
                    .get("coverageThreshold", {})
                    .get("global", {})
                    .get("lines")
            )
            if isinstance(threshold, (int, float)):
                return True, int(threshold), f"coverage threshold = {int(threshold)}% (from package.json)"
        except (ValueError, KeyError):
            pass

    # Fallback: look in the CI workflow. This is the only place dotnet-webapi
    # stores the gate (via `/p:Threshold=NN`).
    candidates = [
        path / ".github" / "workflows" / "ci.yml",
        path / "azure-pipelines.yml",
    ]
    patterns = [
        r"coverage[_-]?threshold[:\s=]+(\d+)",
        r"--cov-fail-under=(\d+)",
        r"--coverage=(\d+)",
        r"/p:Threshold=(\d+)",
        r"--code-coverage[_-]?(?:threshold|minimum)?[:\s=]+(\d+)",
    ]
    for c in candidates:
        if not c.exists():
            continue
        text = _file_text(c)
        for p in patterns:
            m = re.search(p, text, flags=re.IGNORECASE)
            if m:
                return True, int(m.group(1)), f"coverage threshold = {m.group(1)}% (from {c.name})"
    return False, None, "no coverage threshold detected (checked pyproject.toml, package.json, CI workflow)"


def _go_mod_go_version(path: Path) -> tuple[str | None, str]:
    """Read the `go` directive from go.mod. Returns (version_str_or_None, raw_text).

    Best-effort: extracts the first line matching `^go <version>`. Returns
    (None, raw) when the file is missing/unreadable or no version declared.
    """
    text = _file_text(path)
    if not text:
        return None, ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("go ") and not line.startswith("go("):
            parts = line.split(None, 1)
            if len(parts) == 2:
                return parts[1], text


def _go_mod_module_path(path: Path) -> tuple[str | None, str]:
    """Read the `module` directive from go.mod. Returns (module_or_None, raw_text).

    The module path is the first non-comment, non-blank line that starts with
    `module `. Returns (None, raw) when the file is missing/unreadable.
    """
    text = _file_text(path)
    if not text:
        return None, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("module "):
            parts = line.split(None, 1)
            if len(parts) == 2:
                return parts[1], text
    return None, text


def _has_go_test_files(path: Path) -> tuple[bool, str]:
    """True if any *_test.go file exists under path (recursively)."""
    matches = list(path.rglob("*_test.go"))
    if matches:
        return True, f"{len(matches)} *_test.go file(s) found"
    return False, "no *_test.go files found"


def _has_internal_go_package(path: Path) -> tuple[bool, str]:
    """True if at least one `internal/<name>/*.go` package exists.

    The go-webapi scaffold emits internal/<service_name_snake>/. This check
    catches the case where someone deleted the package directory entirely
    (the binary would still build with an empty `internal/`).
    """
    internal = path / "internal"
    if not internal.exists():
        return False, "no `internal/` directory"
    go_pkgs = [d for d in internal.iterdir() if d.is_dir() and any(d.rglob("*.go"))]
    if go_pkgs:
        return True, f"`internal/` has {len(go_pkgs)} Go package(s): " + ", ".join(
            sorted(d.name for d in go_pkgs)
        )
    return False, "`internal/` exists but contains no Go packages"


def _ci_uses_go_race(path: Path) -> tuple[bool, str]:
    """True if any CI workflow uses `go test ... -race`.

    The go-webapi scaffold runs tests with `-race` by default; this check
    catches accidental drops of the race detector after a workflow edit.
    Looks for `go test` followed by `-race` (on the same logical command)
    in either GitHub Actions or Azure DevOps pipelines.
    """
    candidates = [
        path / ".github" / "workflows" / "ci.yml",
        path / "azure-pipelines.yml",
    ]
    for c in candidates:
        if not c.exists():
            continue
        text = _file_text(c)
        # Look for "go test" near "-race". We accept either ordering and
        # tolerate flags between them. A simple regex captures the common
        # case; this is a coarse check.
        import re as _re
        if _re.search(r"go\s+test\b[^\n]*-race\b|-race\b[^\n]*go\s+test\b", text, flags=_re.IGNORECASE):
            return True, f"`-race` detected in {c.name}"
    return False, "no `-race` flag in `go test` invocations in CI"


def _has_vendor_dir(path: Path) -> tuple[bool, str]:
    """True if `vendor/` exists. We don't want vendored deps in a scaffolded service.

    Vendoring is occasionally legitimate (offline builds, hermetic CI), so
    this is info-level rather than warn. It exists to surface accidental
    `go mod vendor` runs that should be reverted.
    """
    vendor = path / "vendor"
    if vendor.exists() and vendor.is_dir():
        return False, "vendor/ directory present — was `go mod vendor` intentional?"
    return True, "no vendor/ directory (good — modules resolve at build time)"


def _go_checks(service_path: Path) -> list[Check]:
    """Run all Go-specific doctor checks. Caller gates on go.mod presence."""
    checks: list[Check] = []

    # go:cmd-server-exists — error if the conventional entrypoint is gone.
    cmd_main = service_path / "cmd" / "server" / "main.go"
    checks.append(Check(
        name="go:cmd-server-exists",
        severity=SEVERITY_ERROR,
        passed=cmd_main.exists(),
        message=(
            "cmd/server/main.go — present"
            if cmd_main.exists()
            else "cmd/server/main.go — missing (the go-webapi scaffold emits this)"
        ),
        fix=(
            ""
            if cmd_main.exists()
            else "regenerate with `servicectl init` --template=go-webapi or restore from source control"
        ),
    ))

    # go:modfile — error if go.mod is missing entirely.
    gomod = service_path / "go.mod"
    mod_exists = gomod.exists()
    checks.append(Check(
        name="go:modfile",
        severity=SEVERITY_ERROR,
        passed=mod_exists,
        message="go.mod — present" if mod_exists else "go.mod — missing",
        fix="" if mod_exists else "regenerate with `servicectl init` --template=go-webapi",
    ))

    if mod_exists:
        # go:modfile-go-version — warn if Go version is older than 1.22.
        # The scaffold declares `go 1.22`; anything older is drift.
        version, _raw = _go_mod_go_version(gomod)
        version_ok = version is not None and _parse_go_minor(version) >= 22
        checks.append(Check(
            name="go:modfile-go-version",
            severity=SEVERITY_WARN,
            passed=version_ok,
            message=(
                f"go.mod declares go {version}"
                if version is not None
                else "go.mod has no `go` directive"
            ),
            fix=(
                ""
                if version_ok
                else "bump `go 1.22` in go.mod to match the scaffold"
            ),
        ))

        # go:modfile-module-path — info if module path is empty or still the
        # scaffold's `github.com/henryorsborn/<service>` placeholder. We can't
        # tell from the file alone whether the placeholder is intentional
        # (you really are scaffolding a public module) or drift (you forgot
        # to update after forking), so this is info rather than warn.
        module, _raw = _go_mod_module_path(gomod)
        module_ok = bool(module) and "henryorsborn" not in (module or "")
        checks.append(Check(
            name="go:modfile-module-path",
            severity=SEVERITY_INFO,
            passed=module_ok,
            message=(
                f"module {module}"
                if module
                else "no `module` directive in go.mod"
            ),
            fix=(
                ""
                if module_ok
                else "update the `module` directive in go.mod to your real module path before publishing"
            ),
        ))

    # go:has-internal-package — warn if no Go packages live under internal/.
    pkg_ok, pkg_msg = _has_internal_go_package(service_path)
    checks.append(Check(
        name="go:has-internal-package",
        severity=SEVERITY_WARN,
        passed=pkg_ok,
        message=pkg_msg,
        fix="" if pkg_ok else "add at least one package under `internal/<service>/`",
    ))

    # go:has-tests — warn if no *_test.go files exist anywhere.
    tests_ok, tests_msg = _has_go_test_files(service_path)
    checks.append(Check(
        name="go:has-tests",
        severity=SEVERITY_WARN,
        passed=tests_ok,
        message=tests_msg,
        fix="" if tests_ok else "add at least one `*_test.go` file (httptest works well for HTTP handlers)",
    ))

    # go:ci-uses-race — info; encourages keeping the race detector on.
    race_ok, race_msg = _ci_uses_go_race(service_path)
    checks.append(Check(
        name="go:ci-uses-race",
        severity=SEVERITY_INFO,
        passed=race_ok,
        message=race_msg,
        fix=(
            ""
            if race_ok
            else "add `-race` to `go test` in the CI workflow to catch data races"
        ),
    ))

    # go:no-vendor-dir — info; surfaces accidental `go mod vendor` commits.
    no_vendor_ok, no_vendor_msg = _has_vendor_dir(service_path)
    checks.append(Check(
        name="go:no-vendor-dir",
        severity=SEVERITY_INFO,
        passed=no_vendor_ok,
        message=no_vendor_msg,
        fix="" if no_vendor_ok else "remove the vendor/ directory if `go mod vendor` was unintentional",
    ))

    return checks


def _parse_go_minor(version: str) -> int:
    """Parse `1.22` or `1.22.0` into the minor version (22). Returns 0 on parse failure."""
    parts = version.split(".")
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except (ValueError, IndexError):
        return 0


def run_checks(service_path: Path) -> DoctorReport:
    """Run all doctor checks against a service directory."""
    report = DoctorReport(
        path=str(service_path.resolve()),
        deploy_target=_infer_deploy_target(service_path),
    )

    # Always-required files.
    required_files = [
        ("Dockerfile", "Dockerfile missing", "rerun `servicectl init` to regenerate"),
        (".gitleaks.toml", "gitleaks baseline missing", "add a `.gitleaks.toml` to enable secrets scanning in CI"),
        ("docker-compose.dev.yml", "local dev compose missing", "add a `docker-compose.dev.yml` for one-command local dev"),
        (".env.example", "env example missing", "add a `.env.example` listing required environment variables"),
        ("README.md", "README missing", "add a README.md describing how to run/test/deploy the service"),
    ]
    for rel, msg, fix in required_files:
        p = service_path / rel
        report.checks.append(Check(
            name=f"file:{rel}",
            severity=SEVERITY_ERROR if rel == "Dockerfile" else SEVERITY_WARN,
            passed=p.exists(),
            message="present" if p.exists() else msg,
            fix="" if p.exists() else fix,
        ))

    # Dockerfile quality.
    df = service_path / "Dockerfile"
    is_ms, ms_msg = _dockerfile_is_multistage(df)
    report.checks.append(Check(
        name="dockerfile:multi-stage",
        severity=SEVERITY_ERROR,
        passed=is_ms,
        message=ms_msg,
        fix="convert to multi-stage: separate build stage from runtime stage" if not is_ms else "",
    ))
    nr, nr_msg = _dockerfile_runs_nonroot(df)
    report.checks.append(Check(
        name="dockerfile:non-root-user",
        severity=SEVERITY_WARN,
        passed=nr,
        message=nr_msg,
        fix="add `USER <non-root-user>` to the runtime stage" if not nr else "",
    ))

    # CI: at least one of the supported CI configs should exist.
    has_gh = (service_path / ".github" / "workflows" / "ci.yml").exists()
    has_az = (service_path / "azure-pipelines.yml").exists()
    if not (has_gh or has_az):
        report.checks.append(Check(
            name="ci:any-config",
            severity=SEVERITY_ERROR,
            passed=False,
            message="no CI configuration found (.github/workflows/ci.yml or azure-pipelines.yml)",
            fix="rerun `servicectl init` with --ci=github-actions or --ci=azure-devops",
        ))
    else:
        report.checks.append(Check(
            name="ci:any-config",
            severity=SEVERITY_INFO,
            passed=True,
            message="github-actions" if has_gh else "azure-devops",
        ))

    # Devcontainer.
    devcontainer = service_path / ".devcontainer" / "devcontainer.json"
    report.checks.append(Check(
        name="devcontainer:present",
        severity=SEVERITY_INFO,
        passed=devcontainer.exists(),
        message="present" if devcontainer.exists() else "missing (optional but recommended)",
        fix="" if devcontainer.exists() else "add `.devcontainer/devcontainer.json` for VS Code remote dev",
    ))

    # Source + tests non-empty.
    src = service_path / "src"
    tests = service_path / "tests"
    src_ok = src.exists() and any(src.rglob("*"))
    tests_ok = tests.exists() and any(tests.rglob("*"))
    report.checks.append(Check(
        name="src:non-empty",
        severity=SEVERITY_WARN,
        passed=src_ok,
        message="present with content" if src_ok else "src/ missing or empty",
        fix="" if src_ok else "add a `src/` directory with your service code",
    ))
    report.checks.append(Check(
        name="tests:non-empty",
        severity=SEVERITY_WARN,
        passed=tests_ok,
        message="present with content" if tests_ok else "tests/ missing or empty",
        fix="" if tests_ok else "add a `tests/` directory with at least one test",
    ))

    # Coverage threshold (only meaningful if a CI workflow exists).
    cov_ok, cov_val, cov_msg = _coverage_threshold(service_path)
    report.checks.append(Check(
        name="ci:coverage-threshold",
        severity=SEVERITY_INFO,
        passed=cov_ok,
        message=cov_msg,
        fix="" if cov_ok else "no `coverage_threshold:` value found in pyproject.toml / package.json / CI workflow — servicectl defaults to 80%",
    ))

    # Azure overlay (only if infra/main.bicep exists — i.e. was scaffolded with --deploy=azure).
    if report.deploy_target == "azure":
        required_azure = [
            ("infra/main.bicep", "main Bicep file missing", "regenerate with --deploy=azure"),
            ("infra/dev.bicepparam", "dev bicepparam missing", "regenerate with --deploy=azure"),
            ("infra/staging.bicepparam", "staging bicepparam missing", "regenerate with --deploy=azure"),
            ("infra/prod.bicepparam", "prod bicepparam missing", "regenerate with --deploy=azure"),
            ("deploy.yml", "CD workflow missing", "regenerate with --deploy=azure"),
        ]
        for rel, msg, fix in required_azure:
            p = service_path / rel
            report.checks.append(Check(
                name=f"azure:{rel}",
                severity=SEVERITY_ERROR,
                passed=p.exists(),
                message="present" if p.exists() else msg,
                fix="" if p.exists() else fix,
            ))

    # Go-specific checks. Run when the service *looks like* a Go service.
    # Detection: presence of go.mod OR a cmd/server/main.go entrypoint.
    # Either signal alone means "this is a Go-shaped service" and warrants
    # the Go checks (including the ones that fire when go.mod is missing).
    looks_like_go = (service_path / "go.mod").exists() or (service_path / "cmd" / "server" / "main.go").exists()
    if looks_like_go:
        for c in _go_checks(service_path):
            report.checks.append(c)

    return report


def render_text(report: DoctorReport) -> str:
    """Render the report as a human-readable text block."""
    lines = []
    sym = {True: "✓", False: "✗"}
    color = {True: "green", False: "red"}

    header = f"doctor: {report.path}"
    if report.deploy_target != "unknown":
        header += f"  (deploy: {report.deploy_target})"
    lines.append(header)
    lines.append("=" * len(header))

    if not report.checks:
        lines.append("  (no checks ran)")
        return "\n".join(lines)

    # Group by severity for readable output.
    for severity in (SEVERITY_ERROR, SEVERITY_WARN, SEVERITY_INFO):
        items = [c for c in report.checks if c.severity == severity]
        if not items:
            continue
        lines.append("")
        lines.append(f"{severity.upper()}S:")
        for c in items:
            mark = sym[c.passed]
            status = "PASS" if c.passed else "FAIL"
            line = f"  [{mark}] {status:<4}  {c.name}"
            if not c.passed:
                line += f" — {c.message}"
                if c.fix:
                    line += f"\n          fix: {c.fix}"
            else:
                line += f" — {c.message}"
            lines.append(line)

    summary = (
        f"\nsummary: {sum(1 for c in report.checks if c.passed)}/{len(report.checks)} passed; "
        f"{sum(1 for c in report.checks if not c.passed and c.severity == SEVERITY_ERROR)} errors, "
        f"{sum(1 for c in report.checks if not c.passed and c.severity == SEVERITY_WARN)} warnings, "
        f"{sum(1 for c in report.checks if not c.passed and c.severity == SEVERITY_INFO)} info"
    )
    lines.append(summary)
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    return json.dumps(report.to_dict(), indent=2)


__all__ = [
    "DoctorReport",
    "Check",
    "run_checks",
    "render_text",
    "render_json",
    "EXIT_OK",
    "EXIT_WARN",
    "EXIT_ERROR",
]
