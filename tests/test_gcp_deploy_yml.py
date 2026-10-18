"""Tests for the deploy.yml.j2 GitHub Actions workflow for the GCP
overlay (PR 3 of the GKE Autopilot + Cloud SQL milestone).

Scope (PR 3):

- For `--deploy=gcp-gke-autopilot --db=postgres`, deploy.yml must:
  - Add a "Sync db-credentials Secret from Secret Manager" step
    that fetches the password from Secret Manager and writes it
    to a k8s Secret named `db-credentials`.
  - Add a "Wait for ingress IP and patch overlay with real
    hostname" step that polls `kubectl get ingress` and `sed`s
    the placeholder IP into the overlay.
  - In the smoke test, also hit `/readyz` (with a soft warning,
    not a hard fail, on timeout).

- For `--deploy=gcp-gke-autopilot --db=mysql`, deploy.yml must:
  - Skip the Secret sync step (no postgres, no proxy).
  - Still run the ingress IP patch step.
  - Skip the /readyz check.

- For `--deploy=gcp-cloud-run --db=postgres`, deploy.yml must:
  - Skip both the Secret sync step and the ingress IP patch step
    (Cloud Run has its own deployment story and there's no k8s
    tree to patch).

Tests exercise the real `ServiceGenerator.run()` codepath the
scaffolder uses at runtime, so a regression in the jinja wiring
surfaces here.

Run with: pytest tests/test_gcp_deploy_yml.py -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402


def _render_deploy_yml(deploy_target: str, db: str) -> dict:
    """Render the deploy.yml.j2 template via the real generator and
    return the parsed YAML document (the workflow file is a single
    YAML doc)."""
    with tempfile.TemporaryDirectory() as tmp:
        gen = ServiceGenerator(
            name="demo",
            template="go-webapi",
            ci_provider="github-actions",
            deploy_target=deploy_target,
            azure_region="eastus",
            coverage_threshold=80,
            registry="gar",
            db=db,
            output_dir=Path(tmp),
            with_git=False,
            with_readme=True,
            gcp_region="us-central1",
            gcp_project_id="sacscaffolder",
        )
        target = gen.run()
        workflow_path = target / "deploy.yml"
        docs = list(yaml.safe_load_all(workflow_path.read_text()))
        assert len(docs) == 1, f"expected 1 YAML doc, got {len(docs)}"
        return docs[0]


def _steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["deploy"]["steps"]


def _step_names(workflow: dict) -> list[str]:
    return [s.get("name", "<unnamed>") for s in _steps(workflow)]


def _find_step(workflow: dict, name: str) -> dict | None:
    for s in _steps(workflow):
        if s.get("name") == name:
            return s
    return None


class TestGkeAutopilotPostgres:
    """`--deploy=gcp-gke-autopilot --db=postgres` enables all PR 3 steps."""

    def test_secret_sync_step_present(self):
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(wf, "Sync db-credentials Secret from Secret Manager")
        assert step is not None, (
            f"missing Secret sync step; have: {_step_names(wf)}"
        )

    def test_secret_sync_uses_gcloud_secrets_versions_access(self):
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(wf, "Sync db-credentials Secret from Secret Manager")
        run = step["run"]
        assert "gcloud secrets versions access latest" in run
        assert "--secret=" in run
        # Fetches the three values the app needs as envFrom.
        assert "DB_USER" in run
        assert "DB_PASSWORD" in run
        assert "DB_NAME" in run

    def test_secret_sync_uses_terraform_output(self):
        """The Secret ID, db_name, and db_user all come from
        Terraform outputs (PR 1's cloudsql module), not hardcoded."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(wf, "Sync db-credentials Secret from Secret Manager")
        run = step["run"]
        assert "db_password_secret_id" in run
        assert "db_name" in run
        assert "db_user" in run

    def test_secret_sync_uses_kubectl_create_secret_idempotent(self):
        """The step must use `kubectl create --dry-run=client -o yaml |
        kubectl apply -f -` so it's idempotent across deploys (a
        rotated password takes effect on the next push)."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(wf, "Sync db-credentials Secret from Secret Manager")
        run = step["run"]
        assert "kubectl create secret generic db-credentials" in run
        assert "--dry-run=client" in run
        assert "kubectl apply -f -" in run

    def test_ingress_patch_step_present(self):
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(
            wf,
            "Wait for ingress IP and patch overlay with real hostname",
        )
        assert step is not None, (
            f"missing ingress patch step; have: {_step_names(wf)}"
        )

    def test_ingress_patch_uses_kubectl_get_ingress_jsonpath(self):
        """The step must read the GCE LB IP from the Ingress status,
        not from some other source."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        run = step["run"]
        assert "kubectl get ingress" in run
        assert ".status.loadBalancer.ingress[0].ip" in run

    def test_ingress_patch_sed_substitutes_placeholder(self):
        """The dev overlay ships with `<service>.35.35.35.35.nip.io`
        as a placeholder. The step must `sed` that to the real IP."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        run = step["run"]
        assert "sed -i" in run
        # The sed regex form is `35\.35\.35\.35` (each dot escaped
        # for sed's BRE).
        assert "35\\.35\\.35\\.35" in run

    def test_ingress_patch_reapplies_overlay(self):
        """After the `sed`, the step must re-apply the overlay so the
        new hostname takes effect on the Ingress resource."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        run = step["run"]
        # The patch step itself issues exactly one `kubectl apply -k`
        # (the re-apply after the sed). The first apply happens in
        # the kustomize-apply step.
        assert run.count("kubectl apply -k") == 1
        # And it points at the overlay directory.
        assert "k8s/overlays/${ENV}" in run

    def test_ingress_patch_has_retry_loop(self):
        """GCE LBs take 30-90s to provision an IP. The step must poll,
        not assume the IP is available immediately."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        run = step["run"]
        assert "for i in {1.." in run
        assert "sleep 5" in run

    def test_ingress_patch_uses_dev_name_prefix(self):
        """The dev overlay applies `namePrefix: dev-`, so the Ingress
        resource is `dev-<service>`, not `<service>`."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        run = step["run"]
        assert "INGRESS_NAME=\"dev-demo\"" in run

    def test_ingress_patch_does_not_fail_deploy(self):
        """If the LB doesn't provision an IP within 5 minutes, the
        step must not fail the deploy. The app is up; only HTTPS is
        broken (the placeholder hostname doesn't resolve to the LB,
        so the cert can't issue)."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        run = step["run"]
        # The last `exit` in the step must be `exit 0` (or no exit at
        # all, which defaults to 0). The fallback path explicitly
        # chooses `exit 0` over `exit 1` to keep the deploy green.
        assert "exit 0" in run
        assert "exit 1" not in run

    def test_smoke_test_includes_readyz(self):
        """With postgres enabled, the smoke test must also poll /readyz
        (which returns 200 only when the DB connection is up)."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        smoke = _find_step(wf, "Smoke test")
        assert smoke is not None
        run = smoke["run"]
        assert "/readyz" in run

    def test_smoke_test_readyz_is_soft_warning(self):
        """The /readyz check must be a soft warning, not a hard fail.
        A /readyz timeout usually means the cloudsql-proxy can't
        reach the DB (Workload Identity not bound, VPC peering
        broken, etc.), and we don't want to block deploys while we
        debug that."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        smoke = _find_step(wf, "Smoke test")
        run = smoke["run"]
        # ::warning:: is GitHub Actions' soft-fail annotation.
        assert "::warning::" in run
        # The readiness check must NOT cause the step to exit non-zero
        # on its own. The hard fail is reserved for /healthz.
        assert "READYZ_OK" in run


class TestGkeAutopilotNoPostgres:
    """`--deploy=gcp-gke-autopilot --db=mysql` skips the postgres steps."""

    def test_no_secret_sync_step(self):
        wf = _render_deploy_yml("gcp-gke-autopilot", "mysql")
        step = _find_step(wf, "Sync db-credentials Secret from Secret Manager")
        assert step is None, (
            f"Secret sync step should not run without postgres; have: {_step_names(wf)}"
        )

    def test_ingress_patch_still_present(self):
        """The ingress IP patch is for the dev overlay's nip.io
        hostname, not for postgres -- it should run regardless of db."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "mysql")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        assert step is not None

    def test_smoke_test_skips_readyz(self):
        wf = _render_deploy_yml("gcp-gke-autopilot", "mysql")
        smoke = _find_step(wf, "Smoke test")
        run = smoke["run"]
        assert "/readyz" not in run


class TestGcpCloudRun:
    """`--deploy=gcp-cloud-run` skips both PR 3 steps (no k8s tree)."""

    def test_no_secret_sync_step(self):
        wf = _render_deploy_yml("gcp-cloud-run", "postgres")
        step = _find_step(wf, "Sync db-credentials Secret from Secret Manager")
        assert step is None, (
            f"Cloud Run should not have a Secret sync step; have: {_step_names(wf)}"
        )

    def test_no_ingress_patch_step(self):
        wf = _render_deploy_yml("gcp-cloud-run", "postgres")
        step = _find_step(
            wf, "Wait for ingress IP and patch overlay with real hostname",
        )
        assert step is None, (
            f"Cloud Run should not have an ingress patch step; have: {_step_names(wf)}"
        )

    def test_smoke_test_uses_service_url(self):
        """Cloud Run's existing smoke test uses the `service_url`
        Terraform output (PR 1's outputs.tf.j2)."""
        wf = _render_deploy_yml("gcp-cloud-run", "postgres")
        smoke = _find_step(wf, "Smoke test")
        run = smoke["run"]
        assert "service_url" in run


class TestNoRegressionOnExistingSteps:
    """PR 3 must not change the behavior of the pre-existing steps."""

    def test_terraform_apply_unchanged(self):
        """The Terraform apply step must still run on push to main
        only, not on PRs."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(wf, "Terraform apply")
        assert step is not None
        assert "refs/heads/main" in str(step.get("if", ""))

    def test_kustomize_apply_step_unchanged(self):
        """The kustomize-apply step must still inject the freshly
        pushed image into the overlay."""
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        step = _find_step(wf, "kustomize-apply manifests")
        assert step is not None
        run = step["run"]
        assert "kustomize edit set image" in run
        assert "kubectl apply -k" in run

    def test_step_order_is_logical(self):
        """The new steps must appear in the right order:
        1. Secret sync (BEFORE apply, so the Secret exists when
           the Deployment envFrom resolves)
        2. kustomize-apply (creates the Ingress with placeholder host)
        3. ingress IP patch (AFTER apply, because the IP doesn't
           exist before the Ingress is created)
        4. Smoke test (LAST, after everything is up)
        """
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        names = _step_names(wf)
        secret_idx = names.index("Sync db-credentials Secret from Secret Manager")
        kustomize_idx = names.index("kustomize-apply manifests")
        ingress_idx = names.index("Wait for ingress IP and patch overlay with real hostname")
        smoke_idx = names.index("Smoke test")
        assert secret_idx < kustomize_idx < ingress_idx < smoke_idx, (
            f"step order is wrong: {names}"
        )

    def test_terraform_init_and_plan_still_present(self):
        wf = _render_deploy_yml("gcp-gke-autopilot", "postgres")
        assert _find_step(wf, "Terraform init") is not None
        assert _find_step(wf, "Terraform plan") is not None
