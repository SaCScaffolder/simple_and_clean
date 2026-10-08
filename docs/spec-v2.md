# Service Specification v2

This document describes the v2 service specification accepted by
`servicectl init --from-config=<spec.json>` and (after issue #50 ships)
validated by `servicectl validate --spec=<spec.json>`.

The spec is the source of truth for a service. Once a v2 spec exists for a
service, the same file can drive `servicectl init`, the integration-test
harness, and `servicectl refresh` — no flag re-entry.

The JSON Schema for this spec lives at
[`specs/spec.schema.v2.json`](../specs/spec.schema.v2.json). Anything
described here is also encoded there; if they disagree, the schema wins.

---

## Shape at a glance

```json
{
  "schema_version": 2,
  "service":  { "name": "billing-api", "description": "..." },
  "template": "go-webapi",
  "ci":       { "provider": "github-actions", "coverage_threshold": 85 },
  "deploy":   { "target": "gcp-cloud-run", "gcp_region": "us-central1", "gcp_project_id": "..." },
  "registry": "gar",
  "database": "postgres",
  "overlays": { "auth": "oidc", "observability": "opentelemetry", "logs": "console" }
}
```

Only `schema_version`, `service.name`, and `template` are required.
Everything else has a default that you can override.

---

## Field reference

### `schema_version` (required, integer)

The spec schema version. Must be `2` for this schema. Bump to a new
top-level number when the shape changes incompatibly. See [Version
strategy](#version-strategy) for the rules.

### `service` (required, object)

Identity and naming for the scaffolded service. Kept separate from
runtime concerns so the same spec can be re-targeted at a different
name (e.g. for staging clones) without re-deriving deploy/ci choices.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Output dir, image name, cloud resource. Pattern: `^[A-Za-z0-9._-]+$` |
| `description` | string | no | One-liner used in README + success panel. Max 200 chars. |

### `template` (required, string)

Runtime template. Determines language, framework, and the test/precommit
toolchain that gets installed.

Allowed values: `node-express`, `node-react-web`, `python-flask`,
`dotnet-webapi`, `go-webapi`.

The set of allowed values mirrors the `TEMPLATES` dict in
`src/servicectl/templates.py`. Adding a new template requires both
adding it there and updating this list (the schema validator in #50
will import the schema at validation time and catch the drift).

### `ci` (optional, object)

| Field | Type | Default | Notes |
|---|---|---|---|
| `provider` | string | `github-actions` | `github-actions` or `azure-devops`. |
| `coverage_threshold` | int 0–100 | `80` | Used by the pre-commit hook and CI. They parse the same coverage output and fail at the same threshold — this is the contract. |

### `deploy` (optional, object)

| Field | Type | Default | Notes |
|---|---|---|---|
| `target` | string | `local` | `local`, `azure`, `gcp-cloud-run`, `gcp-gke-autopilot`. |
| `azure_region` | string | `eastus` | Used when `target=azure`. |
| `gcp_region` | string | `us-central1` | Used when `target=gcp-*`. |
| `gcp_project_id` | string \| null | `null` | Optional at scaffold; Terraform will refuse to apply until the placeholder is replaced. |

### `registry` (optional, string)

Container registry. Allowed: `dockerhub`, `ghcr`, `ecr`, `acr`, `gcr`,
`gar`. Short aliases (`ghcr` → `ghcr.io`, `dockerhub` → `docker.io`,
`gar` → `*-docker.pkg.dev`) are expanded inside the image name.

### `database` (optional, string)

Database engine. Allowed: `postgres`, `mysql`, `mssql`, `cosmosdb`.
The deploy overlay picks up the cloud-specific resource type; the
local Compose overlay uses the Docker image metadata keyed off this
value.

### `overlays` (optional, object)

Composability layer. Each entry adds a feature on top of the bare
`template + deploy + database`. The default for each is the no-op.

| Field | Allowed values | Default | Notes |
|---|---|---|---|
| `auth` | `none`, `api-key`, `oauth`, `oidc`, `entra`, `google`, `certificate` | `none` | Renders middleware + env wiring + a starter login route. |
| `observability` | `none`, `opentelemetry` | `none` | Adds OTel SDK + exporter wiring. |
| `logs` | `console`, `elk`, `kusto` | `console` | Where logs ship to. Requires `observability=opentelemetry` to actually emit them. |

Overlays are designed to be added, removed, and reordered without
invalidating the rest of the spec. Future overlays (#66 Postman, #67
Insomnia, #69 ELK, #70 Logstash, #71 Kibana, #72 Kusto, #73 Prometheus)
will land as additional keys here.

---

## Examples

Three example specs live under [`specs/examples/`](../specs/examples/):

| File | What it exercises |
|---|---|
| `go-webapi-gcp-cloud-run.json` | Go template, GCP Cloud Run deploy, GAR registry, OIDC + OTel overlays. |
| `python-flask-azure-entra.json` | Python template, Azure deploy, ACR registry, MSSQL database, Entra auth + ELK logs. |
| `node-express-local.json` | Minimum viable spec — only the three required fields. |

All three validate against the schema today.

---

## v1 → v2 migration

The v1 service config (`.servicectl.json` written into a scaffolded
service, see `src/servicectl/sac_config.py`) is **not** a v2 spec. The
v1 file lives inside the scaffolded service and is the scaffolder's
input record; the v2 spec is the *user-facing* declaration that drives
scaffolding.

Mapping from v1 `.servicectl.json` → v2 spec:

| v1 key | v2 location |
|---|---|
| `service_name` | `service.name` |
| `template` | `template` |
| `ci_provider` | `ci.provider` |
| `deploy_target` | `deploy.target` |
| `azure_region` | `deploy.azure_region` |
| `gcp_region` | `deploy.gcp_region` |
| `gcp_project_id` | `deploy.gcp_project_id` |
| `coverage_threshold` | `ci.coverage_threshold` |
| `registry` | `registry` |
| `db` | `database` |
| `schema_version` (was `1`) | `schema_version` (now `2`) |
| `sac_version` | dropped (v2 doesn't track SAC scaffolder version; the commit trailers do) |
| `sac_base_commit` | dropped (v2 specs are not migration-anchored) |

`servicectl refresh` against a service that has only a v1
`.servicectl.json` keeps working unchanged — the v1 file is still the
refresh source-of-truth for older scaffolds. New services may
additionally commit a v2 spec at the repo root if they want
declarative `init`-time re-runs.

A planned follow-up issue (not yet filed) will introduce
`servicectl migrate-config` that reads a v1 `.servicectl.json` and
emits a v2 spec to stdout. Until then, the table above is the
authoritative translation.

---

## Version strategy

**Spec schema versions are integers, scoped to the spec itself.** They
are not the same as the `sac_version` of the scaffolder that emitted
a service, and they are not the same as the SAC trailer
`SAC-Spec-Version`.

Rules:

1. **Major** = breaking change. Bump the integer. The validator
   (`servicectl validate`, issue #50) refuses to validate a spec whose
   `schema_version` is greater than the highest version it knows
   about, and refuses to validate a spec whose `schema_version` is
   less than its declared minimum supported version.
2. **Minor** = add a new optional field or a new enum value with
   sensible default. The spec stays the same integer; the schema is
   updated in-place. This is *not* a breaking change because old
   consumers ignore unknown fields (`additionalProperties: false`
   only applies to the spec's *contents* — the schema document is
   allowed to gain new properties on `service`, `ci`, etc., as long
   as the JSON object's existing keys still validate).
3. **Field rename** is a major bump. We don't keep alias shims — the
   v1→v2 migration is the only one, and the table above is the model
   for how renames are documented.

The current spec is **v2**, first shipped 2026-10-08. It supersedes
the implicit v1 spec (the flat `--from-config` JSON shape accepted by
`servicectl init` before this issue), which is still supported
indefinitely for backward compatibility.
