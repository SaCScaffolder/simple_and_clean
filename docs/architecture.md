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