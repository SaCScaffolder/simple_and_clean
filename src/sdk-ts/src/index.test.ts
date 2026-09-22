import { describe, expect, it } from 'vitest';
import {
  buildArgs,
  readScaffoldJSON,
  resolveConfig,
  ScaffoldError,
  type ServiceConfig,
} from './index.js';

describe('resolveConfig', () => {
  it('requires name', () => {
    expect(() =>
      // @ts-expect-error -- testing runtime guard
      resolveConfig({ template: 'node-express' }),
    ).toThrow(TypeError);
  });

  it('requires template', () => {
    expect(() =>
      // @ts-expect-error -- testing runtime guard
      resolveConfig({ name: 'billing-api' }),
    ).toThrow(TypeError);
  });

  it('rejects names with uppercase or underscores', () => {
    expect(() => resolveConfig({ name: 'BillingAPI', template: 'node-express' })).toThrow(TypeError);
    expect(() => resolveConfig({ name: 'billing_api', template: 'node-express' })).toThrow(TypeError);
    expect(() => resolveConfig({ name: 'Billing-API', template: 'node-express' })).toThrow(TypeError);
  });

  it('accepts dotted and dashed names', () => {
    expect(() => resolveConfig({ name: 'billing-api', template: 'node-express' })).not.toThrow();
    expect(() => resolveConfig({ name: 'svc.v1', template: 'python-flask' })).not.toThrow();
  });

  it('rejects out-of-range coverage', () => {
    expect(() =>
      resolveConfig({ name: 'a', template: 'node-express', coverage: -1 }),
    ).toThrow(RangeError);
    expect(() =>
      resolveConfig({ name: 'a', template: 'node-express', coverage: 101 }),
    ).toThrow(RangeError);
  });

  it('applies defaults', () => {
    const r = resolveConfig({ name: 'a', template: 'node-express' });
    expect(r.ci).toBe('github-actions');
    expect(r.deploy).toBe('local');
    expect(r.azureRegion).toBe('eastus');
    expect(r.coverage).toBe(80);
    expect(r.registry).toBe('ghcr');
    expect(r.outputDir).toBe('.');
    expect(r.noGit).toBe(false);
    expect(r.noReadme).toBe(false);
  });

  it('preserves overrides', () => {
    const r = resolveConfig({
      name: 'b',
      template: 'dotnet-webapi',
      ci: 'azure-devops',
      deploy: 'azure',
      azureRegion: 'westus2',
      coverage: 90,
      registry: 'acr',
      noGit: true,
    });
    expect(r.ci).toBe('azure-devops');
    expect(r.deploy).toBe('azure');
    expect(r.azureRegion).toBe('westus2');
    expect(r.coverage).toBe(90);
    expect(r.registry).toBe('acr');
    expect(r.noGit).toBe(true);
  });
});

describe('buildArgs', () => {
  it('produces the canonical flag set', () => {
    const cfg: Required<ServiceConfig> = {
      name: 'billing-api',
      template: 'node-express',
      ci: 'github-actions',
      deploy: 'local',
      azureRegion: 'eastus',
      coverage: 85,
      registry: 'ghcr',
      outputDir: '.',
      noGit: false,
      noReadme: false,
    };
    expect(buildArgs(cfg, [])).toEqual([
      'init',
      'billing-api',
      '--template=node-express',
      '--ci=github-actions',
      '--deploy=local',
      '--azure-region=eastus',
      '--coverage=85',
      '--registry=ghcr',
      '--output-dir=.',
    ]);
  });

  it('appends --no-git and --no-readme when set', () => {
    const cfg: Required<ServiceConfig> = {
      name: 'x',
      template: 'python-flask',
      ci: 'github-actions',
      deploy: 'local',
      azureRegion: 'eastus',
      coverage: 80,
      registry: 'ghcr',
      outputDir: '.',
      noGit: true,
      noReadme: true,
    };
    expect(buildArgs(cfg, [])).toContain('--no-git');
    expect(buildArgs(cfg, [])).toContain('--no-readme');
  });

  it('appends extra args after the standard set', () => {
    const cfg: Required<ServiceConfig> = {
      name: 'x',
      template: 'python-flask',
      ci: 'github-actions',
      deploy: 'local',
      azureRegion: 'eastus',
      coverage: 80,
      registry: 'ghcr',
      outputDir: '.',
      noGit: false,
      noReadme: false,
    };
    expect(buildArgs(cfg, ['--verbose'])).toEqual([
      'init',
      'x',
      '--template=python-flask',
      '--ci=github-actions',
      '--deploy=local',
      '--azure-region=eastus',
      '--coverage=80',
      '--registry=ghcr',
      '--output-dir=.',
      '--verbose',
    ]);
  });
});

describe('readScaffoldJSON', () => {
  it('returns null when file does not exist', async () => {
    const result = await readScaffoldJSON(
      'C:\\Users\\henry\\source\\repos\\sac\\simple_and_clean\\tests',
      'definitely-does-not-exist.json',
    );
    expect(result).toBeNull();
  });
});

describe('ScaffoldError', () => {
  it('carries exitCode and stderr', () => {
    const err = new ScaffoldError('boom', 2, 'something went wrong');
    expect(err.name).toBe('ScaffoldError');
    expect(err.exitCode).toBe(2);
    expect(err.stderr).toBe('something went wrong');
    expect(err.message).toBe('boom');
    expect(err instanceof Error).toBe(true);
  });
});
