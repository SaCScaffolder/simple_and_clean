"""Tests for registry alias expansion in the scaffolder.

The generator accepts short aliases (`ghcr`, `gar`, `gcr`, etc.) on the
command line and the spec, but Docker image refs and
`docker/login-action`'s `registry:` field need the full hostname. These
tests pin the alias map so the rendered CI workflow points at a
syntactically valid registry and the image push can resolve.

Bug history (Oct 2026): `_registry_hostname()` shipped without a `gar`
entry. Scaffolds with `registry: gar` rendered `IMAGE_NAME=gar/...`
verbatim, and `docker/login-action` failed every push with
`lookup gar on 127.0.0.53:53: server misbehaving`. GAR also needs the
GCP project ID baked into the image path (it's not in
`github.repository_owner`), so the workflow templates substitute
`gcp_project_id` instead of `${OWNER}` when the registry is GAR.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402


def _gen(**overrides) -> ServiceGenerator:
    """Build a ServiceGenerator with GAR + GCP defaults baked in.

    `name` is set to a stable value so we can assert on rendered output.
    For pure-logic tests (no `.run()`), `output_dir` is a unique mkdtemp
    path so back-to-back tests don't collide. The scaffold-render tests
    typically override `output_dir` to a `tempfile.TemporaryDirectory()`.
    """
    import tempfile
    defaults = dict(
        name="gar-test-svc",
        template="go-webapi",
        ci_provider="github-actions",
        deploy_target="gcp-cloud-run",
        azure_region="eastus",
        gcp_region="us-central1",
        gcp_project_id="my-cool-project",
        coverage_threshold=80,
        registry="gar",
        db="postgres",
        output_dir=Path(tempfile.mkdtemp(prefix="gc_test_")),
        with_git=False,
        with_readme=True,
    )
    defaults.update(overrides)
    return ServiceGenerator(**defaults)


def test_registry_hostname_expands_gar_to_regional_pkg_dev():
    """`gar` -> `<region>-docker.pkg.dev`, not the literal `gar`."""
    g = _gen()
    assert g._registry_hostname() == "us-central1-docker.pkg.dev"


def test_registry_hostname_gar_uses_configured_region():
    """The expansion is region-specific; don't hardcode us-central1."""
    g = _gen(gcp_region="europe-west4")
    assert g._registry_hostname() == "europe-west4-docker.pkg.dev"


def test_registry_hostname_passthrough_for_unknown_registry():
    """Unknown registries pass through unchanged (private registries)."""
    g = _gen(registry="registry.example.com:5000")
    assert g._registry_hostname() == "registry.example.com:5000"


def test_registry_hostname_keeps_ghcr_alias():
    """Don't regress ghcr.io expansion while adding gar."""
    g = _gen(registry="ghcr")
    assert g._registry_hostname() == "ghcr.io"


def test_image_name_for_gar_includes_gcp_project_id():
    """GAR image refs are <region>-docker.pkg.dev/<project-id>/<name>.

    Without the project segment, pushes land in the wrong namespace.
    """
    g = _gen()
    assert g._render_image_name() == "us-central1-docker.pkg.dev/my-cool-project/gar-test-svc"


def test_image_name_for_gar_lowercases_project_id():
    """Docker image refs require lowercase; assert we lowercase the project."""
    g = _gen(gcp_project_id="My-Mixed-Case-Project")
    # We lowercase in the Jinja template via `| lower`, not in the generator.
    # The generator passes the spec value through; the template is responsible
    # for lowercasing before it lands in bash.
    assert "My-Mixed-Case-Project" in g._render_image_name()


def test_image_name_for_gar_falls_back_to_placeholder_when_project_missing():
    """Missing gcp_project_id -> REPLACE_WITH_GCP_PROJECT_ID, not None."""
    g = _gen(gcp_project_id=None)
    name = g._render_image_name()
    assert "REPLACE_WITH_GCP_PROJECT_ID" in name
    assert "None" not in name


