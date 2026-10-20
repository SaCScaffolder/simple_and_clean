"""Regression tests for the renderer's `service_id` normalization.

What this PR fixes:

- The GCP overlay used to embed `{{ service_name }}` directly into
  cloud/k8s/Terraform resource IDs (pool IDs, repo IDs, SA account_ids,
  k8s Deployment names, ingress names, etc.). GCP, k8s, and Terraform
  all reject underscores, dots, uppercase, and spaces in resource
  names — but `servicectl init --name=...` previously accepted any
  of those.
- The next deploy step after the bootstrap.sh display-name cap fix
  (PR #140) would have hit
  `INVALID_ARGUMENT: Invalid WorkloadIdentityPool ID. It can only
  consist of digits [0-9], lowercase characters [a-z], dashes [-]`
  on a user input like `ala_gcp_service_demo`. Verified against
  the `sacscaffolder` GCP project with a manual
  `gcloud iam workload-identity-pools create
  ala_service_gcp_demo2-pool --location=global
  --project=sacscaffolder` call.

What this PR does:

- `ServiceGenerator.__post_init__` now lowercases the user's typed
  name and collapses `_`, `.`, ` ` to `-` (and strips
  leading/trailing dashes). After this method runs, `self.name`
  is always the canonical dashed-lowercase form regardless of
  what the user typed.
- `_render_context` exposes `service_id` as an explicit-name alias
  for `self.name` post-normalization, so templates that need a
  resource-ID-legal value can use `{{ service_id }}` and make the
  intent visible at the call site.
- The GCP overlay swaps `{{ service_name }}` -> `{{ service_id }}`
  at every site that lands in a resource ID. Human-readable
  places (display_name, description, comments) keep
  `{{ service_name }}`.

These tests cover:

  1. `__post_init__` normalization (snake, dotted, mixed case,
     whitespace, leading/trailing separators, all at once).
  2. Rejection of names that normalize to empty.
  3. The render context exposes `service_id` matching the
     normalized `self.name`.
  4. The snake and pascal derivations still work correctly when
     fed by a normalized input (i.e. they produce the same shapes
     they'd produce if the user had typed a dashed input).
  5. End-to-end: rendering a service with `name="ala_service_gcp_demo"`
     produces `infra/main.tf` and `infra/bootstrap.sh` whose
     resource-ID fields use `ala-service-gcp-demo` (dashed) and
     never contain `ala_service_gcp_demo` (snake).
"""
import re
import tempfile
from pathlib import Path

import pytest

from servicectl.generator import ServiceGenerator, ScaffoldError


# --- __post_init__ normalization tests --------------------------------------


def _make_gen(name: str, output_dir: Path) -> ServiceGenerator:
    """Build a ServiceGenerator with the minimum args needed to
    exercise __post_init__. We pass output_dir to a non-existent
    path so the generator doesn't try to scaffold anything."""
    return ServiceGenerator(
        name=name,
        template="go-webapi",
        ci_provider="github-actions",
        deploy_target="gcp-gke-autopilot",
        azure_region="eastus",
        coverage_threshold=80,
        registry="gar",
        db="postgres",
        output_dir=output_dir,
        with_git=False,
        with_readme=True,
        gcp_region="us-central1",
        gcp_project_id="sacscaffolder",
    )


def test_snake_case_input_normalizes(tmp_path: Path) -> None:
    """User types snake_case; self.name becomes dashed-lowercase."""
    gen = _make_gen("ala_gcp_service_demo", tmp_path)
    assert gen.name == "ala-gcp-service-demo"


def test_dotted_input_normalizes(tmp_path: Path) -> None:
    gen = _make_gen("my.service.v2", tmp_path)
    assert gen.name == "my-service-v2"


def test_uppercase_input_normalizes(tmp_path: Path) -> None:
    gen = _make_gen("MyService", tmp_path)
    assert gen.name == "myservice"


def test_whitespace_input_normalizes(tmp_path: Path) -> None:
    gen = _make_gen("my service", tmp_path)
    assert gen.name == "my-service"


