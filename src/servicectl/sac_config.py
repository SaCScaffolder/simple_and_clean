"""SAC config — read/write `.servicectl.json` for a scaffolded service.

This module is the bridge between the scaffolder (which knows the flags
that produced a service) and the refresh command (which needs those same
flags to regenerate the service against the current `simple_and_clean`
release). Without it, refresh would have to guess — which means the user
would have to re-pass `--template`, `--deploy`, `--registry`,
`--coverage`, `--db` every time they want to refresh. That's a footgun:
a typo in the flag would silently change the scaffold.

The config file is a flat JSON object committed alongside the initial
scaffold commit. Format:

    {
        "schema_version": 1,
        "service_name": "billing-api",
        "template": "python-flask",
        "ci_provider": "github-actions",
        "deploy_target": "azure",
        "azure_region": "centralus",
        "gcp_region": "us-central1",
        "gcp_project_id": "my-gcp-project",
        "coverage_threshold": 80,
        "registry": "ghcr",
        "db": "postgres",
        "sac_version": "0.1.0"
    }

The file is `.servicectl.json` (dotfile in the service root), distinct
from the SAC trailer schema on the commit message. The trailers mark
"this commit is a SAC-managed refresh" while the JSON marks "this
service was scaffolded with these flags."
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


CONFIG_FILENAME = ".servicectl.json"
SCHEMA_VERSION = 1


@dataclass
class SacConfig:
    """The set of scaffolder inputs that produced a service.

    `service_name` is the service's package name as the scaffolder
    wrote it (e.g. "billing-api"). It is stored so `servicectl refresh`
    can regenerate the scaffold with the same name, even when the
    repo's local directory name differs (the integration test clones
    the fixture repo to `${REPO_NAME}` and the GitHub repo name is
    often prefixed, e.g. `sac_example_ala_service_modified`, while the
    spec's service name is the unprefixed `ala_service_modified`).
    `from_generator` and `init` both write it; `refresh` reads it.
    An empty string means "not set" -- the caller is expected to
    fall back to the directory name in that case (graceful migration
    for repos scaffolded before this field existed).

    `sac_base_commit` is the legacy-migration anchor: when set, commits
    reachable from this SHA (i.e. at or older than it) are treated as
    SAC-managed by `classify_history()`, regardless of trailers. Use
    this for repos that were scaffolded before SAC trailers shipped;
    once a repo has been re-tagged or its commits naturally carry the
    trailer, you can drop the field.

    `from_dict()` / `to_dict()` are the JSON boundary; everything else
    in this module works with the dataclass.
    """

    schema_version: int = SCHEMA_VERSION
    service_name: str = ""
    template: str = "python-flask"
    ci_provider: str = "github-actions"
    deploy_target: str = "local"
    azure_region: str = "eastus"
    gcp_region: str = "us-central1"
    gcp_project_id: str | None = None
    coverage_threshold: int = 80
    registry: str = "ghcr"
    db: str = "postgres"
    sac_version: str = ""
    sac_base_commit: str | None = None  # legacy-migration anchor (optional)

    @classmethod
    def from_generator(
        cls,
        *,
        name: str,
        template: str,
        ci_provider: str,
        deploy_target: str,
        azure_region: str,
        gcp_region: str,
        gcp_project_id: str | None,
        coverage_threshold: int,
        registry: str,
        db: str,
        sac_version: str,
    ) -> "SacConfig":
        """Build a SacConfig from the same kwargs ServiceGenerator takes.

        `name` is the service's package name (e.g. "billing-api"). It is
        stored on the config so refresh can regenerate the scaffold with
        the same name, independent of the directory the repo happens to
        be cloned into. Pulled out as a factory so ServiceGenerator.run()
        can do a one-liner rather than spelling the field list twice.
        """
        return cls(
            service_name=name,
            template=template,
            ci_provider=ci_provider,
            deploy_target=deploy_target,
            azure_region=azure_region,
            gcp_region=gcp_region,
            gcp_project_id=gcp_project_id,
            coverage_threshold=coverage_threshold,
            registry=registry,
            db=db,
            sac_version=sac_version,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the dataclass as a JSON-ready dict. Schema version is included."""
        return asdict(self)

    def to_generator_kwargs(self) -> dict[str, object]:
        """Return the kwargs needed to construct a ServiceGenerator for refresh.

        Excludes bookkeeping (schema_version, sac_version, sac_base_commit)
        and includes everything else, so a refresh against an older
        scaffold can pick up exactly the same flags the user originally
        passed.

        `sac_base_commit` is excluded because it's metadata for the
        legacy-migration path, not a scaffolder input. The ServiceGenerator
        would reject it as an unknown kwarg.

        `service_name` is renamed to `name` here (the field the
        ServiceGenerator constructor expects). The caller is responsible
        for any further rename (e.g. falling back to the directory name
        if `service_name` is empty, which signals a config that was
        written before this field existed).

        Note: `output_dir` is not part of the config; the refresh CLI
        computes it from the `--repo` argument.
        """
        d = self.to_dict()
        d.pop("schema_version", None)
        d.pop("sac_version", None)
        d.pop("sac_base_commit", None)
        # Map service_name -> name for the ServiceGenerator kwarg.
        if "service_name" in d:
            d["name"] = d.pop("service_name")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "SacConfig":
        """Parse a JSON object into a SacConfig. Unknown keys are dropped silently."""
        known = {f for f in cls.__dataclass_fields__.keys()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    @classmethod
    def from_v2_dict(cls, d: dict[str, object]) -> "SacConfig":
        """Translate a v2 spec into a SacConfig.

        The v2 spec nests related fields under `service`, `ci`, and `deploy`
        objects (e.g. `service.name`, `deploy.target`, `deploy.gcp_region`).
        The v1 flat shape (the one `SacConfig` natively uses) puts each field
        at the top level (`name`, `deploy_target`, `gcp_region`). This
        classmethod projects v2 onto v1 so a v2 spec fed to `--from-config`
        produces the same `SacConfig` as the equivalent v1 spec.

        Overlays (`overlays.auth`, `overlays.observability`, `overlays.logs`)
        are silently dropped: the v1 CLI has no corresponding flag, and
        `SacConfig` does not store overlay state. Documented in
        `docs/spec-v2.md` as a one-way degradation -- v2 features with no
        v1 equivalent are skipped.

        Required v2 keys: `service.name` and `template` (per the v2 schema).
        Other v2 keys default to the same values `SacConfig` would have
        used if the v1 spec omitted them.

        Raises:
            KeyError: if `service.name` or `template` is missing. Surfaces
            to the CLI as a clear error so the user knows which spec field
            to fix.
        """
        if "service" not in d or "name" not in d.get("service", {}):  # type: ignore[operator]
            raise KeyError("v2 spec missing required 'service.name'")
        if "template" not in d:
            raise KeyError("v2 spec missing required 'template'")

        service = d.get("service", {})  # type: ignore[assignment]
        ci = d.get("ci", {})  # type: ignore[assignment]
        deploy = d.get("deploy", {})  # type: ignore[assignment]

        v1_flat: dict[str, object] = {
            "service_name": service.get("name"),  # type: ignore[attr-defined]
            "template": d.get("template"),
            "ci_provider": ci.get("provider", "github-actions"),
            "coverage_threshold": ci.get("coverage_threshold", 80),
            "deploy_target": deploy.get("target", "local"),
            "azure_region": deploy.get("azure_region", "eastus"),
            "gcp_region": deploy.get("gcp_region", "us-central1"),
            "gcp_project_id": deploy.get("gcp_project_id"),
            "registry": d.get("registry", "ghcr"),
            "db": d.get("database", "postgres"),
        }
        # `gcp_project_id` may be null in the v2 spec when the user wants
        # to defer setting it (the rendered Terraform uses a placeholder).
        # SacConfig accepts None for that field.
        return cls.from_dict(v1_flat)


def write_config(service_dir: Path, config: SacConfig) -> Path:
    """Write `.servicectl.json` into service_dir.

    Returns the path written. Callers typically commit the file
    alongside the scaffold. We don't try to add it to git here — the
    generator's git flow is responsible for staging.
    """
    target = service_dir / CONFIG_FILENAME
    target.write_text(
        json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def read_config(service_dir: Path) -> SacConfig:
    """Read `.servicectl.json` from service_dir. Raises:
        FileNotFoundError: if the file doesn't exist (not a SAC scaffold, or never initialized).
        json.JSONDecodeError: if the file is malformed.
        KeyError: if a required field is missing (handled by from_dict defaults).
    """
    path = service_dir / CONFIG_FILENAME
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        # Defensive: an array or scalar JSON would crash the kwargs expansion below.
        raise ValueError(
            f"{path} must contain a JSON object at the top level, got {type(data).__name__}"
        )
    return SacConfig.from_dict(data)


def resolve_sac_base_commit(config: SacConfig) -> str | None:
    """Return the legacy-migration SAC base commit SHA, if any.

    Precedence: the `SAC_BASE_COMMIT` env var wins over the config-file
    field. Returns None if neither is set. Whitespace is stripped.

    Why the env-var override exists: a one-off `SAC_BASE_COMMIT=<sha>
    servicectl refresh` should not require editing `.servicectl.json`.
    Persisted migrations can land in the config file; ad-hoc ones can
    use the env var.
    """
    import os

    env_val = os.environ.get("SAC_BASE_COMMIT", "").strip()
    if env_val:
        return env_val
    if config.sac_base_commit:
        return config.sac_base_commit.strip() or None
    return None


__all__ = [
    "SacConfig",
    "write_config",
    "read_config",
    "resolve_sac_base_commit",
    "CONFIG_FILENAME",
    "SCHEMA_VERSION",
]