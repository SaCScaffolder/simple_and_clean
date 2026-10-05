# Changelog

All notable changes to `servicectl` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Coverage threshold enforced locally.** `[tool.pytest.ini_options]` in pyproject.toml now sets `--cov=servicectl --cov-fail-under=80`, so any `pytest` invocation (including the in-source `tests/test_smoke.py` runner) fails when coverage drops below 80%. Before this change, the coverage threshold was documented but never enforced — a commit that dropped coverage would still pass tests locally and only fail in CI on push. The `.githooks/pre-commit` hook now runs `pytest -q` so local == CI behavior.
- **9 new smoke tests** covering previously-untested code paths (combined with the existing tests, coverage is now 81.19%, clearing the 80% threshold):
  - `test_init_from_config_invalid_json_errors` (cli.py JSONDecodeError branch)
  - `test_init_from_config_top_level_not_object_errors` (cli.py isinstance check)
  - `test_init_from_config_missing_template_errors` (cli.py post-merge template check)
  - `test_init_invalid_name_characters_errors_cleanly` (cli.py ScaffoldError -> err_console)
  - `test_main_module_entry_point_invokes_cli` (covers servicectl/__main__.py via runpy so coverage tracks)
  - `test_modify_show_only_prints_current_settings` (cli.py show_only branch)
  - `test_modify_same_db_is_noop` (cli.py same-db short-circuit)
  - `test_modify_path_with_no_db_flag_errors` (cli.py "no --db flag passed" branch)
  - `test_doctor_human_text_output` (cli.py render_text branch, not just render_json)
- **Regression check**: temporarily raising `--cov-fail-under=99` in pyproject.toml causes the pre-commit hook to reject commits with `Coverage failure: total of 81 is less than fail-under=99`, confirming the gate is wired end-to-end.

### Fixed
- **`servicectl doctor` was not registered as a Click subcommand.** `def doctor(...)` was a free function with no `@main.command()` decorator, so `servicectl doctor` did not appear in `--help` and could not be invoked. The decorators were inside the function body instead of above it. Moved them to a proper `@main.command()` stack. Discovered when writing `test_doctor_*` tests: `AttributeError: 'function' object has no attribute 'name'`.
- **`servicectl modify` had doctor's option set.** modify was decorated with `--strict` / `--json` / `--pause` instead of its own `--db` / `--show` / `--dry-run` / `--yes`. The function signature took the right parameters but the decorators disagreed, so Click rejected every modify-specific flag. Replaced doctor's options with modify's real ones. Pre-existing bug, latent since modify was first added.

### Build
- Pre-commit hook now runs `python -m pytest -q` (was: `python tests/test_smoke.py`). The new command picks up the pyproject.toml `addopts`, including `--cov-fail-under=80`. Same install instruction as before: `git config core.hooksPath .githooks`.

### Fixed (CI)
- **`test_init_from_config_top_level_not_object_errors` was wrap-fragile.** The assertion looked for `"must contain a JSON object"` as a literal substring of the CLI's error message, but Rich's `Console` word-wraps the message at terminal width. On Windows CI runners with a narrower-than-80 terminal, Rich split `"JSON object"` across a newline and the substring match failed. Switched the assertion to `exit_code != 0`, which already proves the CLI rejected the bad JSON and is independent of how the message is wrapped for display. Documented the trap in the docstring so it isn't regressed. Drive the scaffolder from a JSON file instead of (or alongside) CLI flags. Schema is a flat JSON object whose keys are the same names as the CLI options (`name`, `template`, `ci_provider`, `deploy_target`, `azure_region`, `gcp_region`, `gcp_project_id`, `coverage_threshold`, `registry`, `db`, `output_dir`, `in_place`, `git_remote`). **Precedence**: explicit CLI flag > JSON value > built-in default. Click's `parameter_source` API is used to tell whether a flag was passed on the command line vs left at its default, so an explicit `--coverage=95` always wins over the JSON's 80. **Strict mode** (`--strict-config`) errors on unknown JSON keys; without it, unknown keys are silently dropped so JSON specs stay forward-compatible when new options are added. Use case: machine-generated scaffolding specs (e.g. a PowerShell wrapper that reads a JSON file and shells out to `servicectl init --from-config=...`) without parsing CLI args in shell. 5 new smoke tests cover happy path, CLI override, missing required key, strict unknown-key error, and lenient unknown-key drop.

