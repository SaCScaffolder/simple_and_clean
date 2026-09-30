"""Service modifier - re-renders specific files in an existing scaffolded service
to apply a new --db (or future) flag without losing the rest of the service.

For v1 this only supports modifying --db. Other fields (--ci, --registry,
--azure-region, --coverage) will be added in subsequent versions.

Re-rendering strategy: take the relevant .j2 template from the original
template tree, render it with the new context, and overwrite the existing
scaffolded file. The user sees a unified diff before writing and can back out.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .doctor import _infer_deploy_target
from .generator import SUPPORTED_DBS, ScaffoldError, template_path


class ModifierError(RuntimeError):
    """Raised when modification fails for a recoverable reason."""


# Sentinel strings in docker-compose.dev.yml that uniquely identify each flavor.
# Used to detect which --db was used at scaffold time without parsing the file
# structurally (fragile but cheap, and matches the templates exactly).
_DB_SENTINELS: dict[str, list[str]] = {
    "postgres": ["postgres:16-alpine"],
    "mysql": ["mysql:8.0"],
    "mssql": ["azure-sql-edge"],
    "cosmosdb": ["azure-cosmos-emulator"],
}


@dataclass
class ServiceModifier:
    """Modify an existing scaffolded service.

    For v1 only --db is supported. The path is the service directory;
    the new_db is the desired --db value.
    """

    path: Path
    new_db: str

    # Populated by run() / detect_current_db(); exposed for --show.
    current_db: str | None = field(default=None, init=False)
    deploy_target: str = field(default="unknown", init=False)
    template: str | None = field(default=None, init=False)

    def inspect(self) -> None:
        """Populate deploy_target, template, and current_db from disk.

        Safe to call before run(); does not require new_db to be set.
        Use this for --show flows where we want to read state without
        committing to a modification target.
        """
        self.deploy_target = _infer_deploy_target(self.path)
        self.template = self._detect_template()
        self.current_db = self._detect_current_db()

    def _validate(self) -> None:
        if not self.path.is_dir():
            raise ModifierError(f"service path does not exist: {self.path}")
        compose = self.path / "docker-compose.dev.yml"
        if not compose.exists():
            raise ModifierError(
                f"no docker-compose.dev.yml at {self.path}. "
                "Was this service scaffolded by servicectl?"
            )
        if self.new_db not in SUPPORTED_DBS:
            raise ModifierError(
                f"unsupported --db: {self.new_db!r} (allowed: {sorted(SUPPORTED_DBS)})"
            )

    def _detect_current_db(self) -> str:
        """Best-effort: figure out which --db was used at scaffold time.

        Looks for sentinel strings in docker-compose.dev.yml. If none match,
        returns 'unknown' and the caller treats the service as unscaffoldable.
        """
        compose = (self.path / "docker-compose.dev.yml").read_text(encoding="utf-8")
        for db, sentinels in _DB_SENTINELS.items():
            if any(s in compose for s in sentinels):
                return db
        return "unknown"

    def _detect_template(self) -> str:
        """Best-effort: figure out which template was used at scaffold time.

        Maps docker-compose layout to a registered template name. We can't
        perfectly distinguish all five templates from docker-compose alone
        (since they all share the same env-var patterns), so we use sentinel
        files specific to each template as tiebreakers.
        """
        # The compose file is the same shape across templates for db, so we
        # need to look at template-specific files to disambiguate.
        if (self.path / "pyproject.toml").exists():
            return "python-flask"
        if (self.path / "go.mod").exists():
            return "go-webapi"
        if (self.path / "package.json").exists() and (self.path / "src" / "App.tsx").exists():
            return "node-react-web"
        if (self.path / "package.json").exists():
            return "node-express"
        if (self.path / "Program.cs").exists() or (self.path / "{{ service_name_pascal }}.csproj").exists():
            return "dotnet-webapi"
        return "unknown"

    def _render_context(self) -> dict[str, object]:
        """Build the Jinja context for re-rendering.

        Reads what we can from disk (template name, deploy target, current db)
        and merges in the new db. Other fields use sensible defaults since
        we don't yet support modifying them.
        """
        # Best-effort service name: the directory name. For nested paths,
        # this may differ from the original scaffolded name, but the templates
        # use service_name only for metadata (not for code paths), so the
        # rendered output is still valid.
        service_name = self.path.resolve().name
        service_name_snake = service_name.replace("-", "_").replace(".", "_")
        service_name_pascal = "".join(
            part.capitalize() for part in service_name.replace("_", "-").split("-")
        )
        db_meta = SUPPORTED_DBS[self.new_db]
        return {
            "service_name": service_name,
            "service_name_snake": service_name_snake,
            "service_name_pascal": service_name_pascal,
            "template": self.template or "unknown",
            "template_description": "",
            "ci_provider": "github-actions",
            "deploy_target": self.deploy_target,
            "azure_region": "eastus",
            "coverage_threshold": 80,
            "registry": "ghcr",
            "db": self.new_db,
            "db_azure_resource_type": db_meta["azure_resource_type"],
            "db_docker_image": db_meta["docker_image"],
            "db_env_prefix": db_meta["env_prefix"],
            "db_healthcheck_cmd": db_meta["healthcheck_cmd"],
            "image_name": f"ghcr/{service_name}",
        }

    def _files_to_rerender(self) -> list[Path]:
        """Paths (relative to self.path) of files that need re-rendering.

        Always includes docker-compose.dev.yml. Adds Azure overlay files if
        the service was scaffolded with --deploy=azure.
        """
        files: list[Path] = [Path("docker-compose.dev.yml")]
        if self.deploy_target == "azure":
            files.append(Path("infra/main.bicep"))
            for env in ("dev", "staging", "prod"):
                files.append(Path(f"infra/{env}.bicepparam"))
        return files

    def _render_one(self, rel: Path, ctx: dict[str, object]) -> str:
        """Render the template at templates/<tpl>/<rel> with the given context.

        For docker-compose.dev.yml, we use the per-template template tree.
        For infra/*.bicep*, we use the Azure overlay's `infra/` subdir
        (which is where main.bicep.j2 and the bicepparam templates live).
        """
        if rel.parts[0] == "infra":
            # Azure overlay tree: Bicep + bicepparam templates live in
            # deploy/azure/infra/, not at the root of deploy/azure/.
            tpl_root = Path(__file__).parent / "deploy" / "azure" / "infra"
            template_rel = "/".join(rel.parts[1:]) + ".j2"
        else:
            tpl_root = template_path(self.template) if self.template else None
            if tpl_root is None or not tpl_root.is_dir():
                raise ModifierError(
                    f"could not locate template tree for {self.template!r}; "
                    "cannot re-render"
                )
            template_rel = str(rel) + ".j2"
        env = Environment(
            loader=FileSystemLoader(str(tpl_root)),
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        tmpl = env.get_template(template_rel)
        return tmpl.render(**ctx)

    def diff(self) -> list[tuple[Path, str]]:
        """Return a list of (relative_path, unified_diff_text) for each file
        that would change. Empty list means no changes (e.g., current db == new db).
        """
        ctx = self._render_context()
        diffs: list[tuple[Path, str]] = []
        for rel in self._files_to_rerender():
            target = self.path / rel
            if not target.exists():
                continue
            old_text = target.read_text(encoding="utf-8")
            try:
                new_text = self._render_one(rel, ctx)
            except ModifierError:
                raise
            except Exception as e:
                raise ModifierError(f"failed to render {rel}: {e}") from e
            if old_text == new_text:
                continue
            old_lines = old_text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
            diff_text = "".join(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            diffs.append((rel, diff_text))
        return diffs

    def run(self, dry_run: bool = False) -> list[Path]:
        """Run the modification.

        Returns the list of files that were written (empty if dry_run or
        no changes needed).
        """
        self._validate()
        self.deploy_target = _infer_deploy_target(self.path)
        self.template = self._detect_template()
        self.current_db = self._detect_current_db()
        if self.current_db == "unknown":
            raise ModifierError(
                "could not detect current --db from docker-compose.dev.yml. "
                "Was this service scaffolded by servicectl?"
            )
        if self.current_db == self.new_db:
            return []

        diffs = self.diff()
        if not diffs:
            return []

        if dry_run:
            return []  # caller will print diffs separately

        written: list[Path] = []
        ctx = self._render_context()
        for rel in self._files_to_rerender():
            target = self.path / rel
            if not target.exists():
                continue
            new_text = self._render_one(rel, ctx)
            if target.read_text(encoding="utf-8") == new_text:
                continue
            target.write_text(new_text, encoding="utf-8")
            written.append(rel)
        return written
