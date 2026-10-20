# Architecture

`servicectl` is a polyglot platform tool by design. This document explains
*why* the repo has three language subpackages, what each one owns, and how
they relate to each other.

If you only have two minutes, read the **Source of truth** callout in the
middle of this page. It explains the discipline that keeps the polyglot
layout from drifting.

---

## The shape

```
                ┌─────────────────────────────────────────────┐
                │                  CALLERS                    │
                │   Engineers using `servicectl init ...`     │
                │   Scripts / CI pipelines / chatops bots     │
                │   Pre-commit hooks / editor integrations    │
                └────────────────┬────────────────────────────┘
                                 │
            ┌───────────┬────────┼────────┬────────────┐
            │           │        │        │            │
            ▼           ▼        ▼        ▼            ▼
   ┌─────────────────┐ ┌────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
   │ src/sdk-ts/     │ │ src/servicectl/     │ │ src/doctor-go/       │ │ src/servicectl-go/   │
   │ TypeScript SDK  │ │ Python CLI          │ │ Go sidecar           │ │ Go wrapper CLI       │
   │                 │ │                     │ │                      │ │                      │
   │ Typed affordance│ │ Scaffolder + full   │ │ Fast focused subset  │ │ Typed affordance     │
   │ over the CLI.   │─│ doctor. Source of   │─│ of doctor. Mirror,   │─│ over the CLI.        │
   │ Calls into it.  │ │ truth.              │ │ not reimplementation.│ │ Calls into it.       │
   └─────────────────┘ └─────────┬───────────┘ └──────────────────────┘ └──────────────────────┘
                                 │
                                 │ renders templates,
                                 │ copies static files,
                                 │ runs `git init`
                                 ▼
                  ┌─────────────────────────────┐
                  │ Scaffolded service          │
                  │ (Node, Python, .NET, ...)   │
                  │   + Dockerfile              │
                  │   + CI workflow             │
                  │   + deploy manifests        │
                  └─────────────────────────────┘
```

Three runtimes, three jobs, one repo. Each subpackage uses the tool best
suited to what it does:

| Subpackage | Language | Why this language | Source of truth? |
|---|---|---|---|
| `src/servicectl/` | Python | Rich templating (Jinja2), batteries-included stdlib, mature CLI ecosystem (Click, Rich) for interactive UX. | **Yes** — owns templating, validation rules, file-presence checks. |
| `src/doctor-go/` | Go | Sub-millisecond cold start, single static binary, stdlib-only. The right shape for pre-commit hooks and tight CI gates. | No — mirrors a focused subset of the Python doctor's rules. |
| `src/sdk-ts/` | TypeScript | Platform teams driving scaffolding from scripts, CI pipelines, internal tools, and chatops bots want typed access. `child_process` + string parsing works once, then doesn't. | No — shells out to the Python CLI as the source of truth. |
| `src/servicectl-go/` | Go | Engineers who prefer Go for scripts / CI / chatops want a static binary that drives scaffolding the same way the TypeScript SDK does for Node. Single-file stdlib-only, shells out to Python. | No — shells out to the Python CLI as the source of truth. |

## Source of truth

**The Python CLI under `src/servicectl/` owns the rules.** The Go and
TypeScript packages do not re-implement scaffolding or validation — they
call into or mirror the Python logic.

This is the discipline that prevents drift. If you change a service-name
validation rule, a templated file, a file-presence check, or an exit code,
you change it in the Python package first. Then you update `doctor-go` or
`sdk-ts` to match (or document, in their README, why they deliberately
diverge).

> **Why not just rewrite the whole thing in one language?**
> Because the platform this tool emits is itself polyglot (Node, Python,
> .NET, soon Go and Rust). The tooling should reflect that reality rather
> than fight it. A platform team that ships Node services shouldn't have
> to install a Python interpreter to validate a pre-commit hook. A
> chatops bot in TypeScript shouldn't have to reimplement the templating
> logic to drive scaffolding.