def test_already_dashed_input_unchanged(tmp_path: Path) -> None:
    """If the user typed a dashed-lowercase name, normalization is
    a no-op. This locks in the behavior that pre-existing tests
    using names like 'demo' or 'my-custom-name' produce identical
    output to before the PR."""
    gen = _make_gen("my-custom-name", tmp_path)
    assert gen.name == "my-custom-name"


def test_leading_trailing_separators_stripped(tmp_path: Path) -> None:
    gen = _make_gen("-foo-", tmp_path)
    assert gen.name == "foo"


def test_mixed_separators_collapse(tmp_path: Path) -> None:
    """Snake + dot + space + case, all at once."""
    gen = _make_gen("My_Service.v2 release", tmp_path)
    assert gen.name == "my-service-v2-release"


def test_only_separators_rejected(tmp_path: Path) -> None:
    """Service name that normalizes to empty raises ScaffoldError.

    Without this guard, we'd accept input like `___` and end up
    with `self.name == ""`, which would then crash every downstream
    consumer (Terraform `name = ""`, k8s `name: ""`, etc.)."""
    with pytest.raises(ScaffoldError) as exc_info:
        _make_gen("___", tmp_path)
    assert "normalizes to empty" in str(exc_info.value)


# --- _render_context exposure -----------------------------------------------


def test_service_id_exposed_in_render_context(tmp_path: Path) -> None:
    """After normalization, the render context exposes `service_id`
    as an alias for `service_name`. The two are equal but the
    intent at the call site differs: `service_id` is the value
    to use in a resource ID."""
    gen = _make_gen("ala_gcp_service_demo", tmp_path)
    ctx = gen._render_context()
    assert ctx["service_id"] == "ala-gcp-service-demo"
    assert ctx["service_name"] == "ala-gcp-service-demo"
    # service_name_snake is now derived from the normalized
    # self.name (which is already dashed), so the .replace("-","_")
    # transform is what produces the snake form.
    assert ctx["service_name_snake"] == "ala_gcp_service_demo"
    # Pascal is "TitleCase" each dash-separated segment.
    assert ctx["service_name_pascal"] == "AlaGcpServiceDemo"


def test_render_context_uses_normalized_name_for_resource_ids(tmp_path: Path) -> None:
    """gcp_runtime_sa_email and db_connection_name are built from
    self.name directly. After normalization, the email and
    connection string use the dashed form."""
    gen = _make_gen("ala_gcp_service_demo", tmp_path)
    ctx = gen._render_context()
    assert ctx["gcp_runtime_sa_email"].startswith("ala-gcp-service-demo-runtime@")
    assert ctx["db_connection_name"].endswith(":ala-gcp-service-demo-dev")


# --- End-to-end render test -------------------------------------------------


