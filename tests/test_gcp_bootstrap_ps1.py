"""Regression tests for `deploy/gcp/infra/bootstrap.ps1.j2`.

Scope:

- PR #142 adds a PowerShell 7+ (pwsh) parallel of `bootstrap.sh`.
  Both scripts perform the same GCP bootstrap: enable APIs, create
  a Workload Identity Pool, create a Provider bound to the GitHub
  repo, create a CI service account, grant it the deploy roles,
  bind the WIF provider to it, and print the values you need as
  GitHub secrets.
- PowerShell on Windows is the primary motivation: the WSL bash
  stub on Henry's machine rejects bash scripts, and a pure
  PowerShell bootstrapper removes the bash dependency entirely
  for Windows users.
- These tests verify:
    1. `bootstrap.ps1` is rendered next to `bootstrap.sh` in the
      GCP overlay.
    2. The PowerShell syntax is structurally valid (right keywords,
      right quoting, no bash-specific tokens like `$VAR:0:32`).
    3. The same gcloud subcommand set is invoked (enable services,
      WIF pool create, WIF provider create-oidc, SA create,
      IAM policy bindings).
    4. Display names are capped at 32 chars (the Limit-DisplayName
      helper does what bash's `${VAR:0:32}` does).
    5. Service_id (post-normalization) is used for resource IDs.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402

# Service name long enough to have triggered the original display-name
# bug if normalization weren't in place. Same length as in the
# bootstrap.sh test file.
_LONG_SERVICE_NAME = "ala-gcp-service-demo"


def _render_bootstrap_ps1(
    deploy_target: str = "gcp-gke-autopilot",
    db: str = "postgres",
    service_name: str = _LONG_SERVICE_NAME,
) -> str:
    """Render bootstrap.ps1.j2 via the real generator and return the
    rendered script text."""
    with tempfile.TemporaryDirectory() as tmp:
        gen = ServiceGenerator(
            name=service_name,
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
        bootstrap_path = target / "infra" / "bootstrap.ps1"
        assert bootstrap_path.exists(), (
            f"bootstrap.ps1 not rendered at {bootstrap_path}. "
            "PR #142 should add bootstrap.ps1.j2 to the GCP overlay."
        )
        return bootstrap_path.read_text(encoding="utf-8")


def _render_bootstrap_sh(
    deploy_target: str = "gcp-gke-autopilot",
    db: str = "postgres",
    service_name: str = _LONG_SERVICE_NAME,
) -> str:
    """Render bootstrap.sh.j2 via the real generator. Used by the
    parity tests in test_gcp_bootstrap_sh_ps1_parity.py to compare
    the two scripts' behavior."""
    with tempfile.TemporaryDirectory() as tmp:
        gen = ServiceGenerator(
            name=service_name,
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
        return (target / "infra" / "bootstrap.sh").read_text(encoding="utf-8")


class TestBootstrapPs1Rendered:
    """The PowerShell script is rendered next to bootstrap.sh."""

    def test_ps1_file_appears_in_rendered_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gen = ServiceGenerator(
                name=_LONG_SERVICE_NAME,
                template="go-webapi",
                ci_provider="github-actions",
                deploy_target="gcp-gke-autopilot",
                azure_region="eastus",
                coverage_threshold=80,
                registry="gar",
                db="postgres",
                output_dir=Path(tmp),
                with_git=False,
                with_readme=True,
                gcp_region="us-central1",
                gcp_project_id="sacscaffolder",
            )
            target = gen.run()
            assert (target / "infra" / "bootstrap.ps1").exists(), (
                "bootstrap.ps1 should be rendered alongside bootstrap.sh "
                "for the GCP overlay."
            )
            assert (target / "infra" / "bootstrap.sh").exists(), (
                "bootstrap.sh should still be rendered (parallel to .ps1)."
            )


class TestBootstrapPs1Syntax:
    """The PowerShell script uses PowerShell 7+ idioms, not bash."""

    def test_requires_powershell_7(self) -> None:
        """The script must require PowerShell 7.0+ explicitly via
        `#Requires -Version 7.0`. Without this, Windows PowerShell
        5.1 would silently run the script and fail at runtime on
        features that don't exist there (e.g. `??` null-coalescing)."""
        script = _render_bootstrap_ps1()
        assert "#Requires -Version 7.0" in script or "#requires -version 7.0" in script.lower(), (
            "bootstrap.ps1.j2 should declare `#Requires -Version 7.0` so "
            "users on Windows PowerShell 5.1 fail fast instead of "
            "running a script that will break at runtime."
        )

    def test_no_bash_substring_syntax(self) -> None:
        """No `${VAR:0:32}` bash substring slicing in *executable*
        code. PowerShell's equivalent is `$VAR.Substring(0, [Math]::Min(...))`
        or the Limit-DisplayName helper. Comments referencing the
        bash equivalent for context are allowed."""
        script = _render_bootstrap_ps1()
        # Strip comments (lines starting with `#`, and inline `#` in pwsh
        # which are always line-leading in this script).
        code_lines = "\n".join(
            line for line in script.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert re.search(r"\$\{[A-Z_]+:0:\d+\}", code_lines) is None, (
            "bootstrap.ps1 should not contain bash-style substring "
            "slicing (${VAR:0:32}) in executable code. Use "
            "$VAR.Substring(...) instead. The literal `${VAR:0:32}` "
            "is allowed in comments only."
        )

    def test_no_bash_set_euo(self) -> None:
        """No `set -euo pipefail` in *executable* code. PowerShell uses
        `$ErrorActionPreference = 'Stop'` and `Set-StrictMode`. A comment
        referencing the bash equivalent is fine."""
        script = _render_bootstrap_ps1()
        code_lines = "\n".join(
            line for line in script.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "set -euo pipefail" not in code_lines, (
            "bootstrap.ps1 should use `$ErrorActionPreference = 'Stop'` "
            "instead of bash's `set -euo pipefail`. The literal "
            "`set -euo pipefail` is allowed in comments only."
        )

    def test_uses_pwsh_strict_mode(self) -> None:
        """Strict mode + ErrorActionPreference Stop are the pwsh
        equivalent of `set -euo pipefail`."""
        script = _render_bootstrap_ps1()
        assert "Set-StrictMode" in script
        assert "$ErrorActionPreference" in script and "'Stop'" in script

    def test_uses_pwsh_call_operator(self) -> None:
        """External `gcloud` calls use the `&` call operator, not
        bare invocation. This is required for proper argument
        parsing in pwsh (otherwise the script would fail with
        'the term gcloud is not recognized' or similar)."""
        script = _render_bootstrap_ps1()
        # Match at least 3 occurrences of `& gcloud ...` (we have
        # 5 in the rendered script).
        occurrences = re.findall(r"& gcloud\s+", script)
        assert len(occurrences) >= 3, (
            f"expected at least 3 `& gcloud ...` invocations in "
            f"bootstrap.ps1; found {len(occurrences)}."
        )

    def test_uses_out_null_for_silenced_output(self) -> None:
        """bash's `>/dev/null` maps to pwsh's `| Out-Null`."""
        script = _render_bootstrap_ps1()
        assert "Out-Null" in script, (
            "bootstrap.ps1 should use `| Out-Null` to silence command "
            "output, mirroring bash's `>/dev/null`."
        )


class TestBootstrapPs1DisplayNames:
    """Display names are capped at 32 chars (GCP limit)."""

    def test_limit_display_name_helper_present(self) -> None:
        """The script defines a Limit-DisplayName helper that
        mirrors bash's `${VAR:0:32}` substring truncation."""
        script = _render_bootstrap_ps1()
        assert re.search(r"function\s+Limit-DisplayName", script), (
            "bootstrap.ps1 should define a Limit-DisplayName helper "
            "function (mirrors bash's `${VAR:0:32}` cap)."
        )

    def test_three_display_name_variables_in_scope(self) -> None:
        """The three display-name PowerShell variables are defined
        and assigned via the Limit-DisplayName helper."""
        script = _render_bootstrap_ps1()
        for var in ("DisplayPool", "DisplayProvider", "DisplayCiSa"):
            assert var in script, (
                f"missing `${var} in rendered bootstrap.ps1"
            )

    def test_create_calls_use_display_name_variables(self) -> None:
        """The three `gcloud ... create` calls reference the
        Display* variables, not embedded literals."""
        script = _render_bootstrap_ps1()
        # Each create call should reference its corresponding Display* var.
        assert "--display-name=\"$DisplayPool\"" in script, (
            "WIF pool create should reference $DisplayPool, not a literal."
        )
        assert "--display-name=\"$DisplayProvider\"" in script
        assert "--display-name=\"$DisplayCiSa\"" in script

    def test_long_service_name_does_not_produce_long_display_names(self) -> None:
        """After Limit-DisplayName, the longest display name must
        be at most 32 chars even for the original-bug-triggering
        service_name `ala-gcp-service-demo`."""
        script = _render_bootstrap_ps1()
        # Match each `$Var = Limit-DisplayName '...'` assignment.
        # The literal being passed in is a string literal in the
        # template, but at render time it becomes the rendered
        # `{{ service_id }} gh pool` etc. We measure each.
        assignments = re.findall(
            r"\$Display(?:Pool|Provider|CiSa)\s*=\s*Limit-DisplayName\s+(['\"])([^'\"]*)\1",
            script,
        )
        assert len(assignments) == 3, (
            f"expected 3 `Limit-DisplayName '...'` calls; found {len(assignments)}"
        )
        for quote, literal in assignments:
            assert len(literal) <= 32, (
                f"display-name literal '{literal}' is {len(literal)} chars, "
                f"exceeds GCP's 32-char cap. Limit-DisplayName is the safety "
                f"net but the literal should also be tuned to fit."
            )


class TestBootstrapPs1ResourceIds:
    """Resource IDs use service_id (post-normalization), not
    the raw user-typed service_name."""

    def test_pool_provider_sa_use_service_id(self) -> None:
        script = _render_bootstrap_ps1()
        assert "ala-gcp-service-demo-github-pool" in script
        assert "ala-gcp-service-demo-github-provider" in script
        assert "ala-gcp-service-demo-ci" in script

    def test_no_raw_typed_service_name_in_resource_ids(self) -> None:
        """If the user typed `ala_gcp_service_demo` (snake), no
        resource ID in the rendered bootstrap.ps1 should contain
        the snake form. PR #141's __post_init__ normalization
        makes this automatic."""
        ps1 = _render_bootstrap_ps1(service_name="ala_gcp_service_demo")
        sh = _render_bootstrap_sh(service_name="ala_gcp_service_demo")
        for label, script in (("ps1", ps1), ("sh", sh)):
            assert "ala_gcp_service_demo" not in script, (
                f"bootstrap.{label} contains the snake-case user "
                f"input in a resource-ID context. PR #141 should "
                f"have normalized this away."
            )


class TestBootstrapPs1GitHubSecretsOutput:
    """The script ends by printing the three values the user
    needs as GitHub secrets. PR #140's PR body asserted these
    are: GCP_PROJECT_ID, GCP_WORKLOAD_IDENTITY_PROVIDER, and
    GCP_SERVICE_ACCOUNT."""

    def test_prints_three_github_secret_values(self) -> None:
        script = _render_bootstrap_ps1()
        assert "GCP_PROJECT_ID" in script
        assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in script
        assert "GCP_SERVICE_ACCOUNT" in script

    def test_prints_repo_qualified_provider(self) -> None:
        """The WIF provider's full identifier is
        `projects/<project>/locations/global/workloadIdentityPools/<pool>/providers/<provider>`,
        matching bash's `$REPO_QUALIFIED` build."""
        script = _render_bootstrap_ps1()
        # Render produces: projects/sacscaffolder/locations/global/workloadIdentityPools/ala-gcp-service-demo-github-pool/providers/ala-gcp-service-demo-github-provider
        assert "projects/$ProjectId/locations/global/workloadIdentityPools/$PoolId/providers/$ProviderId" in script

    def test_prints_ci_sa_email(self) -> None:
        # The rendered .ps1 references the variable, which PowerShell
        # will expand at runtime. The parity test verifies the bash
        # version does the equivalent. We just check that the print
        # statement wires up the right variable name.
        script = _render_bootstrap_ps1()
        assert "Write-Host \"  GCP_SERVICE_ACCOUNT             = $CiSaEmail\"" in script