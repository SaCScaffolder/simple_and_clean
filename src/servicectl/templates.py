"""Template registry.

Templates are bundled as directories under `servicectl/templates/<id>/`.
Each template folder contains the static + templated files that get rendered
into the new service directory.

To add a new template:
  1. Create `servicectl/templates/<id>/` with the files you want generated.
  2. Add `<id>` to TEMPLATES below (and a short description).
  3. Add it to the CLI's --template Choice list.
"""

from __future__ import annotations

from importlib import resources

# Registry: id -> short description (used in --help).
TEMPLATES: dict[str, str] = {
    "node-express": "Node.js + Express + PostgreSQL",
    "node-react-web": "Node 20 + Vite + React 18 + TypeScript + Tailwind + shadcn/ui (SPA)",
    "python-flask": "Python + Flask + PostgreSQL",
    "dotnet-webapi": ".NET / C# Web API + PostgreSQL",
    "go-webapi": "Go 1.22+ + net/http + PostgreSQL (distroless runtime)",
}


def list_templates() -> list[str]:
    """Return the list of registered template ids."""
    return list(TEMPLATES.keys())


def get_template_description(template: str) -> str:
    return TEMPLATES.get(template, "")


def template_path(template: str):
    """Return a Traversable pointing at the bundled template directory.

    Reads from the source repo location (relative to this module) so that
    editable installs (`pip install -e .`) see template changes immediately.
    Falls back to importlib.resources for non-editable installs.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent / "templates" / template
    if src.exists():
        return src
    return resources.files("servicectl").joinpath("templates", template)
