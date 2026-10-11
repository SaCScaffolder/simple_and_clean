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

import pytest  # noqa: E402  (used by the publish-tagged tests below)

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


def test_rendered_dockerfile_has_oci_labels():
    """Every scaffolded Dockerfile must have org.opencontainers.image.source
    LABEL. Without that label, GitHub Packages can't auto-link a freshly-
    pushed package to the current repo, and subsequent pushes from a
    rescaffolded repo fail with `permission_denied: write_package`.

    The label value is `ARG IMAGE_SOURCE` (templated at build time via
    --build-arg in ci.yml) so the scaffolder doesn't need to know the
    GitHub repo name at scaffold time.

    See PR #103 and the trios-railway/scarab writeup that surfaced this:
    https://github.com/gHashTag/trios-railway/pull/224
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        df = (out / "Dockerfile").read_text(encoding="utf-8")
        assert "ARG IMAGE_SOURCE" in df, (
            "Dockerfile missing `ARG IMAGE_SOURCE`. The OCI image.source "
            "label is what tells GitHub Packages which repo a freshly-"
            "pushed package belongs to. See PR #103."
            f"\n\nGot:\n{df}"
        )
        assert "org.opencontainers.image.source" in df, (
            "Dockerfile missing `org.opencontainers.image.source` LABEL. "
            "This is the critical one for GHCR auto-linking. See PR #103."
            f"\n\nGot:\n{df}"
        )
        # The LABEL must reference ${IMAGE_SOURCE} so the build-arg is
        # actually used (not just defined).
        assert re.search(
            r"org\.opencontainers\.image\.source=\$\{IMAGE_SOURCE\}",
            df,
        ), (
            "Dockerfile's image.source LABEL doesn't reference the "
            "IMAGE_SOURCE build-arg. The label will be empty, breaking "
            "GHCR auto-linking. See PR #103."
            f"\n\nGot:\n{df}"
        )


def test_rendered_ghcr_ci_passes_image_source_build_arg():
    """The CI workflow's docker build / build-push-action call must pass
    IMAGE_SOURCE=https://github.com/${{ github.repository }} so the
    Dockerfile's `ARG IMAGE_SOURCE` gets a real value at build time.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # Either the build step (docker build) or the publish step
        # (docker build-push-action) must pass the arg. Most templates
        # use the former; go-webapi uses both.
        build_arg = re.search(
            r"--build-arg\s+IMAGE_SOURCE=\"https://github.com/\$\{\{\s*github\.repository\s*\}\}\"",
            ci_yml,
        )
        assert build_arg, (
            "ci.yml doesn't pass `--build-arg IMAGE_SOURCE=...` to "
            "docker build. The Dockerfile's `ARG IMAGE_SOURCE` will be "
            "empty, and GHCR's package auto-link won't fire. See PR #103."
            f"\n\nGot:\n{ci_yml}"
        )


def test_rendered_ghcr_ci_has_preflight_package_check():
    """The GHCR publish job must pre-flight check for a stale mislinked
    package before attempting `docker push`. Without this, the user gets
    a half-finished pipeline and a cryptic `permission_denied:
    write_package` deep inside the push step. With it, they get a clear
    error at the pre-flight step with a copy-pasteable fix.

    The check is gated on IMAGE_NAME starting with `ghcr.io/` so GAR
    scaffolds don't run it (GAR doesn't have this namespace issue).
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # The pre-flight step must exist with a recognizable name.
        assert "Pre-flight: check for stale mislinked GHCR package" in ci_yml, (
            "ci.yml is missing the pre-flight package check. See PR #103."
            f"\n\nGot:\n{ci_yml}"
        )
        # It must query the org container-package endpoint.
        assert "/packages/container/" in ci_yml, (
            "Pre-flight step doesn't query /packages/container/ -- it "
            "should be checking for the existing mislinked package. See PR #103."
            f"\n\nGot:\n{ci_yml}"
        )
        # The failure path must tell the user how to delete the stale package.
        assert "delete:packages" in ci_yml, (
            "Pre-flight step's error message must mention the delete:packages "
            "scope so the user knows what token they need. See PR #103."
            f"\n\nGot:\n{ci_yml}"
        )


def test_rendered_gar_ci_has_no_preflight_package_check():
    """GAR scaffolds must NOT run the pre-flight check -- GAR doesn't have
    the user/org-vs-repo-package namespace issue (GAR's permissions model
    is different: GAR namespaces are bucket-scoped, not org-scoped). The
    `if:` on the pre-flight step must gate it on ghcr.io so GAR skips it.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="gar", deploy_target="gcp-cloud-run", gcp_project_id="my-proj")
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        if "Pre-flight" in ci_yml:
            # If the step is present at all, it MUST be gated on ghcr.io.
            # Find the step and check the next non-empty line for `if:`.
            lines = ci_yml.split("\n")
            for i, line in enumerate(lines):
                if "Pre-flight" in line and "name:" in line:
                    # Look ahead for `if:`
                    for j in range(i, min(i + 8, len(lines))):
                        if lines[j].lstrip().startswith("if:"):
                            assert "ghcr.io" in lines[j], (
                                "GAR scaffold's pre-flight step is not gated "
                                "on ghcr.io -- it would run unnecessarily "
                                "for GAR. See PR #103."
                                f"\n\nGot:\n{ci_yml}"
                            )
                            break
                    else:
                        pytest.fail(
                            "Pre-flight step has no `if:` gating it. It will "
                            "run for non-GHCR registries. See PR #103."
                            f"\n\nGot:\n{ci_yml}"
                        )