## What each package owns

The detailed ownership table lives at
[`src/PACKAGES.md`](../src/PACKAGES.md). The short version:

| Concern | Owner |
|---|---|
| Templating and rendering | `servicectl` (Python) |
| Service-name validation rules | `servicectl` (Python) — others mirror |
| File-presence checks | Both `servicectl.doctor` (full) and `doctor-go` (focused subset) |
| Multi-stage Dockerfile detection | Both |
| Plaintext-secret detection | Both (`doctor-go` is coarse; Python doctor delegates to gitleaks in CI) |
| CLI invocation from scripts/SDKs | `sdk-ts` |
| Typed `ServiceConfig` shape | `sdk-ts` (mirror of Python CLI flags) |
| Exit-code semantics | Both (0=clean, 1=warn, 2=error) |

## How the pieces fit when you scaffold a service

End-to-end, a scaffold call looks like:

1. **Caller.** An engineer runs `servicectl init billing-api --template=...`
   from a terminal. Or a CI script imports `@servicectl/sdk` and calls
   `scaffold({ name, template, ... })`. Or a pre-commit hook runs
   `doctor-go` against an already-scaffolded service.
2. **SDK path (TypeScript only).** If the call came through `sdk-ts`, the
   SDK validates the `ServiceConfig` against the same flag shape as the
   Python CLI, then shells out to `servicectl init` with the equivalent
   arguments. The Python CLI does not know whether it was called directly
   or from the SDK.
3. **Python CLI.** Renders Jinja2 templates from
   `src/servicectl/templates/<id>/`, copies static files, applies the
   deploy-target overlay (e.g. Azure Bicep + bicepparam), runs `git init`
   unless `--no-git`, and emits the README.
4. **Output.** A scaffolded service directory containing the templated
   source, the Dockerfile, the CI workflow, the deploy manifests, and
   the test scaffolding. The caller can immediately `cd` into it and run
   the local dev loop.
5. **Drift check (optional, later).** The caller can later run
   `servicectl doctor` (full surface) or `doctor-go` (fast subset)
   against the scaffolded service to catch drift over time.

## Why this layout is "polyglot by design, not by accident"

A few signals that this is intentional, not inherited:

- **Every subpackage has its own CI matrix job**, with the language
  versions and OSes that matter for that language (see
  [`ci.md`](ci.md)). No single job gates on all three languages — each
  one is gated on its own.
- **Every subpackage has a README** that explicitly states what it does,
  what it does NOT do, and which other subpackage is the source of
  truth for shared logic. Read [`src/doctor-go/README.md`](../src/doctor-go/README.md)
  and [`src/sdk-ts/README.md`](../src/sdk-ts/README.md) — both lead with
  a "Scope" block calling this out.
- **`src/PACKAGES.md`** documents the layout, the toolchain versions per
  subpackage, and the rule for adding a new subpackage. The repo grows
  deliberately, not by accretion.

## Adding a new subpackage

The rule is in [`src/PACKAGES.md`](../src/PACKAGES.md) under "Adding a
new subpackage." The short version: place it under `src/<name>-<lang>/`,
write a README that explicitly scopes it, add a CI matrix job, and update
this document.

If you're adding a subpackage that *re-implements* a Python rule (rather
than mirroring or wrapping), that's a red flag. Either the Python CLI
should own the rule, or the divergence should be documented at the top
of the new subpackage's README.


## Postgres on GKE (Autopilot)

The GKE Autopilot overlay supports `--db=postgres` end-to-end. The
design has four pieces, each with a specific job, and the seams
between them are explicit. If you understand *why* each piece
exists, you can debug the deploy when it breaks; if you only
memorize the commands, you'll be stuck.

### The four pieces