### Fixed
- **node-react-web `vite.config.ts.j2` lacks `/// <reference types="vitest" />`.** Without this triple-slash reference at the top of the file, `tsc -b` rejects the `test:` block with `TS2769: No overload matches this call. Object literal may only specify known properties, and 'test' does not exist in type 'UserConfigExport'`, breaking the `npm run build` script on every scaffold. (Upstream template is wrong, not just the example repo.) Fix: add the triple-slash reference at the top of `vite.config.ts.j2`. Discovered while testing `sac_example_ala_web`. Same pattern applies to any Vite+Vitest config — the `test` block lives in Vitest's type augmentations, not Vite's `UserConfigExport`.
- **node-react-web `src/api/client.ts.j2` returns the literal string "undefined" for missing `VITE_API_BASE_URL`.** Vite substitutes missing `VITE_*` env vars with the *string* `"undefined"` (not actual `undefined`), so the naive `?? "/api"` fallback never triggers. Every request in production becomes `/undefined/...`. Fix: explicitly check for `"undefined"` and empty string alongside nullish, then fall back to `/api`. Discovered via a new test in `sac_example_ala_web/tests/client.test.ts` that asserts the `/api` fallback. New upstream smoke test asserts the rendered `client.ts` guards against the substitution.

### Added
- **GCP deploy overlay (`--deploy=gcp-cloud-run`, `--deploy=gcp-gke-autopilot`).** Two new deploy targets alongside the existing Azure overlay. Cloud Run ships a serverless HTTPS service with autoscaling; GKE Autopilot ships a fully-managed Kubernetes cluster with pay-per-pod billing. Both use **Workload Identity Federation** for passwordless CD (no JSON keys), bootstrap via a generated `infra/bootstrap.sh`, and a shared runtime service account + Artifact Registry. New CLI flags: `--gcp-region` (default `us-central1`), `--gcp-project-id`, and `--registry=gar` (Google Artifact Registry). Overlay lives at `src/servicectl/deploy/gcp/` and contains Terraform modules for both targets. Smoke-tested: 67+1 tests pass, rendered scaffolds produce valid Terraform + a working `deploy.yml` that references the right WIF secrets. Closes the gap where the scaffolder only knew how to deploy to Azure.
- **`.githooks/pre-commit` ships in every scaffolded service** (`dotnet-webapi`, `go-webapi`, `node-express`, `node-react-web`, `python-flask`). Closes the gap where `simple_and_clean`'s own pre-commit hook protected the scaffolder but not the services it generates — so a recruiter who cloned a generated service got no pre-push smoke-test gate and the same class of bug we fixed in `5808d00` / `567509f` could re-emerge. The hook runs the language-appropriate test command (`go test -race`, `npm test`, `pytest -q`, `dotnet test`) with `set -euo pipefail`, picks up `python3` / `python` / `py` in that order, and prints a one-time `git config core.hooksPath .githooks` install hint in every README. Includes a `--no-verify` bypass for WIP commits. New smoke test: `test_scaffolded_services_ship_githooks_pre_commit` asserts the hook ships in all 5 templates with the right test command per language.

### Fixed
- **docker-compose.dev.yml template indentation bug.** All 4 templates (`python-flask`, `node-express`, `dotnet-webapi`, `go-webapi`) had the top-level `volumes:` block indented with 2 spaces under `services:`. This made Docker Compose interpret `volumes:` as a service called "volumes" rather than a top-level named volume declaration, causing `docker compose up` to fail with: `services.volumes additional properties 'pgdata' not allowed`. Fixed by un-indenting the top-level `volumes:` block to column 0 in all 16 occurrences (4 templates x 4 db flavors). Discovered while testing the URL shortener demo locally.
- **Added regression test** `test_docker_compose_has_top_level_volumes_block` that fails if any scaffolded `docker-compose.dev.yml` has the indented `  volumes:` block.

