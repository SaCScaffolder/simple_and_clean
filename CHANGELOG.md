# Changelog

All notable changes to `servicectl` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`go-webapi` template.** Go 1.22+ web API scaffold with `cmd/server/` +
  `internal/<service>/` layout, distroless static multi-stage Dockerfile,
  nonroot user, GitHub Actions + Azure DevOps CI (vet → race-detector test
  with configurable coverage threshold → Trivy scan → publish), docker-compose
  dev stack with Postgres, `.devcontainer/`, `.gitleaks.toml`,
  `.env.example`, gitleaks baseline, README. Registered in
  `src/servicectl/templates.py` and added to the SDK's `TemplateId` union
  in `src/sdk-ts/src/index.ts`. Two new smoke tests
  (`test_go_webapi_scaffolds`, `test_go_webapi_dashed_name_substitutes_correctly`)
  cover file emission and `service_name_snake` substitution in
  `internal/<service>/` paths.
- **`docs/quickstart.md`** — three-entry-point quickstart (Python CLI,
  TypeScript SDK, Go sidecar) with explicit calls for when to use each,
  plus a "Scaffolding a Go service today" section that documents the
  current state of the Go template path.
- **Go-specific doctor checks.** Eight new checks fire automatically when a
  service looks like a Go service (has `go.mod` or `cmd/server/main.go`):
  `go:cmd-server-exists`, `go:modfile`, `go:modfile-go-version`,
  `go:modfile-module-path`, `go:has-internal-package`, `go:has-tests`,
  `go:ci-uses-race`, `go:no-vendor-dir`. Implemented in both
  `src/servicectl/doctor.py` (the source of truth) and `src/doctor-go/main.go`
  (the Go mirror). 9 new Python tests and 9 new Go tests cover the matrix
  of pass / fail cases per check.
- **`src/servicectl-go/`** — a thin Go CLI wrapper around the Python CLI,
  mirroring the `sdk-ts` pattern for TypeScript. Single static binary,
  stdlib only. Parses a typed `resolvedConfig` struct, builds the same
  flags the Python CLI accepts, and shells out to `python -m servicectl
  init`. Supports both Go-convention (`init --template=foo my-svc`) and
  Python-convention (`init my-svc --template=foo`) arg ordering via the
  `splitName` helper. Includes a comprehensive 23 KB README with install
  instructions, full flag reference, usage patterns, error codes, and
  troubleshooting. 3 unit tests cover `splitName`, name validation, and
  template id validation.
- **`node-react-web` template.** Frontend SPA scaffold with Vite 5 + React
  18 + TypeScript + Tailwind + shadcn/ui (light/dark mode, CSS variables,
  example Button primitive). Multi-stage Docker build with node build stage
  → nginx-alpine runtime serving the static `dist/` folder. SPA-friendly
  nginx config (fallback to index.html for client-side routing). Vitest +
  @testing-library/react for tests, eslint with `--max-warnings 0` gate.
  GitHub Actions + Azure DevOps CI: typecheck → lint → test (with
  configurable coverage threshold, enforced in CI via vitest thresholds +
  jq awk gate) → build → Trivy scan → publish. Registered in
  `src/servicectl/templates.py`, the SDK's `TemplateId` union in
  `src/sdk-ts/src/index.ts`, and the Go wrapper's `validTemplate` switch
  in `src/servicectl-go/main.go`. Two new smoke tests
  (`test_node_react_web_scaffolds`, `test_node_react_web_no_db_in_scaffold`)
  cover file emission and a regression guard against accidentally shipping
  DB deps in the frontend bundle.

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
