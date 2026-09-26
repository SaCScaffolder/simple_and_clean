# servicectl-go

> A thin Go wrapper around the `servicectl` Python CLI. Use it when you
> want to scaffold services from Go code, Go scripts, or any context where
> shelling out to a Python interpreter is awkward.

`servicectl-go` is the Go counterpart to `servicectl init`. It does **not**
re-implement the scaffolder. The Python CLI under `src/servicectl/` is the
source of truth; this binary is a typed affordance layer on top of it. See
[`docs/architecture.md`](../../docs/architecture.md#source-of-truth) for the
discipline that keeps the polyglot layout from drifting.

---

## Table of contents

1. [Why this exists](#why-this-exists)
2. [Install](#install)
3. [Quick start](#quick-start)
4. [All flags](#all-flags)
5. [Usage patterns](#usage-patterns)
6. [Behavior and guarantees](#behavior-and-guaranteies)
7. [Comparison with the Python CLI and the TypeScript SDK](#comparison-with-the-python-cli-and-the-typescript-sdk)
8. [When to use what](#when-to-use-what)
9. [Examples](#examples)
10. [Errors and exit codes](#errors-and-exit-codes)
11. [Output shape](#output-shape)
12. [Limitations](#limitations)
13. [Troubleshooting](#troubleshooting)
14. [Development](#development)
15. [Testing locally](#testing-locally)
16. [FAQ](#faq)

---

## Why this exists

`servicectl` is a polyglot platform tool: Python scaffolder, templates that
emit Go / Node / Python / .NET services, and platform tooling that should
reflect that reality rather than fight it. We already have:

- **The Python CLI** (`src/servicectl/`) — the source of truth.
- **The TypeScript SDK** (`src/sdk-ts/`) — a typed affordance layer over
  the Python CLI for chatops bots, internal tools, and CI scripts that
  live in Node.

What's been missing is the **Go counterpart**. Engineers who prefer Go
(or scripts / CI / chatops in Go) want a static binary that drives
scaffolding the same way `sdk-ts` drives it from Node.

`doctor-go` exists for the same reason — a Go affordance layer on top of
the Python `servicectl doctor`. `servicectl-go` is the *write* counterpart
to that *validate* binary.

**It does not re-implement the scaffolder.** It parses a typed `Config`
struct, builds the same flags the Python CLI accepts, and shells out to
`python -m servicectl init <name> [flags]`. The Python CLI remains the
single source of truth for template rendering, file emission, and naming
validation. Any change to a template or a flag flows through automatically
without re-building the Go binary.

---

## Install

### From source (recommended during development)

```bash
cd src/servicectl-go
go build -o servicectl-go .
# On Windows you can build the .exe variant with the same command; go
# produces both depending on the file extension you pass.
go build -o servicectl-go.exe .
```

The binary is self-contained and statically linked by default. No Go
runtime is needed on the target machine.

### From `go install` (once published)

```bash
go install github.com/henryorsborn/simple_and_clean/src/servicectl-go@latest
```

This puts `servicectl-go` on `$GOBIN` (usually `~/go/bin`). Make sure
`$GOBIN` is on your `PATH`.

### Pre-requisites on the target machine

`servicectl-go` is a thin wrapper — it does not contain any of the
scaffolding logic. To actually run a scaffold, the machine needs:

1. **A Python interpreter** on `PATH` (3.10+). The Python CLI is
   installed via `pip install -e .` from the repo root.
2. **The `servicectl` Python package** installed (so `python -m servicectl`
   resolves). Install with:
   ```bash
   git clone https://github.com/henryorsborn/simple_and_clean
   cd simple_and_clean
   pip install -e .
   ```
3. **Jinja2, Click, Rich** — pulled in transitively by `pip install`.

That's it. No Docker, no template files copied, no template engine
embedded. The Go binary's job ends at "translate my typed Config into
shell-out args."

---

## Quick start

```bash
# Scaffold a Go service with the go-webapi template, no git init, no README.
servicectl-go init my-svc --template=go-webapi --no-git --no-readme

# Same thing, with the service name after the flags (both forms work):
servicectl-go init --template=go-webapi --no-git --no-readme my-svc

# Show the resolved config and service root as JSON (handy for scripts):
servicectl-go init my-svc --template=go-webapi --json

# Print the help text:
servicectl-go help
servicectl-go init --help

# Print the version:
servicectl-go version
```

The Python CLI handles all template rendering and file emission. The Go
binary just translates your typed config into the right shell args.

---

## All flags

The `init` subcommand accepts exactly the flags the Python CLI accepts,
plus three wrapper-only flags.

### Required

| Flag | Description |
|---|---|
| `--template=<id>` | Service template. One of: `node-express`, `node-react-web`, `python-flask`, `dotnet-webapi`, `go-webapi`. (The list mirrors `src/servicectl/templates.py` and the SDK's `TemplateId` union.) |

The service name is the first positional argument (e.g. `my-svc`).

### Optional (mirror the Python CLI)

| Flag | Default | Allowed values |
|---|---|---|
| `--ci=<provider>` | `github-actions` | `github-actions`, `azure-devops` |
| `--deploy=<target>` | `local` | `local`, `azure`, `azure-container-apps` |
| `--azure-region=<region>` | `eastus` | any Azure region name |
| `--coverage=<0-100>` | `80` | integer 0-100 |
| `--registry=<reg>` | `ghcr` | `dockerhub`, `ghcr`, `ecr`, `acr`, `gcr` |
| `--output-dir=<path>` | `.` | any existing or new directory path |
| `--no-git` | `false` | skip `git init` after scaffolding |
| `--no-readme` | `false` | skip README generation (not recommended) |

### Wrapper-only

| Flag | Default | Description |
|---|---|---|
| `--python=<path>` | `python` | Python interpreter to invoke. Use `python3` on systems where `python` is Python 2. |
| `--timeout=<duration>` | `2m` | Max time to wait for the Python CLI to finish. Accepts Go duration syntax (`30s`, `2m`, `1h`). |
| `--json` | `false` | After the Python CLI finishes, print the resolved config and the absolute service root path as JSON to stdout. The Python CLI's own output (its panel, summary, etc.) still appears before this JSON block. |

### Name validation rules

The service name must match `^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$`:

- Lowercase letters, digits, `-`, `_`, `.`
- Must start and end with a letter or digit
- No spaces, no shell-unsafe characters

This matches the Python CLI's validation. Examples: `billing-api` ✓,
`my.cool.api` ✓, `BillingAPI` ✗, `bad/name` ✗.

---

## Usage patterns

### Form 1: name first, flags after

```bash
servicectl-go init dispatch-router --template=go-webapi --no-git
```

This is the form the Python CLI uses natively. The Go wrapper detects the
positional name and moves it to the end before parsing flags. Both forms
work; pick whichever you find more readable.

### Form 2: flags first, name last (Go convention)

```bash
servicectl-go init --template=go-webapi --no-git dispatch-router
```

This is the form most Go CLIs use (`kubectl`, `docker`, `gh`).

### Form 3: name after `--` (when the name starts with `--`)

```bash
servicectl-go init -- --weird-name  # not actually supported; just use a non-weird name
```

The `--` separator is handled correctly: anything after `--` is treated as
positional. (You won't need this in practice; the name validation rejects
anything starting with `-`.)

---

## Behavior and guarantees

### What `servicectl-go` does for you

1. **Parses typed flags** into a `Config` struct with the same shape as
   the Python CLI's flags and the SDK's `ServiceConfig`.
2. **Validates** the template id, name shape, and coverage range before
   invoking anything. Bad input fails fast without spawning Python.
3. **Translates** the config into `python -m servicectl init <name> [flags]`.
4. **Forwards** the Python CLI's stdout/stderr to your terminal so you see
   the same scaffold output you'd see running `servicectl init` directly.
5. **Surfaces** failures with a clear error message and a non-zero exit
   code.

### What `servicectl-go` deliberately does NOT do

- **Re-implement templating.** Jinja lives in Python; the Go binary has
  zero template knowledge.
- **Re-implement validation.** Bad template ids and bad names are caught
  by the Go binary before spawning Python, but anything more sophisticated
  (template-specific checks, deploy-target-specific file emission) lives
  in the Python CLI.
- **Bypass the source-of-truth rule.** If you change a flag in the Python
  CLI, you may need to add it here too — but that's just updating a few
  lines. There's no template logic to keep in sync.
- **Run in the background or queue work.** It's a synchronous shell-out.
  The Python CLI does whatever it does and exits; the Go binary exits
  with the same code.

### Failure modes

| What happens | Exit code |
|---|---|
| Bad flag value (invalid template id, invalid name, coverage out of range) | `1` (Go wrapper caught it before spawning Python) |
| Python not found on PATH | `1` (with `failed to invoke python: ...`) |
| Python CLI returned non-zero exit (e.g. directory already exists) | `1` (with `servicectl init failed with exit code N`) |
| Python CLI timed out | `1` (with `servicectl init timed out after Xs`) |
| Successful scaffold | `0` |

The Go wrapper's exit codes don't follow the Python CLI's exit code
convention (0 / 1 / 2 for ok / warn / error). The wrapper is binary:
either the scaffold succeeded (exit 0) or something went wrong (exit 1).

For finer-grained failure info, run the Python CLI directly.

---

## Comparison with the Python CLI and the TypeScript SDK

`servicectl-go` is the third entry point. They all do the same thing —
scaffold a service by rendering templates — but they suit different
contexts.

| | Python CLI | TypeScript SDK | Go wrapper (this) |
|---|---|---|---|
| **Source form** | `servicectl` command (Python) | `@servicectl/sdk` npm package | `servicectl-go` static binary |
| **Best for** | Terminal use, ad-hoc scaffolds | Node.js scripts, CI, chatops | Go scripts, CI, chatops, tools that prefer a static binary |
| **Validation** | At parse time + render time | At parse time + render time (via Python CLI) | At parse time only; render-time validation via Python CLI |
| **Typed Config** | N/A (CLI flags) | Yes (`ServiceConfig` interface) | Yes (`resolvedConfig` struct) |
| **Output** | Rich panel + summary | `ScaffoldResult` interface | Resolved config + service root (with `--json`) |
| **Adds drift risk?** | N/A | No — shells out to Python CLI | No — shells out to Python CLI |
| **Can run without Python installed?** | No | No (shells out) | No (shells out) |

**None of these re-implement the scaffolder.** They all converge on
`python -m servicectl init <name> [flags]`.

---

## When to use what

Use the **Python CLI** when you're at a terminal and just want to scaffold
something. It's the simplest path.

Use the **TypeScript SDK** when you're writing Node code, a chatops bot,
or an internal portal that drives scaffolding from TypeScript.

Use **servicectl-go** (this binary) when:

- You're writing a Go tool that drives scaffolding (e.g. a custom CLI
  that wraps multiple platform operations).
- You're writing Go-based CI scripts that scaffold services from PRs.
- You're distributing a single static binary to a team that doesn't have
  Python installed (and the binary itself calls Python on a backend).
- You want type-safe config in Go without re-implementing template logic.

---

## Examples

### Example 1: Scaffold four Go services for the Dispatch project

```bash
servicectl-go init dispatch-router    --template=go-webapi --no-git --no-readme
servicectl-go init dispatch-notifier --template=go-webapi --no-git --no-readme
servicectl-go init dispatch-ingest   --template=go-webapi --no-git --no-readme
servicectl-go init dispatch-admin    --template=go-webapi --no-git --no-readme
```

Each one creates `dispatch-<name>/` in the current directory with the full
go-webapi scaffold (cmd/server, internal/<name>, Dockerfile, CI workflow,
gitleaks config, etc.).

### Example 2: Scaffold a Node service with Azure deploy

```bash
servicectl-go init billing-api \
    --template=node-express \
    --deploy=azure \
    --azure-region=westus2 \
    --coverage=90 \
    --registry=acr
```

Scaffolds `billing-api/` with Node + Express + PostgreSQL, Azure App
Service + ACR + Postgres Flexible Server deploy manifests, 90% coverage
threshold, ACR registry.

### Example 3: Use `--json` from a script

```bash
RESULT=$(servicectl-go init my-svc --template=go-webapi --json 2>/dev/null)
echo "$RESULT" | jq -r '.serviceRoot'
# Output: /current/working/dir/my-svc
```

The JSON output goes to **stdout** after the Python CLI's output. To
extract just the JSON, redirect the Python CLI's output to /dev/null
(or use `--output-dir` to a known location and parse the result there).
For cleaner scripting, run the Python CLI directly and parse its output —
the Go wrapper's JSON output is a thin convenience, not a full API.

### Example 4: Timeout a long-running scaffold

```bash
servicectl-go init my-svc --template=go-webapi --timeout=30s
```

If the Python CLI takes longer than 30 seconds, the Go wrapper kills it
and exits 1 with `servicectl init timed out after 30s`.

### Example 5: Use a different Python interpreter

```bash
servicectl-go init my-svc --template=go-webapi --python=python3
```

Useful on macOS / Linux systems where `python` is Python 2 (deprecated).

---

## Errors and exit codes

The wrapper exits with:

| Exit code | Meaning |
|---|---|
| `0` | Successful scaffold |
| `1` | Bad input (invalid flag, invalid name, missing template) OR Python invocation failed (Python not found, non-zero exit from the CLI, timeout) |

The wrapper's exit codes are deliberately binary — it's a thin shell-out
wrapper, not a multi-stage validator. For fine-grained failure info, run
the Python CLI directly.

### Common error messages

```
servicectl-go: unknown subcommand "foo"
  → Run `servicectl-go help` to see available commands.

servicectl-go: missing required argument: <name>
  → You didn't pass a service name. Form: `servicectl-go init <name> [flags]`.

servicectl-go: invalid service name "BillingAPI"
  → Names must match `^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$`.

servicectl-go: missing required flag: --template
  → --template is required. Allowed: node-express, node-react-web, python-flask, dotnet-webapi, go-webapi.

servicectl-go: invalid --template "foo"
  → Pick one of the five valid template ids.

servicectl-go: invalid --coverage 150
  → Coverage must be 0-100.

servicectl-go: servicectl init failed with exit code 2
  → The Python CLI returned non-zero. Run `servicectl init ...` directly for a clearer error.

servicectl-go: servicectl init timed out after 2m0s
  → Bump --timeout or run the Python CLI directly.

servicectl-go: failed to invoke python: exec: "python": executable file not found in %PATH%
  → Python isn't on PATH. Install it or pass --python=/path/to/python3.
```

---

## Output shape

By default, `servicectl-go init` is silent — it forwards the Python CLI's
output to your terminal. The Python CLI prints:

```
rendered 10 templated files
  copied   4 static files
╭─ service scaffolded ─╮
│ ✓ Created service X  │
│ ...
╰──────────────────────╯
```

With `--json`, the wrapper additionally prints a JSON object to stdout
*after* the Python CLI's output:

```json
{
  "config": {
    "name": "dispatch-router",
    "template": "go-webapi",
    "ci": "github-actions",
    "deploy": "local",
    "azure_region": "eastus",
    "coverage": 80,
    "registry": "ghcr",
    "output_dir": ".",
    "no_git": true,
    "no_readme": true
  },
  "serviceRoot": "C:\\Users\\henry\\source\\repos\\sac\\simple_and_clean\\dispatch-router"
}
```

The JSON object describes what was *configured*, not what was *rendered*.
For a list of rendered files, run `servicectl doctor ./dispatch-router`
or read the directory listing.

---

## Limitations

- **Single subcommand.** Only `init` is implemented. `doctor` from Go is
  the separate `doctor-go` binary. If you need a Go validator, use that
  binary directly.
- **No streaming output.** The Python CLI's stdout/stderr are streamed to
  your terminal line-by-line, but the wrapper itself doesn't expose a
  streaming API for embedding.
- **No programmatic API.** This is a CLI, not a Go library. If you want
  to drive scaffolding from Go code without spawning a subprocess, you
  have to import the Python CLI directly (out of scope for this binary).
- **No Windows-specific path handling.** Paths are passed through as
  strings. The Python CLI handles them. Don't expect the Go wrapper to
  rewrite `\` to `/` or vice versa.
- **No environment-variable fallback.** All config comes from flags. The
  Python CLI doesn't read config from env either, so this is consistent.

---

## Troubleshooting

### `python: executable file not found in %PATH%`

Install Python 3.10+ or pass `--python=python3` (or `--python=/path/to/python`).

### `target directory already exists`

The Python CLI refuses to overwrite an existing directory. Pick a
different name or remove the directory first.

### Scaffold succeeded but `go build` fails in the generated service

The template emits files but doesn't run `go mod tidy` for you. After
scaffolding a Go service:

```bash
cd my-svc
go mod tidy
go test ./...
go run ./cmd/server
```

### Flags don't seem to apply

Make sure you're passing them with the correct syntax. `--template=foo`
and `--template foo` both work. Single-dash variants like `-template` are
NOT supported (Go's flag package only supports `-flag` for single-letter
flags).

### Output is silent

The wrapper forwards stdout/stderr. If you don't see anything, the
Python CLI didn't print anything either — which usually means it ran
into an error before rendering. Try running `python -m servicectl init
<name> --<flags>` directly to see the error.

---

## Development

### Repo layout

```
src/servicectl-go/
├── main.go              # the whole wrapper, ~9KB, stdlib only
├── README.md            # this file
├── go.mod               # module declaration
└── servicectl-go(.exe)  # built binary (gitignored)
```

Single-file layout, mirroring `doctor-go`. The wrapper is small enough
that splitting it into multiple files would be premature.

### Build

```bash
cd src/servicectl-go
go build -o servicectl-go .       # Linux / macOS
go build -o servicectl-go.exe .   # Windows
```

`go build` produces a static binary by default (`CGO_ENABLED=0`).

### Cross-compile

```bash
# From any host, build for Linux:
GOOS=linux GOARCH=amd64 go build -o servicectl-go-linux .

# For macOS ARM (Apple Silicon):
GOOS=darwin GOARCH=arm64 go build -o servicectl-go-darwin-arm64 .

# For Windows from Linux/macOS:
GOOS=windows GOARCH=amd64 go build -o servicectl-go.exe .
```

### Adding a new template

The four templates are listed in three places that must stay in sync:

1. `src/servicectl/templates.py` — the source of truth.
2. `src/sdk-ts/src/index.ts` — TypeScript union.
3. `src/servicectl-go/main.go` — `tplNodeExpress` / `tplPythonFlask` /
   `tplDotnetWebAPI` / `tplGoWebAPI` constants and the `validTemplate`
   function.

If you add a template in the Python CLI, add the constant here too. The
`validTemplate` switch must be updated. That's it — no other changes
needed (the Python CLI handles rendering).

### Adding a new flag

If you add a flag to the Python CLI, mirror it here by adding another
`fs.String(...)` (or `Bool`/`Int`/`Duration`) call in `runInit`, then
include it in the `cliArgs` slice that builds the shell-out args. Keep
the field name and JSON tag in `resolvedConfig` aligned with the SDK's
`ServiceConfig` for consistency.

---

## Testing locally

### Smoke test from the wrapper

```bash
cd simple_and_clean
pip install -e .
cd src/servicectl-go
go build -o servicectl-go.exe .

# Pick a temp target so you don't litter the repo.
mkdir -p /tmp/svc-smoke && cd /tmp/svc-smoke
"C:\Users\henry\source\repos\sac\simple_and_clean\src\servicectl-go\servicectl-go.exe" \
    init demo-svc --template=go-webapi --no-git --no-readme

# Verify the scaffold:
cd demo-svc
ls -la
go vet ./...
go test ./...
```

### Run the Python CLI directly to compare

```bash
# Same scaffold, but driven by Python. The output should match.
servicectl init demo-svc --template=go-webapi --no-git --no-readme
```

### Unit tests for the wrapper itself

The wrapper has no unit tests yet (it's small enough that integration
testing through the Python CLI is sufficient). If you want to add unit
tests, `splitName` is the obvious target — it's pure-function and easy to
table-test:

```go
func TestSplitName(t *testing.T) {
    cases := []struct {
        in       []string
        wantName string
        wantArgs []string
    }{
        {[]string{}, "", []string{}},
        {[]string{"my-svc"}, "my-svc", []string{}},
        {[]string{"my-svc", "--template=foo"}, "my-svc", []string{"--template=foo"}},
        {[]string{"--template=foo", "my-svc"}, "my-svc", []string{"--template=foo"}},
        {[]string{"--template=foo", "my-svc", "--no-git"}, "my-svc", []string{"--template=foo", "--no-git"}},
    }
    for _, tc := range cases {
        name, args := splitName(tc.in)
        if name != tc.wantName || !reflect.DeepEqual(args, tc.wantArgs) {
            t.Errorf("splitName(%v) = (%q, %v), want (%q, %v)", tc.in, name, args, tc.wantName, tc.wantArgs)
        }
    }
}
```

Add this to `src/servicectl-go/main_test.go` if you want it tracked.

---

## FAQ

**Q: Why is this a separate binary instead of a Go package?**
A: Distribution. A static binary that calls Python is easier to ship than
a Go module that callers need to import. The wrapper itself doesn't do
enough to justify being a library — it's a CLI-shaped tool.

**Q: Couldn't this just be a shell script?**
A: Yes, but you lose type safety, you lose Windows portability without
two scripts, and you lose the JSON output. The Go binary is ~250 lines of
real code and handles Windows + Linux + macOS uniformly.

**Q: Does the wrapper cache anything?**
A: No. Every invocation shells out to Python. If you're scaffolding in a
loop, that's fine — Python startup is fast. If you're scaffolding
thousands of services in parallel, run the Python CLI directly in each
goroutine.

**Q: Can I use this in CI?**
A: Yes. Build the binary once (in your image or via `go install`), then
call it from any CI step. The wrapper forwards the Python CLI's exit
code, so a failed scaffold fails the CI step.

**Q: What if I want to add a Go-specific template variant?**
A: Add it to `templates.py` (Python) and the SDK's `TemplateId` union
(TypeScript) first. Then add the constant and update `validTemplate` in
this file. Three small edits.

**Q: Why does `splitName` exist? Doesn't Go's flag package intersperse?**
A: No, it doesn't. As of Go 1.27, `flag.FlagSet.Parse` still stops at
the first non-flag argument. The `splitName` helper moves the first
positional to the end so the Python CLI's flag convention
(`init <name> [flags]`) works without surprise.

---

## License

MIT — same as the parent project.