```
                ┌─────────────────────────────────────────────┐
                │ GCP project (sacscaffolder)                 │
                │ ┌─────────────────────────────────────────┐ │
                │ │ VPC + private services range            │ │
                │ │                                         │ │
                │ │ ┌─────────────────────────────────┐     │ │
                │ │ │ Cloud SQL (postgres 16)        │     │ │
                │ │ │ private IP only                │     │ │
                │ │ └────────────┬────────────────────┘     │ │
                │ └──────────────┼──────────────────────────┘ │
                │                │ private IP (10.x)          │
                │ ┌──────────────▼──────────────────────────┐ │
                │ │ GKE Autopilot cluster (same VPC)       │ │
                │ │ ┌────────────────────────────────────┐ │ │
                │ │ │ Pod                                │ │ │
                │ │ │  ┌──────────┐  ┌─────────────────┐ │ │ │
                │ │ │  │ App      │  │ cloudsql-proxy  │ │ │ │
                │ │ │  │ :8080    │──│ :5432 localhost│ │ │ │
                │ │ │  └────┬─────┘  └────────┬────────┘ │ │ │
                │ │ └───────┼─────────────────┼──────────┘ │ │
                │ │         │ Service (ClusterIP)         │ │
                │ │         │ :80 -> :8080                │ │
                │ │         │ :5432 -> proxy :5432        │ │
                │ └─────────┼──────────────────────────────┘ │
                │           │                                │
                │  ┌────────▼───────────────────────────────┐ │
                │  │ GCE Ingress + ManagedCertificate      │ │
                │  │ host: <svc>.<lb-ip>.nip.io            │ │
                │  └────────────────────────────────────────┘ │
                └─────────────────────────────────────────────┘
                                ▲
                                │ WIF-authenticated deploy
                                │ (deploy.yml GitHub Actions)
                                │
                ┌───────────────┴─────────────────────────────┐
                │ Secret Manager (postgres password)         │
                │   ↳ deploy.yml reads on every push         │
                │   ↳ writes to k8s Secret db-credentials    │
                │   ↳ Pod envFrom db-credentials             │
                └─────────────────────────────────────────────┘
```

### Why each piece exists

- **VPC + private services range.** Cloud SQL's `private IP only`
  mode means the instance is reachable *only* from inside the
  VPC. The GKE cluster's pods are on a subnetwork of the same
  VPC, so the connection is internal. The `private services
  access` peering (set up by Terraform's
  `google_service_networking_connection`) is what makes
  Cloud SQL's IP range reachable from the cluster at all.

- **cloudsql-proxy sidecar.** Without it, every pod would need a
  static postgres password (or a JSON key for the API). The
  proxy runs in the same pod as the app, listens on
  `127.0.0.1:5432`, and mints an ephemeral TLS cert on every
  connection to the Cloud SQL Admin API. The cert is
  short-lived; the proxy rotates it transparently. The app
  connects to `127.0.0.1:5432` like it's a normal postgres
  server.

- **Workload Identity (k8s SA ↔ GCP SA).** The k8s ServiceAccount
  the proxy runs as is annotated with
  `iam.gke.io/gcp-service-account: <runtime-sa>@<project>.iam.gserviceaccount.com`.
  GKE reads this annotation and binds the k8s SA to the GCP SA.
  The GCP side of the binding is the
  `gcloud iam service-accounts add-iam-policy-binding` command
  in `gke-postgres-setup.md` (Half 2). When both halves are in
  place, the proxy can call `generateEphemeralCert` on
  `sqladmin.googleapis.com` without a JSON key.

- **Secret Manager + k8s Secret sync.** The cloudsql module
  generates a `random_password` and writes it to Secret Manager.
  The k8s Secret is the bridge between Secret Manager and the
  pod's `envFrom`. The deploy workflow re-creates the k8s
  Secret on every push via
  `kubectl create --dry-run=client -o yaml | kubectl apply -f -`,
  so a rotated password takes effect on the next deploy.

