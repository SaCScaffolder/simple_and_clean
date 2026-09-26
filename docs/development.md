# Development

How to work on this repo. Covers toolchain setup per subpackage, the
commands you'll actually run, the test surfaces, and the gotchas that
have bitten me once so they don't bite you.

If you only need to run the tests, scroll to **Quick reference** at the
bottom.

---

## Repo layout

```
simple_and_clean/
├── src/
│   ├── servicectl/         Python CLI — source of truth for scaffolding
│   ├── doctor-go/          Go sidecar validator — fast subset of doctor
│   └── sdk-ts/             TypeScript SDK — typed wrapper around the CLI
├── tests/                  Cross-subpackage smoke tests
│   ├── test_smoke.py       Python scaffolder end-to-end
│   └── test_doctor.py      Python doctor end-to-end
├── docs/                   This folder
├── .github/workflows/ci.yml
├── pyproject.toml          Python packaging
├── doctor.cmd              Windows convenience wrapper around `servicectl doctor`
├── README.md
└── CHANGELOG.md
```

Each subpackage under `src/` is independently buildable, testable, and
publishable. The Python CLI is the **source of truth** for shared rules —
the Go and TypeScript packages mirror or wrap it, they don't re-implement
it. See [`architecture.md`](architecture.md) for the full reasoning.

## Prerequisites

| Toolchain | Version | Used by |
|---|---|---|
| Python | 3.10+ (3.13 tested) | `src/servicectl/`, top-level smoke tests |
| Go | 1.22+ (1.27 tested) | `src/doctor-go/` |
| Node.js | 20+ (20 and 22 tested) | `src/sdk-ts/` |
| Azure CLI + Bicep | for Bicep validation | optional, only if editing the Azure overlay |
| Git | any recent | all of the above |

## First-time setup

```bash
git clone https://github.com/henryorsborn/simple_and_clean
cd simple_and_clean
```

You can develop any of the three subpackages in isolation — you don't
have to install all three toolchains to work on one. Pick the section
below that matches what you're changing.

### Python (`src/servicectl/` + top-level tests)

```bash
python -m pip install --upgrade pip setuptools wheel
pip install -e .                # editable install from pyproject.toml

# Verify
python -m servicectl --help
python tests/test_smoke.py      # smoke suite — under a second on a warm cache
python tests/test_doctor.py     # doctor suite
```

