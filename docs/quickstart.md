# Quickstart

Five minutes from "I've cloned the repo" to "I've scaffolded a service and
validated it." Three entry points are shown — pick the one that matches
your context.

If you only have two minutes, read **Which entry point should I use?**
at the top, then jump to that section.

---

## Which entry point should I use?

| If you are... | Use | Why |
|---|---|---|
| ...a platform engineer driving scaffolding from a terminal. | **Python CLI** (`servicectl init ...`) | The source of truth. Always works. |
| ...writing a chatops bot, CI pipeline, or internal tool that scaffolds services. | **TypeScript SDK** (`@servicectl/sdk`) | Typed `ServiceConfig`, structured `ScaffoldError`, fits the Node.js ecosystem. |
| ...adding a pre-commit hook or a fast CI gate that validates an *already-scaffolded* service. | **Go sidecar** (`doctor-go`) | Sub-millisecond cold start, single static binary, no Python interpreter required. |
| ...writing Go scripts, Go-based CI, or a chatops bot that prefers a Go static binary. | **Go wrapper** (`servicectl-go`) | Typed `resolvedConfig`, single static binary, shells out to the Python CLI. |
| ...writing a Go service. | **Python CLI directly** *or* **Go wrapper**. The `go-webapi` template ships; either entry point works. | The scaffold emits a working Go service that you customize. |

