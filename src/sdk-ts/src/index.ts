/**
 * @servicectl/sdk — typed wrapper around the servicectl CLI.
 *
 * Why this exists:
 *
 *   servicectl is a CLI first, but platform teams routinely want to drive
 *   scaffolding from scripts, CI pipelines, internal tools, or chatops bots.
 *   Reaching for `child_process` and string-parsing CLI output works once,
 *   then it stops working. This SDK gives you:
 *
 *     1. A typed `ServiceConfig` shape that mirrors the CLI's --flags.
 *     2. A `scaffold()` function that shells out to the CLI safely, with
 *        structured error handling and a typed result.
 *     3. Typed accessors for inspecting what was generated, so callers can
 *        react to file shape without re-implementing filesystem walks.
 *
 *   This package is intentionally narrow. It does NOT re-implement the
 *   scaffolder in TypeScript — that would duplicate the Python logic and
 *   drift over time. The Python CLI stays the source of truth; the SDK
 *   is a typed affordance layer on top of it.
 */

import { spawn } from 'node:child_process';
import { readFile, readdir, stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';

// ---------- Types ----------

/** Templates supported by `servicectl init --template=...`. */
export type TemplateId =
  | 'node-express'
  | 'python-flask'
  | 'dotnet-webapi';

/** CI provider. */
export type CIProvider = 'github-actions' | 'azure-devops';

/** Deploy target. */
export type DeployTarget = 'local' | 'azure' | 'azure-container-apps';

/** Container registry. */
export type Registry = 'dockerhub' | 'ghcr' | 'ecr' | 'acr' | 'gcr';

/**
 * ServiceConfig is the typed mirror of `servicectl init` flags. Optional
 * fields correspond to optional CLI flags; defaults below match the CLI
 * defaults. Keep this in sync with src/servicectl/cli.py.
 */
export interface ServiceConfig {
  /** Required. The service name (lowercase-with-dashes, e.g. "billing-api"). */
  name: string;

  /** Required. Which scaffold template to use. */
  template: TemplateId;

  /** Default: 'github-actions'. */
  ci?: CIProvider;

  /** Default: 'local'. */
  deploy?: DeployTarget;

  /** Azure region. Only meaningful when deploy is azure or azure-container-apps. */
  azureRegion?: string;

  /** Coverage threshold 0-100. Default: 80. */
  coverage?: number;

  /** Container registry. Default: 'ghcr'. */
  registry?: Registry;

  /** Where to write the scaffold. Default: '.' (current dir). */
  outputDir?: string;

  /** If true, skip `git init` inside the new service dir. Default: false. */
  noGit?: boolean;

  /** If true, skip README generation. Default: false. */
  noReadme?: boolean;
}

/** Shape returned by `scaffold()`. */
export interface ScaffoldResult {
  /** Absolute path to the generated service root. */
  path: string;
  /** Files actually written, as relative paths under `path`. */
  files: string[];
  /** The resolved ServiceConfig (after applying defaults). */
  config: Required<ServiceConfig>;
  /** stdout from the underlying CLI invocation. */
  stdout: string;
  /** stderr from the underlying CLI invocation. */
  stderr: string;
}

/** Error thrown by `scaffold()` when the CLI fails. */
export class ScaffoldError extends Error {
  constructor(
    message: string,
    public readonly exitCode: number,
    public readonly stderr: string,
  ) {
    super(message);
    this.name = 'ScaffoldError';
  }
}

// ---------- Defaults ----------

const DEFAULTS = {
  ci: 'github-actions',
  deploy: 'local',
  azureRegion: 'eastus',
  coverage: 80,
  registry: 'ghcr',
  outputDir: '.',
  noGit: false,
  noReadme: false,
} as const satisfies Required<Omit<ServiceConfig, 'name' | 'template'>>;

/** Resolve a partial ServiceConfig against defaults. Exposed for testing. */
export function resolveConfig(config: ServiceConfig): Required<ServiceConfig> {
  if (!config.name || typeof config.name !== 'string') {
    throw new TypeError('ServiceConfig.name is required');
  }
  if (!config.template) {
    throw new TypeError('ServiceConfig.template is required');
  }
  // Validate name shape: lowercase, dashes, optional dots. Matches the
  // Python CLI's accepted pattern (which is forgiving on dotted names).
  // Accepts single-char names like "a" by not requiring both a leading
  // and trailing alphanumeric.
  if (!/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(config.name)) {
    throw new TypeError(
      `ServiceConfig.name must be lowercase-with-dashes (got ${JSON.stringify(config.name)})`,
    );
  }
  if (config.coverage !== undefined && (config.coverage < 0 || config.coverage > 100)) {
    throw new RangeError(`ServiceConfig.coverage must be 0-100 (got ${config.coverage})`);
  }
  return {
    name: config.name,
    template: config.template,
    ci: config.ci ?? DEFAULTS.ci,
    deploy: config.deploy ?? DEFAULTS.deploy,
    azureRegion: config.azureRegion ?? DEFAULTS.azureRegion,
    coverage: config.coverage ?? DEFAULTS.coverage,
    registry: config.registry ?? DEFAULTS.registry,
    outputDir: config.outputDir ?? DEFAULTS.outputDir,
    noGit: config.noGit ?? DEFAULTS.noGit,
    noReadme: config.noReadme ?? DEFAULTS.noReadme,
  };
}

// ---------- CLI invocation ----------

export interface ScaffoldOptions {
  /**
   * Override the path to the servicectl binary. Defaults to 'servicectl'
   * on PATH. Set this if you've installed under a different name or path.
   */
  binary?: string;
  /** Extra raw args appended to the CLI invocation. Use sparingly. */
  extraArgs?: string[];
  /** Timeout in milliseconds. Default: 120_000. */
  timeoutMs?: number;
  /** Override stdin/stdout pipes (for CI capture). Default: 'pipe'. */
  stdio?: 'pipe' | 'inherit';
}

/**
 * Scaffold a service by invoking `servicectl init` with the resolved flags.
 *
 * Throws ScaffoldError on non-zero exit. Returns a typed ScaffoldResult
 * including the list of files written.
 */
export async function scaffold(
  config: ServiceConfig,
  options: ScaffoldOptions = {},
): Promise<ScaffoldResult> {
  const resolved = resolveConfig(config);
  const binary = options.binary ?? 'servicectl';
  const args = buildArgs(resolved, options.extraArgs ?? []);

  const { stdout, stderr } = await runBinary(binary, args, {
    timeoutMs: options.timeoutMs ?? 120_000,
    stdio: options.stdio ?? 'pipe',
  });

  const serviceRoot = resolve(resolved.outputDir, resolved.name);
  const files = await listFiles(serviceRoot);

  return {
    path: serviceRoot,
    files,
    config: resolved,
    stdout,
    stderr,
  };
}

function buildArgs(c: Required<ServiceConfig>, extra: string[]): string[] {
  const args = [
    'init',
    c.name,
    `--template=${c.template}`,
    `--ci=${c.ci}`,
    `--deploy=${c.deploy}`,
    `--azure-region=${c.azureRegion}`,
    `--coverage=${c.coverage}`,
    `--registry=${c.registry}`,
    `--output-dir=${c.outputDir}`,
  ];
  if (c.noGit) args.push('--no-git');
  if (c.noReadme) args.push('--no-readme');
  return [...args, ...extra];
}

export { buildArgs };

function runBinary(
  binary: string,
  args: string[],
  opts: { timeoutMs: number; stdio: 'pipe' | 'inherit' },
): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolveFn, rejectFn) => {
    const child = spawn(binary, args, {
      stdio: [opts.stdio, opts.stdio, opts.stdio],
      shell: process.platform === 'win32',
    });

    let stdout = '';
    let stderr = '';
    if (child.stdout) {
      child.stdout.on('data', (chunk: Buffer) => {
        stdout += chunk.toString('utf8');
      });
    }
    if (child.stderr) {
      child.stderr.on('data', (chunk: Buffer) => {
        stderr += chunk.toString('utf8');
      });
    }

    const timer = setTimeout(() => {
      child.kill();
      rejectFn(
        new ScaffoldError(
          `servicectl init timed out after ${opts.timeoutMs}ms`,
          -1,
          stderr,
        ),
      );
    }, opts.timeoutMs);

    child.on('error', (err) => {
      clearTimeout(timer);
      rejectFn(
        new ScaffoldError(
          `failed to spawn ${binary}: ${err.message}`,
          -1,
          stderr,
        ),
      );
    });

    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        rejectFn(
          new ScaffoldError(
            `servicectl init exited with code ${code}`,
            code ?? -1,
            stderr,
          ),
        );
        return;
      }
      resolveFn({ stdout, stderr });
    });
  });
}

// ---------- Inspection helpers ----------

async function listFiles(root: string): Promise<string[]> {
  const out: string[] = [];
  async function walk(dir: string, prefix: string): Promise<void> {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.name === 'node_modules' || e.name === '.git') continue;
      const abs = join(dir, e.name);
      const rel = prefix ? join(prefix, e.name) : e.name;
      if (e.isDirectory()) {
        await walk(abs, rel);
      } else {
        out.push(rel);
      }
    }
  }
  await walk(root, '');
  out.sort();
  return out;
}

/**
 * Read and parse a JSON file from a scaffolded service (e.g. package.json,
 * appsettings.json, pyproject.toml's JSON variant). Returns null if the
 * file doesn't exist. Throws if the file exists but isn't valid JSON.
 */
export async function readScaffoldJSON<T = unknown>(
  serviceRoot: string,
  relativePath: string,
): Promise<T | null> {
  const full = join(serviceRoot, relativePath);
  try {
    await stat(full);
  } catch {
    return null;
  }
  const text = await readFile(full, 'utf8');
  return JSON.parse(text) as T;
}
