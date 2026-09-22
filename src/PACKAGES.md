# Subpackages

This directory contains the three language subpackages that make up the
`servicectl` platform stack. Each is independently buildable, tested, and
documented; the Python CLI is the source of truth for scaffolding, and the
Go and TypeScript packages are affordance layers on top of it.

```
src/
├── servicectl/      Python CLI — the source of truth for scaffolding
├── doctor-go/       Go sidecar validator — fast focused subset of doctor
└── sdk-ts/          TypeScript SDK — typed wrapper around the CLI
```

## Why a polyglot layout?

`servicectl` is a platform tool that emits services in multiple runtimes
(Node, Python, .NET, soon Go and Rust). The platform tooling itself should
reflect that reality rather than fight it. Three runtimes in `src/` is
deliberate — each subpackage uses the right tool for its job:

- **Python** for the scaffolder. Rich templating (Jinja2), batteries-included
  stdlib, and a CLI ecosystem (Click, Rich) that makes interactive UX easy.
  This is where the actual scaffolding logic lives.
- **Go** for fast-path validation. Sub-millisecond cold start, single static
  binary, stdlib-only. Suitable for pre-commit hooks and tight CI gates where
  spinning up Python would dominate runtime.
- **TypeScript** for the SDK. Platform teams that want to drive scaffolding
  from chatops bots, internal tools, or CI scripts need typed access. Reaching
  for `child_process` and parsing CLI output works once, then it doesn't.

The Python CLI is the **source of truth**. The Go and TypeScript packages
do NOT re-implement scaffolding or validation — they call into or mirror
the Python logic. This is the discipline that prevents drift.

## Per-package development

### `src/servicectl/` — Python

Toolchain: Python 3.10+, pip.

```bash
pip install -e .                    # editable install
python -m servicectl init my-svc --template=python-flask
python tests/test_smoke.py          # smoke tests
```

See [../../README.md](../../README.md) for the full Python CLI reference.

### `src/doctor-go/` — Go

Toolchain: Go 1.22+ (tested on 1.27).

```bash
cd src/doctor-go
go test ./...                       # run unit tests
go vet ./...                        # static checks
go build -o doctor-go .             # build the binary
./doctor-go --path ../servicectl_test_samples/sample-scaffolds/surfside_pos_dup
```

See [./doctor-go/README.md](./doctor-go/README.md) for the full reference,
exit codes, and CI integration examples.

### `src/sdk-ts/` — TypeScript

Toolchain: Node 20+, npm, TypeScript 5+.

```bash
cd src/sdk-ts
npm install                         # install dev deps
npm run typecheck                   # tsc --noEmit
npm test                            # vitest run
npm run build                       # emits dist/
```

See [./sdk-ts/README.md](./sdk-ts/README.md) for the full API reference
and usage examples.

## Adding a new subpackage

If you add a new language subpackage:

1. Place it under `src/<name>-<lang>/`.
2. Add a README documenting the scope (what it does, what it does NOT do,
   and which package is the source of truth for shared logic).
3. Add a `.gitignore` for build outputs.
4. Extend `.github/workflows/ci.yml` with a new matrix job.
5. Add a row to the table above.
6. Update [../../CHANGELOG.md](../../CHANGELOG.md) under `[Unreleased]`.
7. Update [../../README.md](../../README.md) if the public surface changes.

## Boundaries — what each package owns

| Concern | Owner |
|---|---|
| Templating and rendering | `servicectl` (Python) |
| Service-name validation rules | `servicectl` (Python) — others mirror |
| File-presence checks | Both `servicectl.doctor` (full) and `doctor-go` (focused subset) |
| Multi-stage Dockerfile detection | Both (mirror logic) |
| Plaintext-secret detection | Both (`doctor-go` is coarse; Python doctor delegates to gitleaks in CI) |
| CLI invocation from scripts/SDKs | `sdk-ts` |
| Typed `ServiceConfig` shape | `sdk-ts` (mirror of Python CLI flags) |
| Exit-code semantics | Both (0=clean, 1=warn, 2=error) |

If a rule changes in `servicectl.doctor`, update `doctor-go` to match (or
document why it deliberately diverges). Don't let the rules drift silently.