### Added
- **`--git-remote` flag for `servicectl init`.** Scaffolds a service and pushes the initial commit to a remote in one step. Usage: `servicectl init my-svc --template=go-webapi --git-remote=https://github.com/me/my-svc.git`. The flag must be an `https://` URL (SSH not supported in v1). After `git init`, the tool runs `git remote add origin <url>` followed by `git push -u origin main`. If the push fails (auth, non-fast-forward, wrong URL), the service is left in a usable state with the remote configured but not pushed — the error message includes a tip for the common non-fast-forward case (`git pull --rebase origin main && git push`).
- **3 new smoke tests** cover the `--git-remote` flag:
  - `test_git_remote_rejects_non_https` (SSH form rejected at validation)
  - `test_git_remote_accepts_https_url` (validation accepts a valid https URL)
  - `test_git_remote_with_no_git_does_not_crash` (no-op when `--no-git` is also set)

### Changed
- `src/servicectl/generator.py`: added `git_remote: str | None = None` field to `ServiceGenerator`. Added validation that `--git-remote` starts with `https://`. After a successful `git init`, runs `git remote add origin <url>` and `git push -u origin main`, with clear error messages on failure.
- `src/servicectl/cli.py`: added `--git-remote` Click option (string, default None). Threaded through to `ServiceGenerator(git_remote=git_remote)`.

### Added
- **`--in-place` flag for `servicectl init`.** Scaffolds directly into `--output-dir` instead of creating a `<name>` subfolder. Useful when you want to scaffold a service into an existing empty directory (for example, an empty Git repo you've already initialized, or a pre-prepared demo directory). Refuses to overwrite a non-empty directory to protect user code. Default behavior (creating `<output_dir>/<name>`) is unchanged.
- **5 new smoke tests** cover the `--in-place` flag:
  - `test_in_place_into_empty_dir_succeeds`
  - `test_in_place_into_nonexistent_dir_creates_it`
  - `test_in_place_into_nonempty_dir_refuses`
  - `test_in_place_into_current_dir_succeeds`
  - `test_default_init_still_creates_subfolder` (regression guard)

### Changed
- `src/servicectl/generator.py`: added `in_place: bool = False` field to `ServiceGenerator`. New `_resolve_target()` method handles both default and in-place path computation. The `run()` method now passes `exist_ok=self.in_place` to `target.mkdir()` so in-place scaffolding into an existing empty dir works.
- `src/servicectl/cli.py`: added `--in-place` Click flag with help text describing the new behavior. Threaded through to `ServiceGenerator(in_place=in_place)`.

### Added
- **`servicectl modify` command for --db.** New command lets you change the database backend of an existing scaffolded service without re-scaffolding from scratch. Usage:
  - `servicectl modify --show` prints current template, deploy, and db settings
  - `servicectl modify --db=<flavor>` rewrites `docker-compose.dev.yml` (always) plus `infra/main.bicep` and the 3 `infra/<env>.bicepparam` files (if `--deploy=azure` was used)
  - `--dry-run` shows what would change as a unified diff without writing
  - `--yes` skips the confirmation prompt
- **8 new smoke tests** cover the modify command:
  - `test_modify_show_prints_current_settings`
  - `test_modify_postgres_to_mysql_rerenders_compose`
  - `test_modify_mysql_to_postgres_rerenders_compose`
  - `test_modify_to_cosmos_rerenders_compose`
  - `test_modify_with_azure_overlay_rerenders_bicepparam`
  - `test_modify_same_db_is_noop`
  - `test_modify_rejects_unknown_db`
  - `test_modify_rejects_non_scaffolded_directory`

### Changed
- `src/servicectl/modifier.py`: NEW module containing `ServiceModifier` and `ModifierError`. Reads current `--db` from `docker-compose.dev.yml` via sentinel strings (postgres:16-alpine / mysql:8.0 / azure-sql-edge / azure-cosmos-emulator). Detects template via file presence (pyproject.toml / go.mod / package.json / Program.cs). Detects Azure overlay via `infra/main.bicep` existence.
- `src/servicectl/cli.py`: added `modify` command with `--db`, `--show`, `--dry-run`, `--yes` options. Renders unified diff with color (green +/red -) and asks for confirmation before writing.

### Notes
- v1 of `modify` only supports `--db`. Future versions will add `--ci`, `--registry`, `--azure-region`, `--coverage`.
- Service-name modification is intentionally NOT supported in v1 (too destructive; would need a separate explicit-confirmation flow).
- Template modification is intentionally NOT supported in v1 (high risk of losing user edits; defer to v2).
- **9 new smoke tests** cover the `--db` flag:
  `test_db_default_is_postgres`, `test_db_mysql_emits_mysql_image_and_env`,
  `test_db_mssql_emits_mssql_image_and_env`, `test_db_cosmosdb_emits_emulator`,
  `test_db_dotnet_uses_correct_connection_string_key`,
  `test_db_go_webapi_supports_all_flavors`,
  `test_azure_overlay_emits_db_param_in_bicepparam`,
  `test_azure_bicep_uses_conditional_resource_blocks`,
  `test_db_validation_rejects_unknown_flavor`.

### Added
- **`--db` flag for database backend selection.** New `servicectl init` flag
  selects the database flavor for both local dev (docker-compose) and
  Azure deploy (Bicep). Supported values: `postgres` (default,
  Azure Database for PostgreSQL Flexible Server), `mysql` (Azure Database
  for MySQL Flexible Server), `mssql` (Azure SQL Database), `cosmosdb`
  (Azure Cosmos DB with SQL API).
  - Local dev: each template's `docker-compose.dev.yml` branches on `db`
    and emits the right Docker image, env var prefix, and healthcheck.
    Cosmos uses the Azure Cosmos DB Linux emulator (heavyweight: ~3GB
    memory, 30s+ start time).
  - Azure deploy: `infra/main.bicep` now uses conditional resource blocks.
    Only the chosen DB's resource type deploys (Postgres Flexible Server,
    MySQL Flexible Server, SQL Server + database, or Cosmos DB account).
    App Service appSettings include the right connection-string env vars
    per flavor.
  - SKU params in `infra/{dev,staging,prod}.bicepparam` cover all 4 flavors;
    Bicep ignores the ones not relevant to the chosen `--db`.
