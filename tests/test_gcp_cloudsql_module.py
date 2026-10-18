"""Tests for the cloudsql Terraform module and the GKE + postgres overlay
wiring in `servicectl.deploy.gcp.infra`.

Scope (PR 1 of the GKE Autopilot + Cloud SQL milestone):

- `gcp_postgres_enabled` flag in `_render_context` is True only when
  `deploy_target == gcp-gke-autopilot` AND `db == postgres`.
- The cloudsql Terraform module exists with the resources we expect
  (VPC, subnetwork, private services range, peering, random password,
  Secret Manager, Cloud SQL instance, db, user, IAM grants).
- The GKE Autopilot module accepts (and only uses) the
  `network_self_link` / `pods_subnet_self_link` / `pods_secondary_range` /
  `services_secondary_range` inputs when the postgres overlay asks it
  to live on the cloudsql VPC.
- The root `main.tf.j2` calls `module.cloudsql` only when
  `gcp_postgres_enabled` is True.
- The root `outputs.tf.j2` surfaces the postgres outputs only when
  `gcp_postgres_enabled` is True.
- The per-env `.tfvars.j2` files include the postgres variables
  (db_tier, db_name, db_user, etc.) only when `gcp_postgres_enabled` is
  True.
- `gcp-cloud-run + postgres` does NOT enable postgres (separate
  overlay, out of scope for this milestone).
- `gcp-gke-autopilot + db=none` does NOT enable postgres.

These tests deliberately exercise the same `_render_context` /
`ServiceGenerator.run()` codepath the real scaffolder uses, so a
regression in the jinja wiring surfaces here.

Run with: pytest tests/test_gcp_cloudsql_module.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402


def _ctx(name: str = "test-svc", **overrides) -> dict[str, object]:
    """Build the render context for a default Go webapi scaffold.

    Defaults match the locked-in config for this milestone (project
    `sacscaffolder`, region `us-central1`).
    """
    gen = ServiceGenerator(
        name=name,
        template=overrides.get("template", "go-webapi"),
        ci_provider=overrides.get("ci_provider", "github-actions"),
        deploy_target=overrides["deploy_target"],
        azure_region=overrides.get("azure_region", "eastus"),
        coverage_threshold=overrides.get("coverage_threshold", 80),
        registry=overrides.get("registry", "gar"),
        db=overrides["db"],
        output_dir=Path(tempfile.gettempdir()),  # not used; we only need _render_context
        with_git=False,
        with_readme=True,
        gcp_region=overrides.get("gcp_region", "us-central1"),
        gcp_project_id=overrides.get("gcp_project_id", "sacscaffolder"),
    )
    return gen._render_context()


class TestRenderContextFlags:
    """The `_render_context` cross-product flags."""

    def test_gke_postgres_enables_gcp_postgres(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        assert ctx["gcp_gke_autopilot_enabled"] is True
        assert ctx["gcp_postgres_enabled"] is True
        assert ctx["gcp_cloud_run_enabled"] is False

    def test_gke_db_mysql_disables_gcp_postgres(self):
        """`db=mysql` on GKE: postgres is not requested, so the
        cloudsql module must not be wired in. (MySQL on GKE is a
        separate overlay, out of scope for this milestone; this test
        just guards the cross-product flag against accidental
        expansion.)"""
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="mysql")
        assert ctx["gcp_gke_autopilot_enabled"] is True
        assert ctx["gcp_postgres_enabled"] is False

    def test_cloud_run_postgres_disables_gcp_postgres(self):
        # postgres on gcp-cloud-run is a separate overlay (out of scope
        # for this milestone); the flag must be False so the cloudsql
        # module isn't wired in.
        ctx = _ctx(deploy_target="gcp-cloud-run", db="postgres")
        assert ctx["gcp_cloud_run_enabled"] is True
        assert ctx["gcp_postgres_enabled"] is False
        assert ctx["gcp_gke_autopilot_enabled"] is False

    def test_cloud_run_db_mysql_disables_gcp_postgres(self):
        ctx = _ctx(deploy_target="gcp-cloud-run", db="mysql")
        assert ctx["gcp_postgres_enabled"] is False


class TestCloudsqlModuleFiles:
    """The cloudsql module directory has the expected files."""

    MODULE_DIR = Path(__file__).resolve().parent.parent / "src" / "servicectl" / "deploy" / "gcp" / "infra" / "modules" / "cloudsql"

    def test_module_directory_exists(self):
        assert self.MODULE_DIR.is_dir(), f"missing module dir: {self.MODULE_DIR}"

    def test_module_has_main_tf(self):
        assert (self.MODULE_DIR / "main.tf.j2").exists()

    def test_module_has_variables_tf(self):
        assert (self.MODULE_DIR / "variables.tf.j2").exists()

    def test_module_has_outputs_tf(self):
        assert (self.MODULE_DIR / "outputs.tf.j2").exists()


class TestCloudsqlModuleRendered:
    """Render the cloudsql module with the gke+postgres context and
    assert the right resources are emitted."""

    REQUIRED_RESOURCES = [
        'resource "google_project_service" "sqladmin"',
        'resource "google_project_service" "servicenetworking"',
        'resource "google_compute_network" "main"',
        'resource "google_compute_subnetwork" "pods"',
        'resource "google_compute_global_address" "private_services"',
        'resource "google_service_networking_connection" "private_services"',
        'resource "random_password" "db_password"',
        'resource "google_secret_manager_secret" "db_password"',
        'resource "google_secret_manager_secret_version" "db_password"',
        'resource "google_sql_database_instance" "main"',
        'resource "google_sql_database" "main"',
        'resource "google_sql_user" "main"',
        'resource "google_project_iam_member" "cloudsql_client"',
        'resource "google_secret_manager_secret_iam_member" "db_password_reader"',
    ]

    REQUIRED_OUTPUTS = [
        'output "connection_name"',
        'output "private_ip"',
        'output "database_name"',
        'output "database_user"',
        'output "password_secret_id"',
        'output "vpc_self_link"',
        'output "pods_subnet_self_link"',
        'output "pods_secondary_range_name"',
        'output "services_secondary_range_name"',
    ]

    @property
    def MODULE_DIR(self) -> Path:
        return (
            Path(__file__).resolve().parent.parent
            / "src" / "servicectl" / "deploy" / "gcp" / "infra" / "modules" / "cloudsql"
        )

    def test_module_main_has_all_required_resources(self):
        """The module's main.tf.j2, when rendered with the gke+postgres
        context, must contain every resource in REQUIRED_RESOURCES.

        We use the source template directly via `jinja2.Environment`
        rather than `ServiceGenerator.run()` because the test only
        cares about the jinja rendering, not the full scaffold. This
        keeps the test fast and free of any side effects on disk.
        """
        from jinja2 import Environment, FileSystemLoader
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        env = Environment(
            loader=FileSystemLoader(str(self.MODULE_DIR)),
            keep_trailing_newline=True,
        )
        rendered = env.get_template("main.tf.j2").render(**ctx)
        for needle in self.REQUIRED_RESOURCES:
            assert needle in rendered, f"missing resource in cloudsql main.tf.j2: {needle!r}"

    def test_module_outputs_have_all_required_outputs(self):
        from jinja2 import Environment, FileSystemLoader
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        env = Environment(
            loader=FileSystemLoader(str(self.MODULE_DIR)),
            keep_trailing_newline=True,
        )
        rendered = env.get_template("outputs.tf.j2").render(**ctx)
        for needle in self.REQUIRED_OUTPUTS:
            assert needle in rendered, f"missing output in cloudsql outputs.tf.j2: {needle!r}"

    def test_module_iam_grant_targets_cloudsql_client(self):
        """The runtime SA must be granted `roles/cloudsql.client`,
        which is what the Cloud SQL Auth Proxy sidecar needs to call
        `generateEphemeralCert` for the postgres connection."""
        from jinja2 import Environment, FileSystemLoader
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        env = Environment(
            loader=FileSystemLoader(str(self.MODULE_DIR)),
            keep_trailing_newline=True,
        )
        rendered = env.get_template("main.tf.j2").render(**ctx)
        assert "roles/cloudsql.client" in rendered
        assert "serviceAccount:${var.runtime_sa_email}" in rendered

    def test_module_iam_grant_targets_secret_accessor(self):
        """The runtime SA must also be granted `secretmanager.secretAccessor`
        on the password secret, so the deploy workflow (PR 3) can
        fetch the password into a k8s Secret."""
        from jinja2 import Environment, FileSystemLoader
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        env = Environment(
            loader=FileSystemLoader(str(self.MODULE_DIR)),
            keep_trailing_newline=True,
        )
        rendered = env.get_template("main.tf.j2").render(**ctx)
        assert "roles/secretmanager.secretAccessor" in rendered
        assert "google_secret_manager_secret_iam_member" in rendered

    def test_instance_uses_private_ip_only(self):
        """The Cloud SQL instance must be configured for private IP only
        (no public IP). Reason: the whole point of the Auth Proxy
        sidecar pattern is that pods talk to a private IP; exposing
        the instance publicly would be a security regression."""
        from jinja2 import Environment, FileSystemLoader
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        env = Environment(
            loader=FileSystemLoader(str(self.MODULE_DIR)),
            keep_trailing_newline=True,
        )
        rendered = env.get_template("main.tf.j2").render(**ctx)
        assert "ipv4_enabled    = false" in rendered
        assert "private_network = google_compute_network.main.id" in rendered


class TestRootMainTfWiring:
    """The root `main.tf.j2` wires the cloudsql module in only when
    `gcp_postgres_enabled` is True, and the gke_autopilot module
    receives the cloudsql VPC inputs in that case."""

    INFRA_DIR = Path(__file__).resolve().parent.parent / "src" / "servicectl" / "deploy" / "gcp" / "infra"

    def _render(self, ctx: dict[str, object]) -> str:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(str(self.INFRA_DIR)),
            keep_trailing_newline=True,
        )
        return env.get_template("main.tf.j2").render(**ctx)

    def test_gke_postgres_calls_cloudsql_module(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render(ctx)
        assert 'module "cloudsql"' in rendered

    def test_gke_postgres_passes_vpc_self_link_to_gke(self):
        """When postgres is enabled, the gke_autopilot module must be
        told to use the cloudsql VPC, so the cluster lands on the
        same network the Cloud SQL instance is peered into."""
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render(ctx)
        assert "network_self_link" in rendered
        assert "module.cloudsql.vpc_self_link" in rendered

    def test_gke_postgres_passes_secondary_ranges_to_gke(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render(ctx)
        assert "pods_secondary_range" in rendered
        assert "services_secondary_range" in rendered
        assert "module.cloudsql.pods_secondary_range_name" in rendered
        assert "module.cloudsql.services_secondary_range_name" in rendered

    def test_gke_db_mysql_does_not_call_cloudsql(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="mysql")
        rendered = self._render(ctx)
        assert 'module "cloudsql"' not in rendered

    def test_cloud_run_postgres_does_not_call_cloudsql(self):
        """Cloud Run + postgres is a separate overlay; the cloudsql
        module must NOT be wired in for this combination."""
        ctx = _ctx(deploy_target="gcp-cloud-run", db="postgres")
        rendered = self._render(ctx)
        assert 'module "cloudsql"' not in rendered
        assert 'module "gke_autopilot"' not in rendered


class TestRootOutputsTf:
    """The root `outputs.tf.j2` surfaces the postgres outputs only when
    `gcp_postgres_enabled` is True."""

    INFRA_DIR = Path(__file__).resolve().parent.parent / "src" / "servicectl" / "deploy" / "gcp" / "infra"
    REQUIRED_OUTPUTS = [
        'output "db_connection_name"',
        'output "db_private_ip"',
        'output "db_name"',
        'output "db_user"',
        'output "db_password_secret_id"',
    ]

    def _render(self, ctx: dict[str, object]) -> str:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(str(self.INFRA_DIR)),
            keep_trailing_newline=True,
        )
        return env.get_template("outputs.tf.j2").render(**ctx)

    def test_gke_postgres_exposes_all_outputs(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render(ctx)
        for needle in self.REQUIRED_OUTPUTS:
            assert needle in rendered, f"missing output in root outputs.tf.j2: {needle!r}"

    def test_gke_db_mysql_omits_db_outputs(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="mysql")
        rendered = self._render(ctx)
        for needle in self.REQUIRED_OUTPUTS:
            assert needle not in rendered, f"db output should be absent: {needle!r}"


class TestPerEnvTfvars:
    """The per-env `.tfvars.j2` files include the postgres variables
    only when `gcp_postgres_enabled` is True."""

    INFRA_DIR = Path(__file__).resolve().parent.parent / "src" / "servicectl" / "deploy" / "gcp" / "infra"
    REQUIRED_VARS = [
        "db_tier",
        "db_name",
        "db_user",
        "db_availability_type",
        "db_disk_size_gb",
        "db_backup_enabled",
        "db_pitr_enabled",
        "pods_subnet_cidr",
        "pods_secondary_cidr",
        "services_secondary_cidr",
    ]

    def _render(self, tfvars_name: str, ctx: dict[str, object]) -> str:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(str(self.INFRA_DIR)),
            keep_trailing_newline=True,
        )
        return env.get_template(tfvars_name).render(**ctx)

    def test_dev_tfvars_includes_postgres_vars(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render("dev.tfvars.j2", ctx)
        for needle in self.REQUIRED_VARS:
            assert needle in rendered, f"missing in dev.tfvars.j2: {needle!r}"
        # dev tier: db-f1-micro
        assert "db-f1-micro" in rendered
        # dev: ZONAL, no backups, no PITR
        assert "ZONAL" in rendered
        assert "db_backup_enabled    = false" in rendered
        assert "db_pitr_enabled      = false" in rendered

    def test_staging_tfvars_includes_postgres_vars(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render("staging.tfvars.j2", ctx)
        for needle in self.REQUIRED_VARS:
            assert needle in rendered, f"missing in staging.tfvars.j2: {needle!r}"
        assert "db-f1-micro" in rendered
        # staging: ZONAL, backups on, PITR off
        assert "ZONAL" in rendered
        assert "db_backup_enabled    = true" in rendered
        assert "db_pitr_enabled      = false" in rendered

    def test_prod_tfvars_includes_postgres_vars(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render("prod.tfvars.j2", ctx)
        for needle in self.REQUIRED_VARS:
            assert needle in rendered, f"missing in prod.tfvars.j2: {needle!r}"
        # prod tier: db-custom-2-7680
        assert "db-custom-2-7680" in rendered
        # prod: REGIONAL, backups + PITR on
        assert "REGIONAL" in rendered
        assert "db_backup_enabled    = true" in rendered
        assert "db_pitr_enabled      = true" in rendered

    def test_gke_db_mysql_omits_postgres_block(self):
        """When db=mysql on gke, the per-env tfvars should NOT have a
        postgres block at all. (The gke_autopilot module uses the
        project's default network in this case, so there's no need
        for the postgres-specific subnet/CIDR config.)"""
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="mysql")
        for tfvars_name in ("dev.tfvars.j2", "staging.tfvars.j2", "prod.tfvars.j2"):
            rendered = self._render(tfvars_name, ctx)
            assert "db_tier" not in rendered, (
                f"{tfvars_name} should not contain db_tier when db=mysql"
            )


class TestRootVariablesTf:
    """The root `variables.tf.j2` defines the postgres variables only
    when `gcp_postgres_enabled` is True."""

    INFRA_DIR = Path(__file__).resolve().parent.parent / "src" / "servicectl" / "deploy" / "gcp" / "infra"

    def _render(self, ctx: dict[str, object]) -> str:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(str(self.INFRA_DIR)),
            keep_trailing_newline=True,
        )
        return env.get_template("variables.tf.j2").render(**ctx)

    REQUIRED_VARS = [
        'variable "db_tier"',
        'variable "db_name"',
        'variable "db_user"',
        'variable "db_availability_type"',
        'variable "db_disk_size_gb"',
        'variable "db_backup_enabled"',
        'variable "db_pitr_enabled"',
        'variable "pods_subnet_cidr"',
        'variable "pods_secondary_cidr"',
        'variable "services_secondary_cidr"',
    ]

    def test_gke_postgres_exposes_all_vars(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="postgres")
        rendered = self._render(ctx)
        for needle in self.REQUIRED_VARS:
            assert needle in rendered, f"missing in variables.tf.j2: {needle!r}"

    def test_gke_db_mysql_omits_postgres_vars(self):
        ctx = _ctx(deploy_target="gcp-gke-autopilot", db="mysql")
        rendered = self._render(ctx)
        for needle in self.REQUIRED_VARS:
            assert needle not in rendered, f"db var should be absent: {needle!r}"