The Python CLI is the source of truth for all rules. The TypeScript SDK
calls into it. The Go sidecar mirrors a focused subset of `doctor` for
fast validation. See [`architecture.md`](architecture.md#source-of-truth)
for the discipline that keeps the polyglot layout from drifting.

---

## What this doc assumes you've already done

```bash
git clone https://github.com/henryorsborn/simple_and_clean
cd simple_and_clean
```

If you need help installing the toolchains, see
[`development.md`](development.md#prerequisites).

---

## Entry point 1 — Python CLI (terminal)

The CLI is the source of truth. Every other entry point ends up calling
this.

### Install

```bash
pip install -e .
```

Verify:

```bash
servicectl --version
servicectl init --help
```

### Scaffold a service

```bash
# Minimal: pick a template and a name
servicectl init my-service --template=node-express

# Override what you need to override
servicectl init billing-api \
    --template=dotnet-webapi \
    --ci=azure-devops \
    --deploy=azure \
    --azure-region=centralus \
    --coverage=90 \
    --registry=acr

# Skip git init / README generation
servicectl init scratch --template=python-flask --no-git --no-readme
```

You'll get a panel that lists the rendered files, the chosen template /
CI / deploy target, and the exact `cd && az ...` sequence to bring it up
in your target environment.

### Validate an existing service

```bash
# Human-readable
servicectl doctor

# Specific path
servicectl doctor ./my-service

# JSON for piping into jq or CI
servicectl doctor ./my-service --json

# Strict mode (warnings count as errors, exit 2)
servicectl doctor ./my-service --strict
```

Exit codes: `0` clean, `1` warnings only, `2` errors (or warnings under
`--strict`). Use these in CI.

### Available templates today

| ID | Stack | Status |
|---|---|---|
| `node-express` | Node.js 20 + Express + PostgreSQL | shipped |
| `node-react-web` | Node 20 + Vite + React 18 + TypeScript + Tailwind + shadcn/ui (SPA) | shipped |
| `python-flask` | Python 3.12 + Flask + PostgreSQL | shipped |
| `dotnet-webapi` | .NET 8 Web API + PostgreSQL | shipped |
| `go-webapi` | Go 1.22+ + net/http + PostgreSQL, distroless static runtime | shipped |

The available templates are listed programmatically by `servicectl init
--help`. Run that to see what's current.

---

## Entry point 2 — TypeScript SDK (`@servicectl/sdk`)

The SDK is a typed affordance layer over the Python CLI. Use it when
you're driving scaffolding from a Node.js context — chatops bots, CI
scripts, internal tools, or the `admin-web` part of a multi-service
platform.

### Install

```bash
# From the repo
cd src/sdk-ts
npm install
npm run build

# Or, once published:
npm install @servicectl/sdk
```

Requires Node 20+ and a working `servicectl` binary on PATH (`pip install
-e .` from the repo root, or `pipx install servicectl`).

### Scaffold a service

```typescript
import { scaffold, readScaffoldJSON, ScaffoldError } from '@servicectl/sdk';

try {
  const result = await scaffold({
    name: 'billing-api',
    template: 'node-express',
    deploy: 'azure',
    azureRegion: 'centralus',
    coverage: 90,
  });

  console.log(`Scaffolded ${result.files.length} files at ${result.path}`);

  // Typed read of the generated package.json
  const pkg = await readScaffoldJSON<{
    name: string;
    scripts: Record<string, string>;
  }>(result.path, 'package.json');

  console.log('test script:', pkg?.scripts.test);
} catch (err) {
  if (err instanceof ScaffoldError) {
    console.error(`scaffold failed (exit ${err.exitCode}):`, err.stderr);
  } else {
    throw err;
  }
}
```

### Templates available through the SDK today

The SDK's `TemplateId` union currently exposes:

```typescript
export type TemplateId =
  | 'node-express'
  | 'node-react-web'
  | 'python-flask'
  | 'dotnet-webapi'
  | 'go-webapi';
```

All four are first-class. Pass any of them and the SDK will shell out to
the Python CLI with the matching `--template=...` flag.

### When to use the SDK vs the Python CLI

| Context | Use |
|---|---|
| One-off scaffold from a terminal | Python CLI |
| CI pipeline that scaffolds a service per PR | Python CLI inside a step |
| Chatops bot written in TypeScript | SDK |
| Internal portal that lets engineers self-serve a new service | SDK |
| Pre-commit hook or editor integration | SDK (Node-friendly) or Python CLI |

The SDK is a wrapper, not a replacement. If the Python CLI's flags
change, the SDK test suite flags it on the next CI run (the
`buildArgs` test asserts the canonical flag set).

---

## Entry point 3 — Go sidecar (`doctor-go`)

`doctor-go` is a focused subset of `servicectl doctor`, written in Go,
stdlib-only, single static binary. It's the right tool for pre-commit
hooks and tight CI gates where spinning up Python would dominate the
runtime.

### Install

```bash
# From the repo
cd src/doctor-go
go build -o doctor-go .
./doctor-go --version

# Or, once published:
go install github.com/henryorsborn/simple_and_clean/src/doctor-go@latest
```

### Validate a service

```bash
# Current directory, human-readable
doctor-go

# Specific service
doctor-go --path ./my-service

# JSON output for piping
doctor-go --path ./my-service --json

# Strict mode (warnings count as errors)
doctor-go --path ./my-service --strict
```

Exit codes: `0` clean, `1` warnings only, `2` errors (or warnings under
`--strict`). Same semantics as the Python doctor.

### Use as a pre-commit hook

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: doctor-go
        name: servicectl doctor (Go sidecar)
        entry: doctor-go --path . --strict
        language: system
        pass_filenames: false
```

### Use as a CI gate

```yaml
# .github/workflows/ci.yml
- name: doctor-go gate
  run: |
    go install github.com/henryorsborn/simple_and_clean/src/doctor-go@latest
    doctor-go --path ./my-service --strict
```

### What doctor-go does NOT check

`doctor-go` is a focused subset. The Python `servicectl doctor` has a
fuller check surface (coverage-threshold detection per template,
advanced Dockerfile linting, gitleaks delegation). For the full surface,
run `servicectl doctor` from the Python package. The two are not
duplicates; `doctor-go` is the fast path, the Python doctor is the
authoritative path.

---

## Entry point 4 — Go wrapper (`servicectl-go`)

`servicectl-go` is the *write* counterpart to `doctor-go`. It's a thin
Go binary that parses typed config, builds the right shell-out args, and
invokes `python -m servicectl init`. Single static binary, stdlib only.

Use it when you're writing Go scripts, Go-based CI, or a chatops tool
that prefers a Go static binary over the Python CLI.

### Install

```bash
# From the repo
cd src/servicectl-go
go build -o servicectl-go .       # Linux / macOS
go build -o servicectl-go.exe .   # Windows

# Or, once published:
go install github.com/henryorsborn/simple_and_clean/src/servicectl-go@latest
```

Requires Go 1.22+ to build; no runtime dependency on Go. To actually
scaffold a service, the machine also needs Python 3.10+ and the
`servicectl` Python package installed (`pip install -e .` from the repo
root, or `pipx install servicectl`).

### Scaffold a service

```bash
# Both forms work — Go convention (flags first) or Python convention (name first):
servicectl-go init --template=go-webapi --no-git --no-readme dispatch-router
servicectl-go init dispatch-router --template=go-webapi --no-git --no-readme

# JSON output for scripting
servicectl-go init --template=go-webapi --json dispatch-router | tail -1 | jq -r .serviceRoot

# Different Python interpreter (e.g. on systems where `python` is Python 2)
servicectl-go init --template=go-webapi --python=python3 my-svc
```

See [`src/servicectl-go/README.md`](../src/servicectl-go/README.md) for
the full reference, all flags, error codes, and troubleshooting.

### When to use the Go wrapper vs the Python CLI

| Context | Use |
|---|---|
| One-off scaffold from a terminal | Python CLI |
| CI pipeline that scaffolds a service per PR (in bash/yaml) | Python CLI inside a step |
| Chatops bot written in TypeScript | TypeScript SDK |
| Chatops bot written in Go | Go wrapper |
| Internal portal that lets engineers self-serve a new service | TypeScript SDK |
| Go tool that wraps multiple platform operations and wants typed config | Go wrapper |

The Go wrapper is a wrapper, not a replacement. It shells out to the
Python CLI for all rendering. If the Python CLI's flags change, the
wrapper's `resolvedConfig` struct may need a field added — but the
template logic stays in one place.

## Scaffolding a React frontend (SPA)

The `node-react-web` template ships. To scaffold a frontend SPA:

```bash
# Python CLI
servicectl init admin-web --template=node-react-web

# Or via the TypeScript SDK
```

```typescript
import { scaffold } from '@servicectl/sdk';

const result = await scaffold({
  name: 'admin-web',
  template: 'node-react-web',
  ci: 'github-actions',
  deploy: 'local',
  coverage: 80,
});
```

The scaffold emits a Vite + React + TypeScript + Tailwind + shadcn/ui SPA
with a multi-stage Docker build, nginx runtime, Vitest tests, and a
GitHub Actions + Azure DevOps CI matrix.

### What the scaffold gives you

- **Vite 5 dev server** with HMR, listening on `0.0.0.0:5173` so it works
  inside containers / remote dev environments. Proxies `/api/*` to
  `VITE_API_PROXY_TARGET` (defaults to `http://localhost:8080`) so the
  SPA can use relative `/api` paths without CORS gymnastics in dev.
- **Multi-stage Dockerfile**: `node:20-alpine` for the build stage
  (npm ci → vite build), `nginx:1.27-alpine` for the runtime. The final
  image is just nginx + your static `dist/` folder — no node runtime.
- **SPA-friendly nginx config**: hard refresh and deep links work
  because unknown routes fall back to `index.html`.
- **shadcn/ui preconfigured** with the slate base color and CSS variables
  for light/dark mode. One example primitive (`Button`) ships so the
  pattern is obvious. Add more with `npx shadcn@latest add <name>`.
- **React Router** wired up with two placeholder pages (Home, 404).
  Replace them with your real routes.
- **Axios instance** with `VITE_API_BASE_URL` baked at build time and
  commented-out interceptors for auth tokens and global error handling.
- **Vitest + @testing-library/react** tests for App routing and `cn()`
  helper. Coverage gate enforced in CI via vitest thresholds.
- **GitHub Actions** + **Azure DevOps** CI: typecheck → lint
  (`--max-warnings 0`) → test (with configurable coverage threshold) →
  multi-stage Docker build → Trivy scan → publish on push to `main`.
- **TypeScript strict mode** with `@/` path alias mapped to `./src/`,
  matching the `components.json` shadcn config.

### Customizing the scaffold

Once scaffolded, common edits:

- Replace `src/pages/HomePage.tsx` with your real landing page.
- Add routes in `src/App.tsx`.
- Add shadcn primitives: `npx shadcn@latest add card dialog form`.
- Configure auth: edit `src/api/client.ts` to wire token refresh.
- Update `VITE_API_BASE_URL` at build time (it's a build arg, not a
  runtime secret).
- Set up production analytics / Sentry / etc. via additional Vite
  plugins in `vite.config.ts`.

## Scaffolding a Go service

The `go-webapi` template is shipped. To scaffold a Go service:

```bash
# Python CLI
servicectl init router --template=go-webapi

# Or via the TypeScript SDK
```

```typescript
import { scaffold } from '@servicectl/sdk';

const result = await scaffold({
  name: 'router',
  template: 'go-webapi',
  ci: 'github-actions',
  deploy: 'local',
  coverage: 85,
});
```

The scaffold emits a `cmd/server/main.go` entrypoint and an
`internal/<service>/` handler package with snake_case substitution.
For example, `servicectl init router` produces `internal/router/`.

### What the scaffold gives you

- **Go 1.22+ module** at `go.mod` (default module path:
  `github.com/henryorsborn/<service>` — change before publishing).
- **Multi-stage Dockerfile**: `golang:1.22-bookworm` for the build stage,
  `gcr.io/distroless/static-debian12:nonroot` for the runtime. Static
  binary, no libc dependency, minimal attack surface.
- **`/healthz` and `/readyz` endpoints** out of the box (the baseline
  scaffold returns 200 for both — wire real readiness checks as the
  service grows).
- **Graceful shutdown** on SIGINT/SIGTERM with a 10s drain window.
- **GitHub Actions** + **Azure DevOps** CI with: `go vet` →
  `go test -race -coverprofile` (with configurable coverage threshold) →
  multi-stage Docker build → Trivy scan (HIGH/CRITICAL = fail) →
  publish on push to `main`.
- **`docker-compose.dev.yml`** with Postgres so you can run
  `docker compose up` and have a working stack in 30 seconds.
- **`.gitleaks.toml`**, **`.env.example`**, **`.dockerignore`**,
  **`.devcontainer/`** — the same scaffolding shape as the other
  templates.

### Customizing the scaffold

Once scaffolded, common edits:

- Add a real handler in `internal/<service>/` (e.g. `internal/router/handlers.go`).
- Replace the placeholder `Server` struct fields with real dependencies
  (DB pool, logger, config).
- Update `go.mod`'s module path from `github.com/henryorsborn/<service>`
  to your real module path before publishing.
- Add OpenTelemetry, slog, or zap in `internal/<service>/logger.go`.
- Add auth middleware in `internal/<service>/middleware.go`.

---

## Putting it together — the realistic first day

```bash
# 1. Install
git clone https://github.com/henryorsborn/simple_and_clean
cd simple_and_clean
pip install -e .
( cd src/doctor-go && go build -o doctor-go . )

# 2. Scaffold (Python CLI, Option C from above)
servicectl init router --template=dotnet-webapi
# repeat for notifier, ingest, admin-api

# 3. Validate (Go sidecar)
./src/doctor-go/doctor-go --path ./router

# 4. Run the local test suite for the whole repo
python tests/test_smoke.py
python tests/test_doctor.py
( cd src/doctor-go && go test ./... )
( cd src/sdk-ts && npm install && npm test )
```

If all four blocks pass, you're set up correctly. From here, the
[`development.md`](development.md) guide covers everything else.

---

## Getting help

- **README** (top-level) — public face of the project, demo, CLI
  reference, hiring-manager mapping.
- **Source-of-truth rule** —
  [`architecture.md`](architecture.md#source-of-truth)
- **Toolchain setup, common tasks, gotchas** —
  [`development.md`](development.md)
- **CI matrix and failure triage** — [`ci.md`](ci.md)
- **Subpackage details** —
  [`src/doctor-go/README.md`](../src/doctor-go/README.md),
  [`src/sdk-ts/README.md`](../src/sdk-ts/README.md),
  [`src/PACKAGES.md`](../src/PACKAGES.md)