The editable install puts `servicectl` on PATH for the current virtualenv
(or system Python if you're not in a venv — please use a venv).

### Go (`src/doctor-go/`)

```bash
cd src/doctor-go
go test ./...                   # unit tests, <100ms
go vet ./...                    # static checks
go build -o doctor-go .         # build the binary
./doctor-go --version
```

The test suite is table-driven against `t.TempDir()` fixtures — no
network, no external services.

### TypeScript (`src/sdk-ts/`)

```bash
cd src/sdk-ts
npm install                     # or `npm ci` for a clean install
npm run typecheck               # tsc --noEmit
npm test                        # vitest, <1s
npm run build                   # emits dist/
```

The SDK shells out to a `servicectl` binary on PATH. For local end-to-end
testing:

```bash
# In one shell: install the Python CLI editable (see above)
# In another shell:
cd src/sdk-ts
npm run build
node dist/index.js
```

---

## Working conventions

### Source-of-truth discipline

If you change a rule in the Python CLI (file presence check, exit code,
service-name validation, templated file shape), update `doctor-go` and
`sdk-ts` to match in the same change. The polyglot layout only works if
the rules don't drift.

If `doctor-go` deliberately diverges from the Python doctor (because it's
a focused subset), that divergence is documented at the top of
`src/doctor-go/README.md`. Don't add new divergences silently — call
them out in the README or in the PR description.

### Tests live next to the code they test

- Python scaffolder and doctor tests live at the repo root under
  `tests/`. They're cross-subpackage: a smoke test scaffolds a real
  service and asserts the file shape.
- Go unit tests live next to the Go source as `*_test.go`.
- TypeScript unit tests live next to the TS source as `*.test.ts`.

When you add a check or a templated file, add the test for it in the
same change. CI runs all three suites on every push (see [`ci.md`](ci.md)).

### Commit messages

Conventional Commits. The repo's CHANGELOG is generated from commit
messages, and reviewers skim messages to find scope quickly.

```
feat(servicectl): add --no-readme flag
fix(doctor-go): treat single-stage Dockerfile as warning, not error
chore(ci): add Node 22 to sdk-ts matrix
docs(architecture): clarify source-of-truth rule
```

### Windows quirks

A few things to know if you're developing on Windows:

- `doctor.cmd` is a wrapper that handles UTF-8 console setup and forwards
  to `python -m servicectl doctor`. It's the recommended entry point on
  Windows because raw `python -m servicectl doctor` can mangle Unicode
  glyphs (✓ ✗) in the default code page.
- The CI matrix runs both Ubuntu and Windows. If a test is flaky on
  Windows, fix it on Windows — don't `.skip()` it. The whole point of
  the matrix is to catch platform-specific behavior.
- Path separators in Python code: use `pathlib.Path` and
  `os.path.join`, not string concatenation. The smoke suite already does
  this correctly.

---

## Common tasks

### Adding a new templated file to an existing template

1. Add the file under `src/servicectl/templates/<id>/<path>`. If it has
   Jinja placeholders, name it `<filename>.j2`.
2. Add the file path to the template's static-file or templated-file list
   in `src/servicectl/templates.py`.
3. Add a smoke test in `tests/test_smoke.py` asserting the file is emitted
   when the template renders.
4. If the file affects a doctor check (e.g. a new required file),
   update `src/servicectl/doctor.py` and mirror it in
   `src/doctor-go/main.go`.

### Adding a new service template

1. Create `src/servicectl/templates/<id>/`.
2. Register the template ID in `src/servicectl/templates.py` and
   `src/servicectl/cli.py`.
3. Add the template ID to the SDK's `ServiceConfig` union in
   `src/sdk-ts/src/index.ts`.
4. Add the template ID to the Go wrapper's `validTemplate` switch and
   the `tpl*` constants in `src/servicectl-go/main.go`. Add a unit test
   case in `main_test.go`.
5. Add a smoke test that scaffolds a service with the new template and
   asserts the expected files are present.
6. Update `src/PACKAGES.md`, `README.md`, `docs/quickstart.md`, and
   `CHANGELOG.md` (under `[Unreleased]`).

**Template conventions:**

- **Backend templates** (Node-Express, Python-Flask, .NET, Go) ship a
  Postgres-backed dev stack via `docker-compose.dev.yml`. They reference
  Postgres via env vars in `.env.example`.
- **Frontend templates** (Node-React-Web) ship **without** a database
  and **without** a docker-compose dev stack. They configure the API
  base URL via `VITE_*` env vars (baked at build time) and ship with an
  nginx runtime instead of a node server. Add a regression test that
  asserts the absence of DB deps (see
  `test_node_react_web_no_db_in_scaffold` for the pattern).

### Adding a new deploy target

1. Create `src/servicectl/deploy/<target>/`.
2. Register the target ID in `src/servicectl/cli.py` and
   `src/servicectl/templates.py`.
3. Add a smoke test that asserts the overlay's files are emitted when
   `--deploy=<target>` is passed and absent otherwise.
4. Update the `ServiceConfig` union in `src/sdk-ts/src/`.
5. If the target needs new doctor checks (e.g. Bicep files for Azure),
   add them to `src/servicectl/doctor.py` and mirror in
   `src/doctor-go/main.go`.

### Adding a new subpackage under `src/`

1. Place it at `src/<name>-<lang>/`.
2. Add a README that opens with a **Scope** block: what this package
   does, what it does NOT do, which other subpackage is the source of
   truth for shared logic.
3. Add a `.gitignore` for build outputs.
4. Extend `.github/workflows/ci.yml` with a new matrix job (Python / Go
   / Node as appropriate).
5. Add a row to the table in [`src/PACKAGES.md`](../src/PACKAGES.md).
6. Update [`architecture.md`](architecture.md) with a one-paragraph
   description of the new subpackage's role.
7. Update [`CHANGELOG.md`](../CHANGELOG.md) under `[Unreleased]`.

---

## Gotchas

These are real things that have bitten during development. Read them
once before you start.

### `doctor-go` is a subset, not a duplicate

If you add a check to `src/servicectl/doctor.py` and forget to mirror it
in `src/doctor-go/main.go`, `doctor-go` will silently pass on services
that the Python doctor would flag. The reverse is fine — `doctor-go` is
allowed to have *fewer* checks than the Python doctor, but not *different*
checks.

### `sdk-ts` does not own CLI flags

Adding a new flag to `servicectl init` requires adding it to both the
Python CLI *and* the TypeScript `ServiceConfig`. The SDK does not
auto-discover CLI flags. Run the SDK test suite after changing CLI
flags — the test for arg construction will fail and remind you.

### Bicep validation in CI is best-effort

The Bicep job in `.github/workflows/ci.yml` is `continue-on-error: true`
on purpose. Bicep CLI behavior varies slightly between versions and
platforms, and a failure there usually indicates a Bicep version
mismatch rather than a real bug in the generated code. Validate Bicep
locally with the Bicep CLI before opening a PR; rely on the CI job as a
backstop, not as a primary signal.

### Don't add new top-level dependencies casually

`servicectl` ships as a pip-installable CLI. Every new top-level
dependency in `pyproject.toml` is a tax on every install. Prefer the
stdlib; reach for a third-party package only when it pays for itself
(Jinja2, Click, Rich all earned their place). Same applies to
`doctor-go` — stdlib-only is a feature, not a constraint.

---

## Quick reference

```bash
# Python (scaffolder + doctor)
pip install -e .
python tests/test_smoke.py
python tests/test_doctor.py

# Go (doctor sidecar)
cd src/doctor-go
go test ./...
go vet ./...
go build -o doctor-go .

# TypeScript (SDK)
cd src/sdk-ts
npm install
npm run typecheck
npm test
npm run build
```