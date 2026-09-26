# Docs

Documentation for `servicectl` and the polyglot subpackages under
`src/`. The top-level [`README.md`](../README.md) is the public face of
the project; the docs in this folder explain the *why* and the *how*.

## Start here

| Doc | Read this if... |
|---|---|
| [`quickstart.md`](quickstart.md) | ...you just cloned the repo and want to scaffold your first service. Three entry points (Python CLI, TS SDK, Go sidecar) with the right call for each context. |
| [`architecture.md`](architecture.md) | ...you want to understand why the repo has three language subpackages, what each one owns, and where the source of truth lives. |
| [`development.md`](development.md) | ...you want to work on the repo. Toolchain setup, per-subpackage commands, common tasks, and gotchas. |
| [`ci.md`](ci.md) | ...a CI job failed and you want to know why, or you want to add a new matrix job for a subpackage. |

## Per-subpackage docs

The detailed reference for each subpackage lives next to its code:

- Python CLI — [`src/servicectl/`](../src/servicectl/) and the
  [top-level README](../README.md)
- Go sidecar validator — [`src/doctor-go/README.md`](../src/doctor-go/README.md)
- TypeScript SDK — [`src/sdk-ts/README.md`](../src/sdk-ts/README.md)
- Go wrapper CLI — [`src/servicectl-go/README.md`](../src/servicectl-go/README.md)
- Layout overview — [`src/PACKAGES.md`](../src/PACKAGES.md)

## Conventions

- **Voice.** The docs match the tone of the top-level README:
  declarative, slightly opinionated, no fluff. "Patterns, not opinions."
- **Source of truth.** The Python CLI under `src/servicectl/` owns the
  rules. If a doc describes a check or a flag, the implementation is in
  Python; the Go and TypeScript packages mirror or wrap it. See
  [`architecture.md`](architecture.md#source-of-truth) for the full
  rule.
- **Updated when.** A doc change goes in the same PR as the code change
  it describes. Out-of-date docs are worse than no docs.