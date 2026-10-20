"""Drift-prevention test for the bootstrap.sh and bootstrap.ps1 parallel.

PR #142 adds a PowerShell 7+ parallel of `bootstrap.sh`. Both scripts
must do the same work in lockstep. If a future fix adds a `gcloud`
call to one script but forgets the other, this test fails.

What's checked:

  - Both scripts invoke the same set of `gcloud` subcommands.
  - Both scripts define the same set of resource-ID variables
    (pool, provider, SA, etc.).
  - Both scripts use display-name variables (capped at 32 chars).
  - Both scripts end by printing the same three GitHub secret
    values (GCP_PROJECT_ID, GCP_WORKLOAD_IDENTITY_PROVIDER,
    GCP_SERVICE_ACCOUNT).

The check is "soft" -- it doesn't diff every byte of the rendered
output. It just verifies the two scripts agree on the structural
elements that matter (which gcloud calls, which IDs, which
outputs).
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402

_SERVICE_NAME = "ala-gcp-service-demo"


def _render(target: str, tmp: Path) -> tuple[str, str]:
    """Render both bootstrap scripts for the given deploy target and
    return (sh, ps1)."""
    gen = ServiceGenerator(
        name=_SERVICE_NAME,
        template="go-webapi",
        ci_provider="github-actions",
        deploy_target=target,
        azure_region="eastus",
        coverage_threshold=80,
        registry="gar",
        db="postgres",
        output_dir=tmp,
        with_git=False,
        with_readme=True,
        gcp_region="us-central1",
        gcp_project_id="sacscaffolder",
    )
    rendered = gen.run()
    return (
        (rendered / "infra" / "bootstrap.sh").read_text(encoding="utf-8"),
        (rendered / "infra" / "bootstrap.ps1").read_text(encoding="utf-8"),
    )


def _gcloud_subcommands(script: str) -> set[str]:
    """Extract the set of `gcloud <subcommand>` invocations from a
    script. We grab everything up to the first newline or shell
    continuation marker; for PowerShell we grab everything up to
    the first line break or pipe.
    """
    # bash-style: lines that start with `gcloud` (with optional
    # backslash continuations). Match the full subcommand chain.
    bash_calls = re.findall(
        r"(?:^|\n)\s*(?:\\\n\s*)?gcloud\s+([a-z][a-z0-9-]+(?:\s+[a-z][a-z0-9-]+)*)",
        script,
    )
    # pwsh-style: `& gcloud ...` lines. The `&` is required in pwsh
    # to call an external command.
    pwsh_calls = re.findall(
        r"(?:^|\n)\s*&\s*gcloud\s+([a-z][a-z0-9-]+(?:\s+[a-z][a-z0-9-]+)*)",
        script,
    )
    # Normalize whitespace and collect.
    out = set()
    for raw in bash_calls + pwsh_calls:
        # Take the first two tokens (e.g. "services enable",
        # "iam workload-identity-pools", "iam workload-identity-pools providers",
        # etc.) for grouping.
        parts = raw.split()
        if len(parts) >= 2:
            out.add(f"{parts[0]} {parts[1]}")
        else:
            out.add(parts[0])
    return out


class TestBootstrapShPs1Parity:
    """The .sh and .ps1 scripts must invoke the same gcloud
    subcommands and reference the same resource ID variables."""

    def test_gcloud_subcommand_sets_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sh, ps1 = _render("gcp-gke-autopilot", Path(tmp))
            sh_set = _gcloud_subcommands(sh)
            ps1_set = _gcloud_subcommands(ps1)
            missing_in_ps1 = sh_set - ps1_set
            missing_in_sh = ps1_set - sh_set
            assert not missing_in_ps1, (
                f"bootstrap.ps1 is missing gcloud subcommands that "
                f"bootstrap.sh has: {sorted(missing_in_ps1)}. "
                f"Either add them to bootstrap.ps1.j2 or update "
                f"this test if the difference is intentional."
            )
            assert not missing_in_sh, (
                f"bootstrap.sh is missing gcloud subcommands that "
                f"bootstrap.ps1 has: {sorted(missing_in_sh)}."
            )

    def test_resource_id_variables_match(self) -> None:
        """Both scripts must define the same set of resource-ID
        variables (POOL_ID/PoolId, PROVIDER_ID/ProviderId, etc.).

        Naming convention differs: bash uses POOL_ID; pwsh uses
        $PoolId. We normalize before comparing by stripping the
        bash-style underscores and lowercasing."""
        with tempfile.TemporaryDirectory() as tmp:
            sh, ps1 = _render("gcp-gke-autopilot", Path(tmp))

            def _sh_vars(text: str) -> set[str]:
                # bash-style: `POOL_ID="..."`
                return set(re.findall(r"^([A-Z][A-Z_0-9]*[A-Z])=", text, re.MULTILINE))

            def _ps1_vars(text: str) -> set[str]:
                # pwsh-style: `$PoolId = ...` or `$PoolId=...`
                return set(re.findall(r"\$([A-Z][a-zA-Z]+)\s*=", text))

            sh_vars = _sh_vars(sh)
            ps1_vars = _ps1_vars(ps1)
            # Normalize: POOL_ID -> poolid, PoolId -> poolid, etc.
            def _norm(v: str) -> str:
                return v.replace("_", "").lower()

            sh_normalized = {_norm(v) for v in sh_vars}
            ps1_normalized = {_norm(v) for v in ps1_vars}

            # The variables each script defines, normalized.
            # Both must include these roles:
            roles = {
                "poolid",
                "providerid",
                "cisaid",
                "projectid",
                "githubrepo",
                "displaypool",
                "displayprovider",
                "displaycisa",
                "repoqualified",
                "cisaemail",
            }
            missing_in_ps1 = roles - ps1_normalized
            missing_in_sh = roles - sh_normalized
            assert not missing_in_ps1, (
                f"bootstrap.ps1 is missing resource-ID variables "
                f"(normalized): {sorted(missing_in_ps1)}"
            )
            assert not missing_in_sh, (
                f"bootstrap.sh is missing resource-ID variables "
                f"(normalized): {sorted(missing_in_sh)}"
            )

    def test_both_print_github_secret_values(self) -> None:
        """Both scripts end by printing the three values the user
        needs as GitHub repo secrets."""
        with tempfile.TemporaryDirectory() as tmp:
            sh, ps1 = _render("gcp-gke-autopilot", Path(tmp))
            for label, text in (("sh", sh), ("ps1", ps1)):
                for secret in (
                    "GCP_PROJECT_ID",
                    "GCP_WORKLOAD_IDENTITY_PROVIDER",
                    "GCP_SERVICE_ACCOUNT",
                ):
                    assert secret in text, (
                        f"bootstrap.{label} should print the {secret} "
                        f"value as a GitHub secret at the end of the script."
                    )

    def test_both_cap_display_names_at_32(self) -> None:
        """Both scripts use a 32-char display-name cap.

        bash uses `${VAR:0:32}` substring syntax; pwsh uses the
        Limit-DisplayName helper which has the same cap.

        We assert that the cap mechanism is referenced in both,
        not that the cap is at exactly 32 chars (the literal may
        be tuned in either script)."""
        with tempfile.TemporaryDirectory() as tmp:
            sh, ps1 = _render("gcp-gke-autopilot", Path(tmp))
            # bash: substring slicing `${DISPLAY_POOL:0:32}` or similar
            assert re.search(r"\$\{DISPLAY_[A-Z_]+:0:32\}", sh) is not None, (
                "bootstrap.sh should cap display names via `${VAR:0:32}`"
            )
            # pwsh: Limit-DisplayName helper
            assert re.search(r"function\s+Limit-DisplayName", ps1), (
                "bootstrap.ps1 should define a Limit-DisplayName helper"
            )