- **GCE Ingress + ManagedCertificate.** GCE Ingress creates a
  Google Cloud Load Balancer in front of the Service. The
  `networking.gke.io/v1` ManagedCertificate provisions a
  free Google Trust Services cert, which attaches to the LB
  once DNS resolves. For dev (nip.io hostnames) this is
  automatic; for prod (real domain) the user has to point
  an A record at the LB.

- **The deploy workflow does the secret sync and the
  ingress-host patch.** The secret sync has to run *before*
  `kubectl apply` (so the k8s Secret exists when the
  Deployment's `envFrom` resolves). The ingress-host patch
  has to run *after* `kubectl apply` (because the LB IP
  only exists once the Ingress is reconciled). The deploy
  workflow's step order in `deploy.yml.j2` reflects this.

### What the deploy workflow does NOT do

A few things that look manual but aren't (or that the workflow
deliberately does *not* do because they're one-time-per-project
or one-time-per-cluster):

- **WIF pool / provider / CI SA creation.** Lives in
  `infra/bootstrap.sh` and `infra/bootstrap.ps1` (one-time per project). Putting it in
  Terraform would require the GitHub repo name as a `variable`,
  which is a deploy-time input; keeping it in a shell script
  makes that natural.

- **The k8s SA ↔ GCP SA Workload Identity binding.** Lives in
  `gke-postgres-setup.md` (one command, one time per project,
  after the cluster and runtime SA both exist). Could be moved
  to a `google_service_account_iam_policy` resource in
  Terraform, but the timing — "after both resources exist" — is
  awkward to express declaratively, and the one-time cost is
  low.

- **`gcloud container clusters get-credentials`.** This runs on
  every deploy, not just the first. It's idempotent and fast
  (re-points the kubeconfig to the same cluster). It does
  *not* run on the bootstrap script because the cluster
  doesn't exist yet.

### Cost shape

For the locked-in config (`sacscaffolder` / `us-central1` /
`db-f1-micro` / GKE Autopilot), the monthly cost is dominated
by the GCE Load Balancer (~$18/mo) and the Cloud SQL
instance ($9/mo for `db-f1-micro`). GKE Autopilot is
per-pod-billed, so a service that receives minimal traffic
costs <$5/mo. Always-on total: ~$45-60/mo. For a portfolio
piece that only needs to be up for a demo, the
[tardown commands](gke-postgres-setup.md#teardown) drop the
cost to zero in ~3 minutes.

### Why not Cloud SQL Auth Proxy's sidecar-less mode?

Cloud SQL offers a "private IP only" instance that's reachable
from the VPC without the proxy. The proxy still adds value
even in that mode: it handles TLS cert rotation, it gives you
a single source of truth for the connection string
(`127.0.0.1:5432`), and it lets the app code stay
postgres-driver-agnostic (the app doesn't need to know it's
talking through a proxy). The "extra container" cost is ~50ms
of memory and zero CPU at idle. The alternative — using the
private IP directly and managing certs in the app — is more
code, more failure modes, and a worse portfolio story.

### Why nip.io?

`nip.io` is a free wildcard DNS service: any hostname of the
form `<anything>.<ip>.nip.io` resolves to `<ip>`. So once the
GCE LB has an IP (say, `35.192.0.1`), the hostname
`demo.35.192.0.1.nip.io` resolves to that IP without you
owning a domain or running a DNS server. The
`networking.gke.io/v1` ManagedCertificate will then issue a
Google Trust Services cert for that hostname. The result: a
real, working HTTPS URL on a real cluster, with no domain
purchase, no DNS setup, and a one-line teardown.

The catch: nip.io hostnames look weird in screenshots
(`demo.35.192.0.1.nip.io` instead of `demo.example.com`).
That's the explicit trade-off this overlay makes — fast and
free, but cosmetically raw. Production deploys swap in a real
domain by editing `k8s/overlays/prod/kustomization.yaml.j2`'s
Ingress patch.
