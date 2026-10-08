# `specs/` — Service specifications

This directory holds the **Service Specification v2 JSON Schema** and
worked-example specs that exercise it.

## Files

| File | Purpose |
|---|---|
| `spec.schema.v2.json` | The v2 schema. Draft 2020-12. Load this to validate any spec. |
| `examples/go-webapi-gcp-cloud-run.json` | Go template, GCP Cloud Run, GAR, OIDC + OTel. |
| `examples/python-flask-azure-entra.json` | Python template, Azure App Service, ACR, MSSQL, Entra + ELK. |
| `examples/node-express-local.json` | Minimum viable spec (only the 3 required fields). |

## Usage

`servicectl init --from-config=specs/examples/go-webapi-gcp-cloud-run.json`
loads the spec; once `#50` (Spec Validation) ships, `servicectl validate
--spec=<file>` will report all schema violations with absolute paths.

For ad-hoc validation:

```bash
pip install jsonschema
python -c "import json, jsonschema; \
    schema = json.load(open('specs/spec.schema.v2.json')); \
    spec = json.load(open('<your-spec>.json')); \
    jsonschema.Draft202012Validator(schema).validate(spec)"
```

## Versioning

The spec schema uses integer versions. v2 is the current version; v1
(an implicit shape — the flat `--from-config` JSON accepted by
`servicectl init` before this directory existed) is supported
indefinitely for backward compatibility but is not formally described
here. See [`docs/spec-v2.md`](../docs/spec-v2.md) for the v1→v2
migration table and the rules for bumping the spec schema version.

## Adding an example

Drop a new `*.json` file under `examples/`. It should validate against
the current schema (run the one-liner above). Prefer examples that
exercise a combination not already covered: a new template, a new
deploy target, a new overlay, or a new database. Update
`docs/spec-v2.md`'s examples table when you add one.