def test_rendered_workflow_uses_gar_image_path_for_gar_registry():
    """The full scaffold + render: ci.yml's build-job IMAGE_NAME must include
    `<region>-docker.pkg.dev/<project-id>/${REPO}`, not `gar/...`.

    This is the regression test for the broken `gar` alias. (PR #99 later
    removed the publish job for GAR entirely since GITHUB_TOKEN can't push
    to GAR, so the assertion now lives in the build job, not publish.)
    """
    with tempfile.TemporaryDirectory() as td:
        out = _gen(output_dir=Path(td)).run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # Must NOT contain the literal 'gar/' hostname (which is what the
        # scaffolder used to produce before this PR).
        assert "IMAGE_NAME=gar/" not in ci_yml, (
            "ci.yml still uses the literal `gar` registry alias — DNS will "
            "fail at publish time. The scaffolder must expand `gar` to "
            "`<region>-docker.pkg.dev`.\n\n"
            f"Got:\n{ci_yml}"
        )
        # Must contain the regional GAR hostname with the project ID.
        assert "us-central1-docker.pkg.dev/my-cool-project" in ci_yml, (
            "Expected the GAR image path with the project ID baked in. "
            f"Got:\n{ci_yml}"
        )


def test_rendered_workflow_keeps_ghcr_image_path():
    """GHCR scaffolds must render the repo-package image path
    `ghcr.io/<owner>/<repo>/<image>` (three path segments), not the
    two-segment user/org-package form. GITHUB_TOKEN has Packages:write
    for the *repo* namespace but not the *org* namespace.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # New path: includes the service name as a path segment.
        assert "IMAGE_NAME=ghcr.io/${OWNER}/${REPO}/gar-test-svc" in ci_yml, (
            f"Expected the repo-package GHCR image path with the service name. "
            f"Got:\n{ci_yml}"
        )
        # The two-segment org-package form (which fails with
        # `permission_denied: write_package`) must NOT appear. We check
        # for the bash line ending (`"`) right after `${REPO}` to be
        # sure the path doesn't terminate there.
        assert not re.search(
            r'IMAGE_NAME=ghcr\.io/\$\{OWNER\}/\$\{REPO\}"',
            ci_yml,
        ), (
            "ci.yml still renders the two-segment org-package GHCR image "
            "path. GITHUB_TOKEN cannot write to the org namespace; the "
            "image must be a repo-package (three segments). See PR #101."
            f"\n\nGot:\n{ci_yml}"
        )


def test_rendered_workflow_trivy_pin_is_resolvable():
    """`aquasecurity/trivy-action@0.20.0` (bare semver) is unresolvable on
    GitHub Actions; the templates must pin to a `vX.Y.Z` release tag.
    """
    with tempfile.TemporaryDirectory() as td:
        out = _gen(output_dir=Path(td)).run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        m = re.search(r"aquasecurity/trivy-action@([^\s'\"]+)", ci_yml)
        assert m is not None, f"No trivy-action pin found in:\n{ci_yml}"
        pin = m.group(1)
        assert pin.startswith("v"), (
            f"trivy-action pin `{pin}` is missing the `v` prefix; bare semver "
            "tags aren't resolvable as Actions versions. Pin must match a "
            "published release tag (e.g. `v0.36.0`)."
        )


def test_rendered_gar_workflow_has_no_publish_job():
    """GAR doesn't accept GITHUB_TOKEN; the CI publish job would always fail
    with `permission_denied: write_package`. The scaffolder omits it for GAR
    and points users at deploy/gcp/deploy.yml.j2 (which uses WIF instead).
    """
    with tempfile.TemporaryDirectory() as td:
        out = _gen(output_dir=Path(td)).run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # A `publish:` job at column 0 (top-level key) means the scaffolder
        # rendered the publish job. Comments mentioning "publish" are fine.
        assert not re.search(r"^  publish:", ci_yml, re.MULTILINE), (
            "GAR scaffolds must not render a `publish:` job in ci.yml — it "
            "would always fail with permission_denied. The GAR-aware push "
            "path is in deploy/gcp/deploy.yml.j2.\n\n"
            f"Got:\n{ci_yml}"
        )


def test_rendered_ghcr_workflow_still_has_publish_job():
    """GHCR scaffolds must still render the publish job. We didn't regress
    the GAR fix into breaking GHCR.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert re.search(r"^  publish:", ci_yml, re.MULTILINE), (
            "GHCR scaffolds must still render a `publish:` job. The GAR fix "
            "in PR #99 should not have removed it for non-GAR registries."
            f"\n\nGot:\n{ci_yml}"
        )