# Continuous Integration

What runs in CI, why each job exists, and how to interpret a failure.

The single CI workflow lives at
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml). It runs on every
push to `main` and every pull request targeting `main`.

---

## Jobs at a glance

```
┌─────────────────────────────────────────────────────────────────┐
│                  ci.yml  (4 jobs, parallel)                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────┐                                   │
│  │ test (Python)           │  matrix: 8 cells                   │
│  │  Python 3.10–3.13       │  (4 versions × 2 OSes)            │
│  │  Ubuntu + Windows       │                                   │
│  └─────────────────────────┘                                   │
│                                                                 │
│  ┌─────────────────────────┐                                   │
│  │ doctor-go (Go)          │  matrix: 4 cells                   │
│  │  Go 1.22 + 1.27         │  (2 versions × 2 OSes)            │
│  │  Ubuntu + Windows       │                                   │
│  └─────────────────────────┘                                   │
│                                                                 │
│  ┌─────────────────────────┐                                   │
│  │ sdk-ts (TypeScript)     │  matrix: 4 cells                   │
│  │  Node 20 + 22           │  (2 versions × 2 OSes)            │
│  │  Ubuntu + Windows       │                                   │
│  └─────────────────────────┘                                   │
│                                                                 │
│  ┌─────────────────────────┐                                   │
│  │ bicep-validate (Azure)  │  single job, ubuntu-latest        │
│  │  best-effort            │  continue-on-error: true          │
│  └─────────────────────────┘                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

All four jobs run in parallel. A failure in any one job fails the
overall CI run. The Bicep job is the exception — see below.

## Per-job details

### `test` — Python scaffolder + doctor

Runs the smoke suite (`tests/test_smoke.py`) and the doctor suite
(`tests/test_doctor.py`) on every supported Python version × OS
combination.

| Dimension | Values | Why |
|---|---|---|
| Python | 3.10, 3.11, 3.12, 3.13 | `servicectl` requires 3.10+ in `pyproject.toml`. The matrix tests the floor (3.10) and the latest stable (3.13), plus both intermediates. |
| OS | Ubuntu, Windows | Catches platform-specific bugs early. Windows is where path-handling and console-encoding bugs hide. |

**What it gates.** A failure means a real change to the scaffolder or
doctor broke one of the smoke or doctor tests. Investigate before
merging.

**How to run locally.**

```bash
pip install -e .
python tests/test_smoke.py
python tests/test_doctor.py
```

### `doctor-go` — Go sidecar validator

Builds, vets, and tests the `doctor-go` binary.

| Dimension | Values | Why |
|---|---|---|
| Go | 1.22, 1.27 | 1.22 is the minimum supported version. 1.27 is the latest stable at the time of writing. Both must pass. |
| OS | Ubuntu, Windows | Same reasoning as the Python job. |

Each cell runs three steps in order: `go vet`, `go test`, `go build`. A
failure at any step fails the cell.

**What it gates.** A failure means a change to `src/doctor-go/main.go`
broke the unit tests, introduced a vet warning, or broke the build. All
three are real signals — don't ignore vet warnings.

**How to run locally.**

```bash
cd src/doctor-go
go test ./...
go vet ./...
go build -o doctor-go .
```

### `sdk-ts` — TypeScript SDK

Installs dependencies, typechecks, runs vitest, and builds.

| Dimension | Values | Why |
|---|---|---|
| Node | 20, 22 | Node 20 is the current LTS. Node 22 is the latest stable. Both must pass. |
| OS | Ubuntu, Windows | Same reasoning. |

Each cell runs four steps in order: `npm ci`, `npm run typecheck`,
`npm test`, `npm run build`. A failure at any step fails the cell.

**What it gates.** A typecheck failure means the SDK doesn't compile
cleanly against the TypeScript types — a real bug. A test failure means
a behavior regression. A build failure means the package can't be
published.

**How to run locally.**

```bash
cd src/sdk-ts
npm install
npm run typecheck
npm test
npm run build
```

### `bicep-validate` — Azure overlay (best-effort)

Scaffolds a sample service with `--deploy=azure`, pins a specific Bicep
CLI version, and validates the generated `infra/main.bicep` and the
three `infra/<env>.bicepparam` files.

**This job is `continue-on-error: true` on purpose.** The Bicep CLI
behavior varies slightly between versions and platforms, and a failure
here usually indicates a Bicep version mismatch rather than a real bug
in the generated code (which is tested locally during development). A
failure here will not block the main test suite from passing; it shows
up as a red ✗ on the PR but does not fail required checks.

If you're changing the Azure overlay, validate Bicep locally with the
Bicep CLI before opening a PR. Rely on this CI job as a backstop, not as
a primary signal.

**Why keep it at all if it's best-effort?** Because *most* of the time
it works, and when it does fail it's worth knowing. Catching a real
Bicep regression occasionally is better than never.

---

## Why a per-language matrix instead of one big job

The polyglot repo layout (see [`architecture.md`](architecture.md)) is
matched by a per-language matrix. Each subpackage is gated on the
versions and OSes that matter for that subpackage. The alternative —
one job that installs Python, Go, and Node and runs everything — would
be slower, would conflate unrelated failures, and would not exercise the
language versions that actually matter.

If you add a new subpackage under `src/`, add a new matrix job for it.
Don't fold it into an existing job.

## Failure triage

| Symptom | First thing to check |
|---|---|
| `test` fails on Windows, passes on Ubuntu | Console encoding / path separator / line-ending issue. Reproduce locally with `python tests/test_smoke.py` on Windows. |
| `doctor-go` fails with a vet warning | Real bug. Vet warnings are not suppressed in this repo. Fix or restructure before merging. |
| `sdk-ts` fails on `npm ci` | `package-lock.json` is out of sync with `package.json`. Run `npm install` locally and commit the updated lockfile. |
| `sdk-ts` fails typecheck but tests pass | Type drift. The TypeScript types don't match the runtime behavior. Look for a recent change to `ServiceConfig` or `ScaffoldResult`. |
| `bicep-validate` fails | Check the Bicep CLI version locally (`bicep --version`). Compare to the pinned version in `ci.yml`. Usually a version mismatch. |
| All jobs fail simultaneously | Probably a transient runner issue. Re-run the workflow. If it persists, check the GitHub Actions status page. |

## Local parity

The CI matrix is the source of truth for "does this build cleanly?"
If you can run all three local suites and pass, your PR will almost
certainly pass CI. If a CI job fails and your local suite doesn't
reproduce it, that's a CI environment problem — open an issue rather
than papering over it locally.

```bash
# The full local pre-push check, from repo root:
pip install -e .
python tests/test_smoke.py
python tests/test_doctor.py
( cd src/doctor-go && go test ./... && go vet ./... && go build -o doctor-go . )
( cd src/sdk-ts && npm install && npm run typecheck && npm test && npm run build )
```

If all four blocks pass, push with confidence. 😘