# ---------------------------------------------------------------------------
# PR #104: tagged-release publishing (`publish-tagged` job).
# ---------------------------------------------------------------------------


def test_rendered_ghcr_ci_has_publish_tagged_job():
    """Every GHCR scaffolded ci.yml must have a `publish-tagged` job that
    only runs on v* tag pushes. This is the new PR #104 feature: tag a
    commit with v1.2.3 and the image gets pushed with the
    :X.Y.Z / :X.Y / :X / :<sha> / :latest tag set.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "publish-tagged:" in ci_yml, (
            "ci.yml is missing the `publish-tagged` job. Tag pushes "
            "(v1.2.3, v1.2.3-rc.1) need a dedicated job to derive "
            "semver labels and push the image with multi-tag labels. "
            "See PR #104."
            f"\n\nGot:\n{ci_yml}"
        )


def test_rendered_publish_tagged_is_gated_on_v_tag():
    """The `publish-tagged` job's `if:` must gate it on
    `startsWith(github.ref, 'refs/tags/v')` so it only runs on v*
    tag pushes, not on every push to main.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # Find the publish-tagged job and check its `if:`
        lines = ci_yml.split("\n")
        for i, line in enumerate(lines):
            if re.match(r"^\s{2}publish-tagged:\s*$", line):
                # Look ahead for `if:`
                for j in range(i, min(i + 5, len(lines))):
                    if lines[j].lstrip().startswith("if:"):
                        assert "refs/tags/v" in lines[j], (
                            f"publish-tagged job's `if:` doesn't gate on "
                            f"refs/tags/v -- it will run on every push. "
                            f"See PR #104.\n\nGot:\n{lines[j]}"
                        )
                        break
                else:
                    pytest.fail(
                        "publish-tagged job has no `if:`. It will run on "
                        "every push. See PR #104."
                    )
                return
        pytest.fail(
            "publish-tagged job not found in the right shape. See PR #104."
            f"\n\nGot:\n{ci_yml}"
        )


def test_rendered_publish_tagged_pushes_five_tag_shapes():
    """The publish-tagged job must push exactly five tag shapes:
    :X.Y.Z, :X.Y, :X, :<sha>, :latest. Anything more is noise;
    anything less means downstream consumers can't pin.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # Slice out the publish-tagged job only. It can be the last
        # job in the file, in which case we slice to EOF.
        lines = ci_yml.split("\n")
        start = None
        end = len(lines)
        for i, line in enumerate(lines):
            if re.match(r"^\s{2}publish-tagged:\s*$", line):
                start = i
            elif start is not None and re.match(r"^[a-zA-Z]", line):
                # Next top-level key (e.g. another job). Stop.
                end = i
                break
        assert start is not None, "publish-tagged job not found"
        block = "\n".join(lines[start:end])
        # 1. Exact semver
        assert re.search(r"\$\{\{\s*steps\.version\.outputs\.version\s*\}\}", block), (
            "publish-tagged doesn't push the :X.Y.Z tag (from "
            "steps.version.outputs.version). See PR #104."
            f"\n\nBlock:\n{block}"
        )
        # 2. Major.minor
        assert re.search(
            r"\$\{\{\s*steps\.version\.outputs\.major\s*\}\}\.\$\{\{\s*steps\.version\.outputs\.minor\s*\}\}",
            block,
        ), (
            "publish-tagged doesn't push the :X.Y tag. See PR #104."
            f"\n\nBlock:\n{block}"
        )
        # 3. Major
        assert re.search(r"\$\{\{\s*steps\.version\.outputs\.major\s*\}\}", block), (
            "publish-tagged doesn't push the :X tag. See PR #104."
            f"\n\nBlock:\n{block}"
        )
        # 4. Sha
        assert r"${{ github.sha }}" in block, (
            "publish-tagged doesn't push the :<sha> tag. See PR #104."
            f"\n\nBlock:\n{block}"
        )
        # 5. latest
        assert re.search(r":latest\b", block), (
            "publish-tagged doesn't push the :latest tag. See PR #104."
            f"\n\nBlock:\n{block}"
        )


def test_rendered_publish_tagged_validates_semver_shape():
    """The version-derivation step must validate that the tag is a real
    semver (vX.Y.Z or vX.Y.Z-pre.N). A typo like v1.2 or vabc must
    fail the job, not silently push garbage tags.
    """
    with tempfile.TemporaryDirectory() as td:
        g = _gen(registry="ghcr", deploy_target="local", gcp_project_id=None)
        out = g.run()
        ci_yml = (out / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # The rendered bash uses `\\.` (a literal backslash followed
        # by a period, so grep -E sees a regex `\.` -> literal dot).
        # In the YAML file the bash single-quoted line has `\\` (two
        # backslash characters). Look for the substring that proves
        # semver validation is wired in.
        assert "[0-9]+" in ci_yml and "\\.[0-9]+" in ci_yml, (
            "publish-tagged's version step doesn't validate the "
            "semver shape. A typo'd tag (v1.2) should fail the job. "
            "See PR #104."
            f"\n\nGot:\n{ci_yml}"
        )
