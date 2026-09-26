# @servicectl/sdk

A TypeScript SDK for programmatic use of the [servicectl](../../README.md)
CLI. Define a `ServiceConfig` in TypeScript, scaffold a service, and inspect
the result with typed accessors.

> **Scope.** This package is a typed affordance layer over the CLI. It
> does NOT re-implement the scaffolder in TypeScript — that would duplicate
> the Python logic and drift over time. The Python CLI stays the source
> of truth; this SDK calls into it.

## Why does this exist?

`servicectl` is a CLI first, but platform teams routinely want to drive
scaffolding from scripts, CI pipelines, internal tools, or chatops bots.
Reaching for `child_process` and string-parsing CLI output works once,
then it stops working.

This SDK gives you:

1. **Typed `ServiceConfig`** that mirrors the CLI's `--flags`, validated
   at runtime so you fail fast on bad input.
2. **`scaffold()`** that shells out to the CLI safely, with structured
   `ScaffoldError` on failure and a typed `ScaffoldResult` on success.
3. **Typed accessors** (`readScaffoldJSON`) for inspecting what was
   generated, so callers can react to file shape without re-implementing
   filesystem walks.

## Install

```bash
npm install @servicectl/sdk
```

Requires Node 20+ and a working `servicectl` installation on PATH
(`pip install servicectl` or `pipx install servicectl`).

## Quick start

```typescript
import { scaffold, readScaffoldJSON, ScaffoldError } from '@servicectl/sdk';

try {
  const result = await scaffold({
    name: 'billing-api',
    template: 'node-express',
    deploy: 'azure',
    azureRegion: 'centralus',
    coverage: 85,
  });

  console.log(`Scaffolded ${result.files.length} files at ${result.path}`);

  // Typed read of the generated package.json
  const pkg = await readScaffoldJSON<{ name: string; scripts: Record<string, string> }>(
    result.path,
    'package.json',
  );
  console.log('test script:', pkg?.scripts.test);
} catch (err) {
  if (err instanceof ScaffoldError) {
    console.error(`scaffold failed (exit ${err.exitCode}):`, err.stderr);
  } else {
    throw err;
  }
}
```

## API

### `ServiceConfig`

```typescript
interface ServiceConfig {
  name: string;                          // required, lowercase-with-dashes
  template: 'node-express' | 'node-react-web' | 'python-flask' | 'dotnet-webapi' | 'go-webapi';
  ci?: 'github-actions' | 'azure-devops';
  deploy?: 'local' | 'azure' | 'azure-container-apps';
  azureRegion?: string;
  coverage?: number;                     // 0-100, default 80
  registry?: 'dockerhub' | 'ghcr' | 'ecr' | 'acr' | 'gcr';
  outputDir?: string;
  noGit?: boolean;
  noReadme?: boolean;
}
```

### `scaffold(config, options?)`

Returns `Promise<ScaffoldResult>`. Throws `ScaffoldError` on non-zero exit.

```typescript
const result = await scaffold(
  { name: 'my-svc', template: 'python-flask' },
  {
    binary: '/custom/path/to/servicectl',
    timeoutMs: 60_000,
    extraArgs: ['--verbose'],
  },
);
```

### `readScaffoldJSON<T>(serviceRoot, relativePath)`

Reads and parses a JSON file from a scaffolded service. Returns `null` if
the file doesn't exist. Throws if the file exists but isn't valid JSON.

### `resolveConfig(config)` (exported for testing)

Applies defaults and validates a partial `ServiceConfig`. Throws
`TypeError` or `RangeError` on bad input.

## Development

```bash
cd src/sdk-ts
npm install
npm run typecheck    # tsc --noEmit
npm test             # vitest run
npm run build        # emits dist/
```

The test suite runs in under a second and covers:

- Config validation (name shape, coverage range, required fields)
- Default application
- CLI arg construction
- Error class shape
- File-not-found handling

## License

MIT — same as the parent project.
