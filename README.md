# servicectl

[![CI](https://github.com/henryorsborn/servicectl/actions/workflows/ci.yml/badge.svg)](https://github.com/henryorsborn/servicectl/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: v0.1.0](https://img.shields.io/badge/status-v0.1.0-orange.svg)](CHANGELOG.md)

**One command. Production-grade service. Green CI in under 60 seconds.**

`servicectl` is a self-service scaffolding tool for platform engineers. It
generates the boring-but-essential bits every new service needs — Dockerfile,
CI pipeline, tests with coverage thresholds, health checks, secrets baseline,
container scanning, dev container — so you can spend your time on the code
that actually matters.

Sensible defaults so a single command gives you something deployable. Every
default overridable through flags and templates, because every team is a
little weird.

Built from patterns I learned running deployment orchestration for 1,700+
services at Microsoft.

## Polyglot by design

`servicectl` is a platform tool that emits services in multiple runtimes.
The platform tooling itself reflects that reality — four language
subpackages under one umbrella, each using the right tool for its job:

| Subpackage | Language | Purpose | Source of truth? |
|---|---|---|---|
| [`src/servicectl/`](src/servicectl/) | Python | Scaffolder + full doctor | **Yes** |
| [`src/doctor-go/`](src/doctor-go/) | Go | Fast-path validator (focused subset of doctor) | No — mirrors Python |
| [`src/sdk-ts/`](src/sdk-ts/) | TypeScript | Typed wrapper around the CLI for chatops/CI/scripts | No — calls Python CLI |
| [`src/servicectl-go/`](src/servicectl-go/) | Go | Typed wrapper around the CLI for Go scripts/CI | No — calls Python CLI |

See [`src/PACKAGES.md`](src/PACKAGES.md) for the full layout and per-package
development instructions.

## Why this exists

I'm a platform engineer. The job I loved most was turning deployment
orchestration from a manual, error-prone process into something reproducible
and self-service — so engineers downstream of my work got hours back to
spend on code that actually mattered.

`servicectl` is the distillation of that work into a tool any engineer can
use in 60 seconds. One command scaffolds a production-grade service:
Dockerfile, CI pipeline, tests with coverage thresholds, health checks,
secrets scanning, container scanning, and dev container. Pick a template,
override what you need, push it. CI is green before you've finished reading
this README.

If you're evaluating this for a hiring loop: this repo is meant to show
how I think about platform defaults, security-by-default, and CI that
doesn't lie. See the **[For hiring managers](#for-hiring-managers)** section
below for the explicit mapping to my Microsoft work.

---

## Demo

```bash
$ pip install servicectl
$ servicectl init billing-api \
    --template=dotnet-webapi \
    --deploy=azure \
    --azure-region=centralus \
    --registry=acr \
    --coverage=85

  rendered 15 templated files
  copied   7 static files
✓ Created service billing-api
  template:    dotnet-webapi
  ci:          github-actions
  deploy:      azure (centralus)
  coverage:    85%
  registry:    acr

Next steps:
  cd billing-api
  az login
  az bicep build --file infra/main.bicep --outfile infra/main.json
  az deployment group create --resource-group <rg> \
      --template-file infra/main.bicep \
      --parameters infra/dev.bicepparam
```

You just generated **22 files** — Dockerfile, docker-compose, GitHub Actions
CI, xUnit test project, devcontainer, .env.example, Bicep infrastructure for
Azure App Service + ACR + Postgres Flexible Server, three environment-specific
parameter files, and a deploy workflow with health-check smoke tests.

Push it. CI runs end-to-end. PR is green. Done.

---

## What every template generates

| File | Why |
| --- | --- |
| `Dockerfile` | Multi-stage build, distroless or slim runtime, nonroot user |
| `docker-compose.dev.yml` | One command up: service + Postgres |
| `.devcontainer/devcontainer.json` | VS Code remote dev, ready to go |
| `.github/workflows/ci.yml` | lint → test (with coverage threshold) → build → scan → publish |
| `azure-pipelines.yml` | Same shape for Azure DevOps |
| `src/` + `tests/` | Minimal but real — healthz + readyz endpoints, sample tests |
| `.env.example` | Every required env var, no plaintext secrets |
| `.gitleaks.toml` | Secrets scanning baseline so accidental commits get caught |
| `README.md` | Stack-specific run / test / deploy instructions |

When `--deploy=azure`, the overlay adds:

| File | Why |
| --- | --- |
| `infra/main.bicep` | ACR + App Service Plan + App Service (Linux, container) + Postgres Flexible Server |
| `infra/{dev,staging,prod}.bicepparam` | Environment-specific SKUs and image tags |
| `deploy.yml` | GitHub Actions CD: OIDC login → build/push to ACR → `az deployment group create` → `/healthz` smoke test |

---

## Templates

- **`node-express`** — Node.js 20 + Express + PostgreSQL, Jest + Supertest, ESLint
- **`node-react-web`** — Node 20 + Vite + React 18 + TypeScript + Tailwind + shadcn/ui (SPA), nginx runtime, Vitest + Testing Library
- **`python-flask`** — Python 3.12 + Flask + PostgreSQL, pytest + ruff
- **`dotnet-webapi`** — .NET 8 Web API + PostgreSQL, xUnit + WebApplicationFactory
- **`go-webapi`** — Go 1.22+ + net/http + PostgreSQL, stdlib testing + Trivy scan, distroless static runtime

Adding a template is a `templates/<id>/` folder with `.j2` files where
`{{ service_name }}`, `{{ service_name_pascal }}`, `{{ coverage_threshold }}`,
etc. get substituted. PRs welcome.

## Deploy targets

- **`local`** (default) — Docker Compose, no cloud account required
- **`azure`** — Azure App Service (Linux, container), Bicep + bicepparam, OIDC-based CD
- `aws` — *coming next: CDK or Terraform for App Runner / ECS Fargate*
- `gcp` — *coming next: Cloud Run*

---

## Install

```bash
pip install servicectl
```

Or from source:

```bash
git clone https://github.com/henryorsborn/servicectl
cd servicectl
pip install -e .
```

Requires Python 3.10+.

## Quick start

```bash
# Pick a template and a name
servicectl init my-service --template=python-flask

# Override what you need to override
servicectl init billing-api \
    --template=dotnet-webapi \
    --ci=azure-devops \
    --deploy=azure \
    --coverage=90 \
    --registry=ghcr

# Don't want git initialized?
servicectl init scratch --template=node-express --no-git

# Need a README without the templated contents?
servicectl init internal-tool --template=python-flask --no-readme

# Validate an existing scaffolded service against servicectl standards
servicectl doctor                       # check current dir, human-readable
servicectl doctor my-service --json     # check a specific service, JSON output
servicectl doctor my-service --strict   # warnings count as errors (CI gate)
servicectl doctor my-service --pause    # wait for keypress before exit (for shortcut launches)
```

## Validating existing services with `doctor`

`servicectl doctor` checks an existing scaffolded service against the
same standards the scaffolder uses. It catches drift over time:
missing files, single-stage Dockerfiles, removed gitleaks config,
dropped Azure bicepparam files, and more.

```
$ servicectl doctor my-service
doctor: my-service  (deploy: azure)
===================================

ERRORS:
  [✓] PASS  file:Dockerfile — present
  [✓] PASS  dockerfile:multi-stage — 2 FROM instructions
  [✓] PASS  azure:infra/main.bicep — present
  ...

WARNS:
  [✗] FAIL  file:.gitleaks.toml — gitleaks baseline missing
          fix: add a `.gitleaks.toml` to enable secrets scanning in CI

summary: 12/17 passed; 0 errors, 1 warning, 0 info
```

**Exit codes** make it usable as a CI gate:
- `0` — clean
- `1` — warnings only
- `2` — errors (or warnings under `--strict`)

```yaml
# GitHub Actions example: run doctor on every PR
- name: servicectl doctor
  run: servicectl doctor ./my-service --strict
```

## CLI reference

```
servicectl init <name>
  --template=<node-express|python-flask|dotnet-webapi>      [required]
  --ci=<github-actions|azure-devops>                        [default: github-actions]
  --deploy=<local|azure|azure-container-apps>              [default: local]
  --azure-region=<region>                                   [default: eastus]
  --coverage=<0-100>                                        [default: 80]
  --registry=<dockerhub|ghcr|ecr|acr|gcr>                   [default: ghcr]
  --output-dir=<path>                                       [default: .]
  --no-git                                                  skip `git init`
  --no-readme                                               skip README generation

servicectl doctor [PATH]
  --json     output JSON instead of human-readable text
  --strict   treat warnings as errors (exit 2 if any warnings exist)
  --pause    wait for a keypress before exiting (for shortcut launches)
```

---

## Design principles

1. **Sensible defaults** so a single command is useful.
2. **Every default overridable** because real teams are weird.
3. **Generated code, not generated magic.** You can read every file we make and understand it. No hidden abstractions.
4. **Patterns, not opinions.** If you hate one of our choices, the file is right there — change it.
5. **No surprises in CI.** Tests pass on the generated output, locally and on GitHub Actions.

---

## For hiring managers

A portfolio piece demonstrating platform-engineering patterns from 4+ years
at Microsoft — specifically turning deployment orchestration from a manual,
error-prone process into something reproducible and self-service.

What it shows:

- **DevSecOps by default.** Every generated service ships with secrets scanning (gitleaks), container vulnerability scanning (Trivy, HIGH/CRITICAL fail), and least-privilege identity patterns (managed identity for ACR pull, no admin user on Postgres).
- **Multi-cloud posture.** The deploy-target flag lets a team add new clouds without forking the per-stack templates. Azure ships today; AWS and GCP are next.
- **CI that doesn't lie.** The CI workflow runs lint, tests with a coverage threshold, multi-stage build, scan, and publish — every stage gates the next. Same shape in GitHub Actions and Azure DevOps.
- **Real deployable output.** The generated Bicep doesn't just define a VM; it defines the full App Service + ACR + Postgres topology with environment-specific SKUs and a working CD workflow with health checks.

Microsoft-side context that this maps to: I ran orchestration for 1,700+
connector services and 200+ weekly deployments across commercial, government,
and sovereign Azure clouds; led the migration of a critical deployment
system from credential-based auth to passwordless; built the Safe Deployment
Practice safeguards (incident-aware rollouts, Last Known Good, automated
rollback paths); shipped platform security controls (sensitive-data scrubbing,
IP allowlisting automation, secret renewal, certificate lifecycle).

`servicectl` is the distillation of those patterns into a tool that any
engineer can use in 60 seconds.

---

## Roadmap

- [ ] `--deploy=aws` (CDK or Terraform, App Runner or ECS Fargate)
- [ ] `--deploy=gcp` (Cloud Run)
- [ ] `--deploy=azure-container-apps` (full implementation, not just a placeholder)
- [ ] `--template-dir=<path>` so users can extend without forking
- [ ] `servicectl doctor` — validate an existing project against the same standards
- [x] `go-webapi` template (`net/http`, distroless runtime, race-detector coverage gate) — shipped 2026-09-23
- [ ] Rust template (`rust-axum`), Java template (`spring-boot`)
- [ ] Pre-commit hook generator (gitleaks + ruff/eslint/dotnet format on commit)

## Contributing

Templates are folders under `src/servicectl/templates/<id>/`. Files ending in
`.j2` are Jinja-rendered; placeholders in the filename itself also get
substituted (see `dotnet-webapi/tests/{{ service_name_pascal }}.Tests/`).

To add a deploy target, drop files in `src/servicectl/deploy/<target>/` and
register it in `cli.py` and `templates.py`.

PRs welcome.

## Running the tests

```bash
pip install -e .
python tests/test_smoke.py
```

The smoke suite covers template registration, all three templates, dashed and
dotted names, dotnet PascalCase filename substitution, custom coverage
threshold, error paths, and the Azure overlay (files emitted when expected,
absent when not).

CI runs the suite on every push across Python 3.10-3.13 on both Ubuntu and
Windows, plus a Bicep compile check via the `azure-cli` Docker image.

## License

MIT.

## Author

Henry Orsborn — [henryorsborn@gmail.com](mailto:henryorsborn@gmail.com) —
[github.com/henryorsborn](https://github.com/henryorsborn)