def test_rendered_resource_ids_use_dashed_form(tmp_path: Path) -> None:
    """Full codepath: render a service named `ala_service_gcp_demo`
    (snake) and assert every place that *should* use the dashed form
    does, and that the snake form does not leak into resource-ID
    fields.

    We render the service to a real tempdir using gen.run() to
    catch any wiring regressions (e.g. if service_id stops being
    passed through to a template that needs it)."""
    target_dir = tmp_path / "ala_service_gcp_demo"
    gen = ServiceGenerator(
        name="ala_service_gcp_demo",
        template="go-webapi",
        ci_provider="github-actions",
        deploy_target="gcp-gke-autopilot",
        azure_region="eastus",
        coverage_threshold=80,
        registry="gar",
        db="postgres",
        output_dir=tmp_path,
        with_git=False,
        with_readme=True,
        gcp_region="us-central1",
        gcp_project_id="sacscaffolder",
    )
    target = gen.run()

    main_tf = (target / "infra" / "main.tf").read_text(encoding="utf-8")
    bootstrap_sh = (target / "infra" / "bootstrap.sh").read_text(encoding="utf-8")
    variables_tf = (target / "infra" / "variables.tf").read_text(encoding="utf-8")
    deployment_yaml = (target / "k8s" / "base" / "deployment.yaml").read_text(encoding="utf-8")
    cloudsql_proxy_yaml = (target / "k8s" / "base" / "cloudsql-proxy.yaml").read_text(encoding="utf-8")
    service_yaml = (target / "k8s" / "base" / "service.yaml").read_text(encoding="utf-8")
    serviceaccount_yaml = (target / "k8s" / "base" / "serviceaccount.yaml").read_text(encoding="utf-8")
    ingress_yaml = (target / "k8s" / "base" / "ingress.yaml").read_text(encoding="utf-8")

    # Positive assertions: the dashed form appears where expected.
    assert 'account_id   = "ala-service-gcp-demo-runtime"' in main_tf
    assert 'repository_id = "ala-service-gcp-demo"' in main_tf
    assert 'cluster_name          = "ala-service-gcp-demo-' in main_tf
    # service_name in cloudsql module input (Cloud Run branch is gated
    # off because this test uses gcp-gke-autopilot; see main.tf.j2 L154).
    assert 'service_name     = "ala-service-gcp-demo"' in main_tf  # cloudsql module input
    assert 'POOL_ID="ala-service-gcp-demo-github-pool"' in bootstrap_sh
    assert 'PROVIDER_ID="ala-service-gcp-demo-github-provider"' in bootstrap_sh
    assert 'CI_SA_ID="ala-service-gcp-demo-ci"' in bootstrap_sh
    assert 'DISPLAY_POOL="ala-service-gcp-demo gh pool"' in bootstrap_sh
    assert 'DISPLAY_CI_SA="ala-service-gcp-demo ci sa"' in bootstrap_sh
    assert 'default     = "gar/ala-service-gcp-demo:latest"' in variables_tf
    assert "name: ala-service-gcp-demo" in deployment_yaml
    assert "app: ala-service-gcp-demo" in deployment_yaml
    assert "serviceAccountName: ala-service-gcp-demo" in deployment_yaml
    assert "name: ala-service-gcp-demo-cloudsql-proxy" in cloudsql_proxy_yaml
    assert "name: ala-service-gcp-demo" in service_yaml
    assert "name: ala-service-gcp-demo" in serviceaccount_yaml
    assert "name: ala-service-gcp-demo-cert" in ingress_yaml

    # Negative assertion: the snake form must NOT appear in any
    # resource-ID context. Comments and Terraform variable names
    # (which contain the literal string `service_name` as an
    # identifier, not a value) are excluded.
    snake = "ala_service_gcp_demo"
    # Strip Terraform comments (// ...) and HTML comments (<!-- ... -->)
    # and Terraform variable *names* (e.g. `variable "service_name"`),
    # then assert no snake form remains.
    def strip_non_id_context(text: str) -> str:
        # Strip // ... end-of-line comments (HCL)
        text = re.sub(r"//[^\n]*", "", text)
        # Strip # ... end-of-line comments (HCL/Terraform)
        text = re.sub(r"#[^\n]*", "", text)
        # Strip <!-- ... --> blocks (none in our templates, but safe)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        # Strip `variable "service_name"` style declarations
        # (the word `service_name` is the variable name, not a value)
        text = re.sub(r'variable\s+"[^"]+"\s*\{', "", text)
        return text

    for label, text in [
        ("main.tf", main_tf),
        ("bootstrap.sh", bootstrap_sh),
        ("variables.tf", variables_tf),
        ("k8s/base/deployment.yaml", deployment_yaml),
        ("k8s/base/cloudsql-proxy.yaml", cloudsql_proxy_yaml),
        ("k8s/base/service.yaml", service_yaml),
        ("k8s/base/serviceaccount.yaml", serviceaccount_yaml),
        ("k8s/base/ingress.yaml", ingress_yaml),
    ]:
        stripped = strip_non_id_context(text)
        assert snake not in stripped, (
            f"{label}: snake-case service name still appears in a "
            f"resource-ID context. Snippet:\n{stripped[max(0, stripped.find(snake)-100):stripped.find(snake)+100]}"
        )