- **9 new smoke tests** cover the `--db` flag:
  `test_db_default_is_postgres`, `test_db_mysql_emits_mysql_image_and_env`,
  `test_db_mssql_emits_mssql_image_and_env`, `test_db_cosmosdb_emits_emulator`,
  `test_db_dotnet_uses_correct_connection_string_key`,
  `test_db_go_webapi_supports_all_flavors`,
  `test_azure_overlay_emits_db_param_in_bicepparam`,
  `test_azure_bicep_uses_conditional_resource_blocks`,
  `test_db_validation_rejects_unknown_flavor`.

### Changed
- `src/servicectl/cli.py`: added `--db` Click option (`postgres` default,
  `postgres|mysql|mssql|cosmosdb` Choice). Added `db` to the success panel.
- `src/servicectl/generator.py`: added `db` field to `ServiceGenerator`,
  validates against `SUPPORTED_DBS`, exposes `db`, `db_azure_resource_type`,
  `db_docker_image`, `db_env_prefix`, `db_healthcheck_cmd` in the Jinja
  rendering context.
- `src/servicectl/templates/python-flask|node-express|dotnet-webapi|go-webapi/docker-compose.dev.yml.j2`:
  rewrote with `{% if db == '...' %}` conditional blocks for all 4 DB
  flavors.
- `src/servicectl/deploy/azure/infra/main.bicep.j2`: rewrote with
  conditional resource blocks for the 4 DB types and a conditional
  `appSettings` array that emits the right connection-string env vars
  per flavor.

### Added (Unreleased prior work)
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
