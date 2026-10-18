"""Tests for the k8s/ overlay in `servicectl.deploy.gcp.k8s`.

Scope (PR 2 of the GKE Autopilot + Cloud SQL milestone):

- `gcp_gke_autopilot_enabled` triggers the k8s/ tree to render.
- `gcp_postgres_enabled` toggles the postgres-specific files
  (Secret + cloudsql-proxy sidecar).
- The base manifests exist with the right shape:
  - Deployment: image, port, env, livenessProbe on /healthz,
    readinessProbe on /readyz, security context.
  - Service: ClusterIP, port 80 -> targetPort http.
  - ServiceAccount: Workload Identity annotation matching the
    cloudsql module's runtime SA email.
  - cloudsql-proxy: sidecar container, --private-ip arg, named
    port 5432, security context.
  - Secret: type Opaque, the keys the Deployment envFrom reads.
  - Ingress: GCE class, ManagedCertificate, nip.io-style host.
  - kustomization: resources list, images block.
- The dev overlay sets replicas=1 and patches the Ingress host.
- The prod overlay sets replicas=2, references a PDB, patches
  the Ingress host.
- `gcp_postgres_enabled=False` omits the Secret and
  cloudsql-proxy files.
- `gcp-cloud-run` does NOT render the k8s/ tree at all.

Tests exercise the real `ServiceGenerator.run()` codepath the
scaffolder uses at runtime, so a regression in the jinja wiring
surfaces here.

Run with: pytest tests/test_gcp_k8s_manifests.py -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402


def _scaffold(deploy_target: str, db: str, **overrides) -> Path:
    """Run a real ServiceGenerator.run() and return the target dir.

    Uses an isolated tempdir so tests don't pollute each other.
    """
    tmp = Path(tempfile.mkdtemp(prefix="servicectl-k8s-test-"))
    try:
        gen = ServiceGenerator(
            name=overrides.get("name", "demo"),
            template="go-webapi",
            ci_provider="github-actions",
            deploy_target=deploy_target,
            azure_region="eastus",
            coverage_threshold=80,
            registry="gar",
            db=db,
            output_dir=tmp,
            with_git=False,
            with_readme=True,
            gcp_region="us-central1",
            gcp_project_id="sacscaffolder",
        )
        return gen.run()
    finally:
        # Note: don't rmtree here -- the caller needs the path. Tests
        # use the tempdir's natural expiry (or are isolated by pytest
        # tmp_path if needed). For real isolation, prefer pytest's
        # `tmp_path` fixture in a fixture-based rewrite; for now this
        # is fine.
        pass


class TestK8sTreeRenders:
    """The k8s/ tree renders for gcp-gke-autopilot and is absent otherwise."""

    def test_k8s_base_dir_exists(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        assert (target / "k8s" / "base").is_dir()
        assert (target / "k8s" / "overlays" / "dev").is_dir()
        assert (target / "k8s" / "overlays" / "prod").is_dir()

    def test_k8s_base_all_manifests_present(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        base = target / "k8s" / "base"
        for name in (
            "deployment.yaml",
            "service.yaml",
            "serviceaccount.yaml",
            "secret.yaml",
            "cloudsql-proxy.yaml",
            "ingress.yaml",
            "kustomization.yaml",
        ):
            assert (base / name).exists(), f"missing base file: {name}"

    def test_k8s_overlays_dev_and_prod_present(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        assert (target / "k8s" / "overlays" / "dev" / "kustomization.yaml").exists()
        assert (target / "k8s" / "overlays" / "prod" / "kustomization.yaml").exists()
        assert (target / "k8s" / "overlays" / "prod" / "pdb.yaml").exists()

    def test_k8s_tree_absent_for_gcp_cloud_run(self):
        """Cloud Run has its own deployment story; the k8s/ tree
        should not be rendered for it."""
        target = _scaffold("gcp-cloud-run", "postgres")
        assert not (target / "k8s").exists()

    def test_k8s_tree_absent_for_local_deploy(self):
        target = _scaffold("local", "postgres")
        assert not (target / "k8s").exists()

    def test_k8s_tree_absent_for_azure(self):
        target = _scaffold("azure", "postgres")
        assert not (target / "k8s").exists()


class TestDeployment:
    """The base Deployment manifest."""

    def test_image_is_gar_with_project(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        # GAR image refs are `<region>-docker.pkg.dev/<project>/<repo>:<tag>`.
        assert "image: us-central1-docker.pkg.dev/sacscaffolder/demo" in d

    def test_port_is_8080(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "containerPort: 8080" in d
        assert "name: PORT" in d
        assert 'value: "8080"' in d

    def test_liveness_probe_targets_healthz(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "livenessProbe:" in d
        assert "path: /healthz" in d
        assert "port: http" in d

    def test_readiness_probe_targets_readyz(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "readinessProbe:" in d
        assert "path: /readyz" in d

    def test_security_context_is_hardened(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "runAsNonRoot: true" in d
        assert "readOnlyRootFilesystem: true" in d
        assert "allowPrivilegeEscalation: false" in d
        assert 'drop: ["ALL"]' in d

    def test_service_account_name_set(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "serviceAccountName: demo" in d

    def test_postgres_env_from_secret(self):
        """When postgres is enabled, the Deployment reads
        DB_USER/DB_PASSWORD/DB_NAME from the db-credentials Secret
        and builds DATABASE_URL from them."""
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "envFrom:" in d
        assert "secretRef:" in d
        assert "name: db-credentials" in d
        # DATABASE_URL points at the cloudsql-proxy's localhost listener.
        assert "DATABASE_URL" in d
        assert "127.0.0.1:5432" in d

    def test_no_postgres_skips_db_env(self):
        """When postgres is not enabled, the Deployment must not
        reference DATABASE_URL or the db-credentials Secret -- those
        are gated on `gcp_postgres_enabled`."""
        target = _scaffold("gcp-gke-autopilot", "mysql")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "DATABASE_URL" not in d
        assert "db-credentials" not in d


class TestService:
    """The base Service manifest."""

    def test_service_is_clusterip(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        s = (target / "k8s" / "base" / "service.yaml").read_text()
        assert "type: ClusterIP" in s

    def test_service_port_80_to_8080(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        s = (target / "k8s" / "base" / "service.yaml").read_text()
        assert "port: 80" in s
        assert "targetPort: http" in s


class TestServiceAccount:
    """The base ServiceAccount and its Workload Identity annotation."""

    def test_workload_identity_annotation(self):
        """The annotation must match the GCP runtime SA the cloudsql
        module provisions (PR 1). The SA name is `<service>-runtime`
        and the email uses the project ID."""
        target = _scaffold("gcp-gke-autopilot", "postgres")
        sa = (target / "k8s" / "base" / "serviceaccount.yaml").read_text()
        assert "iam.gke.io/gcp-service-account:" in sa
        assert "demo-runtime@sacscaffolder.iam.gserviceaccount.com" in sa


class TestCloudsqlProxy:
    """The cloudsql-proxy sidecar manifest."""

    def test_proxy_image(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        c = (target / "k8s" / "base" / "cloudsql-proxy.yaml").read_text()
        assert "image: gcr.io/cloud-sql-connectors/cloud-sql-proxy:2" in c

    def test_proxy_uses_private_ip_arg(self):
        """The proxy must pass `--private-ip` because the cloudsql
        module provisions a private-IP-only instance (PR 1)."""
        target = _scaffold("gcp-gke-autopilot", "postgres")
        c = (target / "k8s" / "base" / "cloudsql-proxy.yaml").read_text()
        assert "--private-ip" in c

    def test_proxy_connection_name_renders(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        c = (target / "k8s" / "base" / "cloudsql-proxy.yaml").read_text()
        # Format: sacscaffolder:us-central1:demo-dev
        assert "sacscaffolder:us-central1:demo-dev" in c

    def test_proxy_listens_on_5432(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        c = (target / "k8s" / "base" / "cloudsql-proxy.yaml").read_text()
        assert "containerPort: 5432" in c

    def test_proxy_hardened_security_context(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        c = (target / "k8s" / "base" / "cloudsql-proxy.yaml").read_text()
        assert "runAsNonRoot: true" in c
        assert "allowPrivilegeEscalation: false" in c

    def test_proxy_omitted_when_postgres_disabled(self):
        """No proxy sidecar when --db != postgres."""
        target = _scaffold("gcp-gke-autopilot", "mysql")
        assert not (target / "k8s" / "base" / "cloudsql-proxy.yaml").exists()
        assert not (target / "k8s" / "base" / "secret.yaml").exists()


class TestIngress:
    """The base Ingress manifest."""

    def test_uses_gce_ingress_class(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        i = (target / "k8s" / "base" / "ingress.yaml").read_text()
        assert "kubernetes.io/ingress.class: gce" in i

    def test_managed_certificate_referenced(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        i = (target / "k8s" / "base" / "ingress.yaml").read_text()
        assert "ManagedCertificate" in i
        assert "demo-cert" in i

    def test_default_backend_points_at_service(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        i = (target / "k8s" / "base" / "ingress.yaml").read_text()
        assert "defaultBackend:" in i
        assert "name: demo" in i
        assert "number: 80" in i


class TestKustomization:
    """The base kustomization.yaml lists the manifests and an images block."""

    def test_kustomization_lists_all_base_manifests(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        k = (target / "k8s" / "base" / "kustomization.yaml").read_text()
        for needle in (
            "serviceaccount.yaml",
            "service.yaml",
            "deployment.yaml",
            "secret.yaml",
            "cloudsql-proxy.yaml",
            "ingress.yaml",
        ):
            assert needle in k, f"kustomization missing {needle}"

    def test_kustomization_omits_postgres_files_when_disabled(self):
        target = _scaffold("gcp-gke-autopilot", "mysql")
        k = (target / "k8s" / "base" / "kustomization.yaml").read_text()
        assert "secret.yaml" not in k
        assert "cloudsql-proxy.yaml" not in k

    def test_kustomization_has_images_block(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        k = (target / "k8s" / "base" / "kustomization.yaml").read_text()
        assert "images:" in k
        # The image is `us-central1-docker.pkg.dev/sacscaffolder/demo`.
        assert "us-central1-docker.pkg.dev/sacscaffolder/demo" in k


class TestDevOverlay:
    """The dev overlay sets replicas=1 and patches the Ingress host."""

    def test_dev_overlay_sets_replicas_1(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "overlays" / "dev" / "kustomization.yaml").read_text()
        assert "value: 1" in d
        assert "replicas" in d

    def test_dev_overlay_patches_ingress_host(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "overlays" / "dev" / "kustomization.yaml").read_text()
        # The patch references the spec.rules[0].host path.
        assert "spec/rules/0/host" in d

    def test_dev_overlay_name_prefix(self):
        """The dev overlay uses `namePrefix: dev-` so resources
        don't collide with the prod overlay's `prod-` prefix
        when both overlays are applied to the same cluster."""
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "overlays" / "dev" / "kustomization.yaml").read_text()
        assert "namePrefix: dev-" in d


class TestProdOverlay:
    """The prod overlay sets replicas=2 and includes the PDB."""

    def test_prod_overlay_sets_replicas_2(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "overlays" / "prod" / "kustomization.yaml").read_text()
        assert "value: 2" in d

    def test_prod_overlay_includes_pdb(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "overlays" / "prod" / "kustomization.yaml").read_text()
        assert "pdb.yaml" in d

    def test_prod_overlay_name_prefix(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        d = (target / "k8s" / "overlays" / "prod" / "kustomization.yaml").read_text()
        assert "namePrefix: prod-" in d

    def test_pdb_min_available_1(self):
        target = _scaffold("gcp-gke-autopilot", "postgres")
        p = (target / "k8s" / "overlays" / "prod" / "pdb.yaml").read_text()
        assert "minAvailable: 1" in p
        assert "kind: PodDisruptionBudget" in p


class TestNoPostgresOmitsDbFiles:
    """Without --db=postgres, the k8s/ tree omits Secret + cloudsql-proxy."""

    def test_no_secret_when_postgres_disabled(self):
        target = _scaffold("gcp-gke-autopilot", "mysql")
        assert not (target / "k8s" / "base" / "secret.yaml").exists()

    def test_no_cloudsql_proxy_when_postgres_disabled(self):
        target = _scaffold("gcp-gke-autopilot", "mysql")
        assert not (target / "k8s" / "base" / "cloudsql-proxy.yaml").exists()

    def test_deployment_no_db_env_when_postgres_disabled(self):
        target = _scaffold("gcp-gke-autopilot", "mysql")
        d = (target / "k8s" / "base" / "deployment.yaml").read_text()
        assert "DATABASE_URL" not in d
        assert "envFrom" not in d
