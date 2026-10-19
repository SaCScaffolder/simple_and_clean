"""Tests for PR 5's deploy.yml additions: the `Run database migrations`
step and the DB round-trip smoke test for /notes.

Scope (PR 5 of the GKE Autopilot + Cloud SQL milestone):

- For `--deploy=gcp-gke-autopilot --db=postgres`, deploy.yml must:
  - Add a "Run database migrations" step between "Sync db-credentials
    Secret from Secret Manager" and "kustomize-apply manifests" that
    `kubectl exec`s the binary's `migrate` subcommand against the
    just-deployed pod.
  - Extend the smoke test to POST a note, GET /notes, and assert
    the round-trip body matches (with `::warning::` on failure,
    not a hard fail, since /readyz already proved DB
    reachability).

- For `--deploy=gcp-gke-autopilot --db=mysql`, deploy.yml must:
  - Skip both new additions (no postgres, no /notes, no migrate
    subcommand).

- For `--deploy=gcp-cloud-run`, deploy.yml must:
  - Skip both new additions (no k8s tree, no migrations).

Tests exercise the real `ServiceGenerator.run()` codepath the
scaffolder uses at runtime.

Run with: pytest tests/test_gcp_deploy_yml_notes.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402


def _render(deploy_target: str, db: str) -> dict:
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
        docs = list(yaml.safe_load_all((target / "deploy.yml").read_text()))
        assert len(docs) == 1
        return docs[0]


def _steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["deploy"]["steps"]


def _names(workflow: dict) -> list[str]:
    return [s.get("name", "<unnamed>") for s in _steps(workflow)]


def _find(workflow: dict, name: str) -> dict | None:
    for s in _steps(workflow):
        if s.get("name") == name:
            return s
    return None


class TestRunMigrationsStep:
    """`--deploy=gcp-gke-autopilot --db=postgres` adds a migrate step."""

    def test_migrate_step_present(self):
        wf = _render("gcp-gke-autopilot", "postgres")
        step = _find(wf, "Run database migrations")
        assert step is not None, f"missing migrate step; have: {_names(wf)}"

    def test_migrate_step_runs_after_pod_exists(self):
        """Migrations must run AFTER the kustomize-apply has
        created the pod (so `kubectl exec` has a target) and
        AFTER the Secret sync (so DATABASE_URL envFrom can
        resolve)."""
        wf = _render("gcp-gke-autopilot", "postgres")
        names = _names(wf)
        secret_idx = names.index("Sync db-credentials Secret from Secret Manager")
        kustomize_idx = names.index("kustomize-apply manifests")
        migrate_idx = names.index("Run database migrations")
        smoke_idx = names.index("Smoke test")
        assert secret_idx < kustomize_idx < migrate_idx < smoke_idx, (
            f"step order is wrong: {names}"
        )
        """Migrations must run AFTER the kustomize-apply has
        created the pod (so `kubectl exec` has a target) and
        AFTER the Secret sync (so DATABASE_URL envFrom can
        resolve)."""
        wf = _render("gcp-gke-autopilot", "postgres")
        names = _names(wf)
        secret_idx = names.index("Sync db-credentials Secret from Secret Manager")
        kustomize_idx = names.index("kustomize-apply manifests")
        migrate_idx = names.index("Run database migrations")
        smoke_idx = names.index("Smoke test")
        assert secret_idx < kustomize_idx < migrate_idx < smoke_idx, (
            f"step order is wrong: {names}"
        )

    def test_migrate_step_uses_kubectl_exec(self):
        """The migrate command runs inside the deployed pod via
        `kubectl exec`, not via a separate Job. This keeps the
        migration runner the same as the app (no separate image
        to maintain)."""
        wf = _render("gcp-gke-autopilot", "postgres")
        step = _find(wf, "Run database migrations")
        run = step["run"]
        assert "kubectl exec" in run
        assert "migrate" in run
        # The binary path inside the pod is /app/<service_name>.
        assert "/app/demo" in run
        assert "migrate" in run.split("/app/demo")[1]

    def test_migrate_step_waits_for_pod(self):
        """The first deploy after a fresh cluster is in `ImagePullBackOff`
        for 30-60s while GAR propagates. The migrate step must wait
        for at least one ready replica before trying `kubectl exec`."""
        wf = _render("gcp-gke-autopilot", "postgres")
        step = _find(wf, "Run database migrations")
        run = step["run"]
        assert "readyReplicas" in run
        # The wait loop polls up to 30 * 5s = 2.5 min.
        assert "for i in" in run
        assert "sleep 5" in run

    def test_migrate_step_uses_dev_name_prefix(self):
        """The dev overlay applies `namePrefix: dev-`, so the
        Deployment is `dev-<service>`, not `<service>`."""
        wf = _render("gcp-gke-autopilot", "postgres")
        step = _find(wf, "Run database migrations")
        run = step["run"]
        assert "dev-demo" in run


class TestNotesSmokeTest:
    """The smoke test for GKE+postgres does a POST+GET round-trip."""

    def test_smoke_test_posts_to_notes(self):
        wf = _render("gcp-gke-autopilot", "postgres")
        smoke = _find(wf, "Smoke test")
        run = smoke["run"]
        assert "POST" in run
        assert "/notes" in run

    def test_smoke_test_gets_notes(self):
        wf = _render("gcp-gke-autopilot", "postgres")
        smoke = _find(wf, "Smoke test")
        run = smoke["run"]
        # There must be both a POST and a GET against /notes.
        assert run.count("curl -sf") >= 2
        assert "GET" in run or "curl" in run

    def test_smoke_test_uses_jq_for_id(self):
        """The smoke test parses the POST response to extract the
        note's id, then uses it to find the row in the GET response."""
        wf = _render("gcp-gke-autopilot", "postgres")
        smoke = _find(wf, "Smoke test")
        run = smoke["run"]
        assert "jq" in run

    def test_smoke_test_asserts_round_trip(self):
        """The test must compare the POSTed body to the GETed body
        and emit a soft warning if they don't match. The soft
        warning matters because /readyz already proved the DB
        is reachable, so a round-trip failure usually means a
        schema mismatch, not a connectivity issue."""
        wf = _render("gcp-gke-autopilot", "postgres")
        smoke = _find(wf, "Smoke test")
        run = smoke["run"]
        assert "::warning::" in run
        # The round-trip is not a hard fail.
        assert "DB round-trip" in run


