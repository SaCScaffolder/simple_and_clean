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

    Mirrors the ServiceGenerator constructor: name is implicit (it's the
    service's directory name, written separately by the user as a rename)
    so we don't store it here. The CLI is responsible for resolving the
    name from `--repo`.

    `from_dict()` / `to_dict()` are the JSON boundary; everything else
    in this module works with the dataclass.
    """

    schema_version: int = SCHEMA_VERSION
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

    @classmethod
    def from_generator(
        cls,
        *,
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

        Pulled out as a factory so ServiceGenerator.run() can do a one-liner
        rather than spelling the field list twice.
        """
        return cls(
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

        Excludes bookkeeping (schema_version, sac_version) and includes
        everything else, so a refresh against an older scaffold can pick
        up exactly the same flags the user originally passed.

        Note: `name` and `output_dir` are not part of the config; the
        refresh CLI computes those from the `--repo` argument.
        """
        d = self.to_dict()
        d.pop("schema_version", None)
        d.pop("sac_version", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "SacConfig":
        """Parse a JSON object into a SacConfig. Unknown keys are dropped silently."""
        known = {f for f in cls.__dataclass_fields__.keys()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


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


__all__ = [
    "SacConfig",
    "write_config",
    "read_config",
    "CONFIG_FILENAME",
    "SCHEMA_VERSION",
]