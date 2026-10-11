"""Lint rendered workflow YAMLs (post-Jinja rendering).

The .j2 templates contain `{% if %}` / `{% raw %}` markers that PyYAML
can't parse directly. We render them against the same context the
scaffolder uses, then load the rendered output as YAML.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from servicectl.generator import ServiceGenerator  # noqa: E402

# Each entry: (template, registry, expected IMAGE_NAME substring in
# rendered ci.yml). Pin to a release (vX.Y.Z) so this script also
# catches bare-semver trivy-action pins that aren't resolvable.
CHECKS = [
    # (template, registry, deploy_target, gcp_project_id, expected_registry, expected_image_substring)
    ("go-webapi", "gar", "gcp-cloud-run", "my-cool-project",
     "us-central1-docker.pkg.dev", "IMAGE_NAME=us-central1-docker.pkg.dev/my-cool-project/${REPO}"),
    ("go-webapi", "ghcr", "local", None,
     "ghcr.io", "IMAGE_NAME=ghcr.io/${OWNER}/${REPO}"),
    ("dotnet-webapi", "gar", "gcp-cloud-run", "another-project",
     "us-central1-docker.pkg.dev", "IMAGE_NAME=us-central1-docker.pkg.dev/another-project/${REPO}"),
    ("node-express", "gar", "gcp-cloud-run", "node-gar-proj",
     "us-central1-docker.pkg.dev", "IMAGE_NAME=us-central1-docker.pkg.dev/node-gar-proj/${REPO}"),
    ("node-react-web", "gar", "gcp-cloud-run", "react-gar-proj",
     "us-central1-docker.pkg.dev", "IMAGE_NAME=us-central1-docker.pkg.dev/react-gar-proj/${REPO}"),
    ("python-flask", "gar", "gcp-cloud-run", "flask-gar-proj",
     "us-central1-docker.pkg.dev", "IMAGE_NAME=us-central1-docker.pkg.dev/flask-gar-proj/${REPO}"),
]


def main() -> int:
    failed = []
    for template, registry, deploy_target, gcp_project_id, expected_registry, expected_substring in CHECKS:
        with tempfile.TemporaryDirectory() as td:
            gen = ServiceGenerator(
                name=f"lint-{template}-{registry}",
                template=template,
                ci_provider="github-actions",
                deploy_target=deploy_target,
                azure_region="eastus",
                gcp_region="us-central1",
                gcp_project_id=gcp_project_id,
                coverage_threshold=80,
                registry=registry,
                db="postgres",
                output_dir=Path(td),
                with_git=False,
                with_readme=True,
            )
            out = gen.run()
            ci_yml_path = out / ".github" / "workflows" / "ci.yml"
            ci_yml = ci_yml_path.read_text(encoding="utf-8")

            # 1. YAML lint
            try:
                yaml.safe_load(ci_yml)
            except yaml.YAMLError as exc:
                failed.append((template, registry, f"YAML parse error: {exc}"))
                continue

            # 2. IMAGE_NAME shape
            if expected_substring not in ci_yml:
                failed.append((template, registry, f"missing {expected_substring!r}"))

            # 3. registry: field
            if f"registry: {expected_registry}" not in ci_yml:
                failed.append((template, registry, f"missing `registry: {expected_registry}`"))

            # 4. trivy pin must be vX.Y.Z (resolvable), not bare semver
            for line in ci_yml.splitlines():
                if "trivy-action@" in line:
                    pin = line.split("trivy-action@", 1)[1].split()[0].rstrip(":'\",")
                    if not pin.startswith("v"):
                        failed.append((template, registry, f"trivy-action pin `{pin}` is missing `v` prefix"))

            # 5. No literal `gar/` hostname anywhere
            if "IMAGE_NAME=gar/" in ci_yml:
                failed.append((template, registry, "ci.yml still contains literal `gar/` hostname"))

            print(f"  PASS {template:14s} registry={registry:8s}")

    if failed:
        print("\nFAILURES:")
        for template, registry, msg in failed:
            print(f"  [{template}/{registry}] {msg}")
        return 1
    print("\nAll rendered workflows lint clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())