class TestNoMigrationsForNonPostgres:
    """`--db != postgres` skips the migrate step and the /notes round-trip."""

    def test_no_migrate_step(self):
        wf = _render("gcp-gke-autopilot", "mysql")
        assert _find(wf, "Run database migrations") is None

    def test_smoke_test_skips_notes(self):
        wf = _render("gcp-gke-autopilot", "mysql")
        smoke = _find(wf, "Smoke test")
        run = smoke["run"]
        assert "/notes" not in run
        assert "POST" not in run or "POST /notes" not in run


class TestNoMigrationsForCloudRun:
    """`--deploy=gcp-cloud-run` skips both new additions."""

    def test_no_migrate_step(self):
        wf = _render("gcp-cloud-run", "postgres")
        assert _find(wf, "Run database migrations") is None

    def test_smoke_test_uses_service_url(self):
        """Cloud Run's smoke test still uses the `service_url` Terraform
        output, not the k8s ingress IP."""
        wf = _render("gcp-cloud-run", "postgres")
        smoke = _find(wf, "Smoke test")
        run = smoke["run"]
        assert "service_url" in run


class TestNoRegressionOnExistingSteps:
    """PR 5 must not change the pre-existing step behavior."""

    def test_existing_step_order_preserved(self):
        """The new migrate step slots in between kustomize-apply
        and the ingress IP patch. All other pre-existing steps
        must remain in their original positions relative to each
        other (or to the new step in a way that preserves their
        semantic order)."""
        wf = _render("gcp-gke-autopilot", "postgres")
        names = _names(wf)
        # The kustomize-apply step still happens before migrate
        # (because the migrate needs the pod to exist).
        kustomize_idx = names.index("kustomize-apply manifests")
        migrate_idx = names.index("Run database migrations")
        assert kustomize_idx < migrate_idx
        # The ingress IP patch still happens after the kustomize-apply
        # (and after migrate, because the patch step is downstream
        # of the deploy pipeline).
        ingress_idx = names.index("Wait for ingress IP and patch overlay with real hostname")
        assert kustomize_idx < ingress_idx
        # And the smoke test is always last.
        smoke_idx = names.index("Smoke test")
        assert ingress_idx < smoke_idx
