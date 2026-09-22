# Changelog

All notable changes to `servicectl` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`src/doctor-go/` — a Go sidecar validator.** Stdlib-only Go binary
  that mirrors a focused subset of `servicectl doctor` (file presence,
  Dockerfile multi-stage, CI workflow, plaintext-secret detection) with
  structured JSON output and CI-gate exit codes. Cold-starts in single-
  digit milliseconds, suitable for pre-commit hooks and tight CI gates.
  Not a replacement for the Python doctor — a focused, polyglot counterpart.
  14 unit tests covering file presence, multi-stage detection, deploy-target
  inference, JSON shape, summary aggregation, and exit-code mapping.
- **`src/sdk-ts/` — a TypeScript SDK.** Typed wrapper around the
  `servicectl init` CLI exposing `ServiceConfig`, `scaffold()`, `resolveConfig()`,
  `readScaffoldJSON()`, and `ScaffoldError`. Validates input at runtime,
  shells out to the Python CLI as the source of truth, and returns a typed
  `ScaffoldResult`. 12 unit tests covering name validation, default
  application, CLI arg construction, and error class shape.
- **Polyglot repo layout.** `src/` now contains three subpackages:
  - `src/servicectl/` — Python CLI (existing)
  - `src/doctor-go/` — Go sidecar validator (new)
  - `src/sdk-ts/` — TypeScript SDK (new)
- **`src/PACKAGES.md`** documents the polyglot structure, per-package
  toolchain versions, and how to develop each subpackage locally.

## [0.2.1] - 2026-09-10

### Fixed
- **dotnet-webapi CI templates now gate on coverage threshold.** Added
  `/p:Threshold={{ coverage_threshold }}` to both the GitHub Actions
  (`templates/dotnet-webapi/.github/workflows/ci.yml.j2`) and Azure
  DevOps (`templates/dotnet-webapi/azure-pipelines.yml.j2`) templates.
  Previously the dotnet template collected cobertura coverage as an
  artifact but never enforced a threshold — a silent quality gap
  compared to python-flask (pytest `--cov-fail-under`) and node-express
  (jest `coverageThreshold`).
- **`servicectl doctor` now reads coverage thresholds from the right
  place per template.** Previously only inspected CI workflows, so
  python-flask and node-express services with thresholds in `pyproject.toml`
  and `package.json` respectively were flagged as missing a threshold.
  Now checks (in order): `pyproject.toml` (`--cov-fail-under=NN`),
  `package.json` (`jest.coverageThreshold.global.lines`), then the CI
  workflow (covers dotnet-webapi's `/p:Threshold=NN` and the existing
  patterns). Added 5 new doctor tests covering each detection path
  and priority ordering.

## [0.2.0] - 2026-09-08

### Added
- **`servicectl doctor` subcommand** — validates an existing scaffolded
  service against servicectl standards. Catches drift: missing files,
  single-stage Dockerfiles, removed gitleaks config, dropped bicepparam
  files, etc.
  - Checks grouped by severity: ERROR (must-fix), WARN (should-fix), INFO (nice-to-have).
  - Exit codes: 0 = clean, 1 = warnings, 2 = errors. Usable as a CI gate.
  - `--json` flag for machine-readable output in pipelines.
  - `--strict` flag to treat warnings as errors.
- Doctor test suite: 9 tests covering the Azure scaffold baseline,
  missing Dockerfile detection, single-stage Dockerfile detection,
  root-user detection, well-formed Dockerfile pass-through, exit code
  logic, and JSON output structure.
- README "Validating existing services with `doctor`" section with
  example output and a GitHub Actions CI gate snippet.

## [0.1.0] - 2026-09-07

### Added
- Initial release.
- Three service templates: `node-express`, `python-flask`, `dotnet-webapi`.
- Two CI providers: GitHub Actions and Azure DevOps.
- One deploy target: `azure` (Azure App Service Linux + ACR + Postgres Flexible Server, Bicep).
- Placeholder for `azure-container-apps` deploy target (CLI Choice + hint only).
- Flags for `--template`, `--ci`, `--deploy`, `--azure-region`, `--coverage`,
  `--registry`, `--output-dir`, `--no-git`, `--no-readme`.
- Filename Jinja substitution (so `dotnet-webapi` can produce
  `MyService.csproj` from `{{ service_name_pascal }}.csproj`).
- Jinja `{% raw %}` blocks for GitHub Actions `${{ }}` expressions.
- Windows console UTF-8 fix for Rich Unicode glyphs.
- Smoke test suite: 9 tests covering template registration, all three
  templates, dashed/dotted names, dotnet PascalCase filenames, custom
  coverage, error paths, and the Azure overlay emit/skip behavior.
- GitHub Actions CI workflow for `servicectl` itself: tests across Python
  3.10-3.13 on Ubuntu and Windows, plus Bicep validation in CI.
