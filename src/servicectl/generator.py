"""Service generator — renders a template into a new service directory."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .templates import get_template_description, list_templates, template_path

# Files we never want to copy through (even if a template includes them).
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "__init__.py", "__pycache__"}


class ScaffoldError(RuntimeError):
    """Raised when scaffolding fails for a recoverable reason."""


@dataclass
class ServiceGenerator:
    name: str
    template: str
    ci_provider: str
    deploy_target: str
    azure_region: str
    coverage_threshold: int
    registry: str
    output_dir: Path
    with_git: bool
    with_readme: bool

    def _validate(self) -> None:
        if not self.name or self.name.strip() == "":
            raise ScaffoldError("service name cannot be empty")
        # Conservative name check: letters, digits, dash, underscore, dot.
        # Allows `my-service`, `my_service`, `my.service.v2`, etc.
        bad = set(" /\\?%*:|\"<>")
        if any(c in bad for c in self.name):
            raise ScaffoldError(f"service name contains invalid character(s): {sorted(bad & set(self.name))}")
        if self.template not in list_templates():
            raise ScaffoldError(f"unknown template: {self.template!r}")
        if self.deploy_target not in {"local", "azure"}:
            raise ScaffoldError(f"unknown deploy target: {self.deploy_target!r}")
        # Make sure we don't overwrite an existing directory.
        target = (self.output_dir / self.name).resolve()
        if target.exists():
            raise ScaffoldError(f"target directory already exists: {target}")

    def _render_context(self) -> dict[str, object]:
        return {
            "service_name": self.name,
            "service_name_snake": self.name.replace("-", "_").replace(".", "_"),
            "service_name_pascal": "".join(part.capitalize() for part in self.name.replace("_", "-").split("-")),
            "template": self.template,
            "template_description": get_template_description(self.template),
            "ci_provider": self.ci_provider,
            "deploy_target": self.deploy_target,
            "azure_region": self.azure_region,
            "coverage_threshold": self.coverage_threshold,
            "registry": self.registry,
            "image_name": f"{self.registry}/{self.name}",
        }

    def _render_file(self, rel: Path, src: Path, dest: Path, env: Environment, ctx: dict[str, object]) -> None:
        # Files ending in `.j2` are rendered and the suffix is stripped.
        # Filename components along the relative path may also contain Jinja
        # placeholders (e.g. `tests/{{ service_name_pascal }}.Tests/foo.j2`) which
        # get substituted when computing the destination path.
        # Substitute placeholders in each parent directory component of the
        # destination, then on the filename itself.
        dest_parts = []
        for part in rel.parent.parts:
            rendered_part = env.from_string(part).render(**ctx)
            dest_parts.append(rendered_part)
        new_dest = dest.joinpath(*dest_parts) if dest_parts else dest
        new_dest.mkdir(parents=True, exist_ok=True)

        if src.name.endswith(".j2"):
            tmpl_stem = src.name[: -len(".j2")]
            dest_name = env.from_string(tmpl_stem).render(**ctx)
            dest_path = new_dest / dest_name
            # Use a forward-slash relative path so Jinja2's FileSystemLoader
            # is happy on Windows.
            tmpl_rel = str(rel).replace("\\", "/")
            tmpl = env.get_template(tmpl_rel)
            rendered = tmpl.render(**ctx)
            dest_path.write_text(rendered, encoding="utf-8")
        else:
            # Static file, but its name might still contain Jinja placeholders.
            dest_name = env.from_string(src.name).render(**ctx)
            shutil.copy2(src, new_dest / dest_name)

    def run(self, console=None) -> Path:
        self._validate()

        target = (self.output_dir / self.name).resolve()
        target.mkdir(parents=True, exist_ok=False)

        tpl_root = template_path(self.template)
        if not tpl_root.is_dir():
            raise ScaffoldError(
                f"template {self.template!r} is missing its files at {tpl_root}. "
                "Did the package install correctly?"
            )

        env = Environment(
            loader=FileSystemLoader(str(tpl_root)),
            autoescape=False,  # We're rendering config/code, not HTML.
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        ctx = self._render_context()

        rendered_count = 0
        copied_count = 0
        for src in sorted(tpl_root.rglob("*")):
            if src.is_dir():
                continue
            if src.name in _IGNORED_NAMES:
                continue
            rel = src.relative_to(tpl_root)
            # `_render_file` builds the full destination path itself,
            # including any Jinja-substituted parent directory names.
            self._render_file(rel, src, target, env, ctx)
            if src.name.endswith(".j2"):
                rendered_count += 1
            else:
                copied_count += 1

        # Optionally overlay deploy-specific files. Today we only ship an
        # `infra/` overlay for the Azure targets. Keeping it separate lets us
        # add AWS/GCP overlays later without bloating the per-template trees.
        if self.deploy_target.startswith("azure"):
            overlay_root = resources.files("servicectl").joinpath("deploy", "azure")
            if overlay_root.is_dir():
                # Overlay uses a fresh Jinja environment rooted at the overlay
                # directory so it can find its own templates (separate path
                # from the per-template environment built above).
                overlay_env = Environment(
                    loader=FileSystemLoader(str(overlay_root)),
                    autoescape=False,
                    undefined=StrictUndefined,
                    keep_trailing_newline=True,
                )
                for src in sorted(overlay_root.rglob("*")):
                    if src.is_dir() or src.name in _IGNORED_NAMES:
                        continue
                    rel = src.relative_to(overlay_root)
                    self._render_file(rel, src, target, overlay_env, ctx)
                    if src.name.endswith(".j2"):
                        rendered_count += 1
                    else:
                        copied_count += 1

        if console is not None:
            console.print(f"  rendered [bold]{rendered_count}[/bold] templated files")
            console.print(f"  copied   [bold]{copied_count}[/bold] static files")

        if self.with_git:
            try:
                subprocess.run(
                    ["git", "init", "-q", "-b", "main", str(target)],
                    check=True,
                    capture_output=True,
                )
                if console is not None:
                    console.print("  ran      [bold]git init[/bold]")
            except FileNotFoundError:
                raise ScaffoldError("`git` was not found on PATH. Install git or re-run with --no-git.")
            except subprocess.CalledProcessError as e:
                raise ScaffoldError(f"`git init` failed: {e.stderr.decode(errors='replace')}")

        return target
