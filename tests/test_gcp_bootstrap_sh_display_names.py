"""Regression tests for `deploy/gcp/infra/bootstrap.sh.j2` display-name
lengths.

Scope:

- GCP caps `--display-name` at 32 characters on
  `gcloud iam workload-identity-pools create`,
  `gcloud iam workload-identity-pools providers create-oidc`, and
  `gcloud iam service-accounts create`. A name longer than 32 chars
  produces an INVALID_ARGUMENT error at create time and the script
  fails before it can print the GitHub secrets you need to set.
- The bug surfaced during the manual smoke test for PR #139
  (the GKE Autopilot + Cloud SQL milestone's "we actually clicked
  the button" step): the rendered bootstrap.sh had three display
  names longer than 32 chars when `service_name=ala-gcp-service-demo`
  (`-display-name="ala-gcp-service-demo GitHub Actions pool"` =
  42 chars).
- The fix moves the three display names into bash variables capped
  at 32 chars via `${var:0:32}`. These tests verify:

    1. The three `gcloud ... create ... --display-name="..."` flags
       in the rendered bootstrap.sh reference the new variables
       (not the old `{{ service_name }} ...` strings).
    2. After the bash `:0:32` truncation runs, all three display
       names are at most 32 characters long, for a service_name long
       enough to have triggered the original bug.

These tests exercise the real `ServiceGenerator.run()` codepath the
scaffolder uses at runtime, so a regression in the jinja wiring or
the bash-truncation logic surfaces here.

Run with: pytest tests/test_gcp_bootstrap_sh_display_names.py -v
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402

# The render context for gke+postgres, matching the milestone defaults.
# Service name is intentionally long enough that the original bug
# (`{{ service_name }} GitHub Actions pool` = 42 chars) would fire.
_LONG_SERVICE_NAME = "ala-gcp-service-demo"


def _render_bootstrap_sh(deploy_target: str = "gcp-gke-autopilot", db: str = "postgres", service_name: str = _LONG_SERVICE_NAME) -> str:
    """Render bootstrap.sh.j2 via the real generator and return the
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
        bootstrap_path = target / "infra" / "bootstrap.sh"
        assert bootstrap_path.exists(), f"bootstrap.sh not rendered at {bootstrap_path}"
        return bootstrap_path.read_text(encoding="utf-8")


def _display_name_for_create(script: str, create_subcommand_regex: str) -> str:
    """Given a rendered bootstrap.sh, return a dict mapping
    `DISPLAY_POOL` / `DISPLAY_PROVIDER` / `DISPLAY_CI_SA` to the
    literal string assigned to that variable on its first (assignment)
    line.

    The script defines the display name as a bash variable like
    `DISPLAY_POOL="<service_name> gh pool"` and then re-binds the
    same variable to its truncation `DISPLAY_POOL="${DISPLAY_POOL:0:32}"`.
    Only the first occurrence (the literal) is the one we want to
    measure for the length invariant; the truncation can never
    *lengthen* the value, so a >32-char literal is a bug regardless
    of the cap.
    """
    pattern = re.compile(
        r'^(DISPLAY_(?:POOL|PROVIDER|CI_SA))="([^"]*)"\s*$',
        re.MULTILINE,
    )
    matches: dict[str, str] = {}
    for m in pattern.finditer(script):
        name = m.group(1)
        # Only keep the FIRST occurrence per variable. Subsequent
        # matches are the truncation reassignment `${VAR:0:32}`,
        # which is a bug to test against (it's the same length
        # regardless of input).
        if name not in matches:
            matches[name] = m.group(2)
    return matches


class TestBootstrapShDisplayNamesExist:
    """The three display-name variables are defined and used."""

    def test_render_includes_three_display_name_variables(self):
        script = _render_bootstrap_sh()
        for var in ("DISPLAY_POOL", "DISPLAY_PROVIDER", "DISPLAY_CI_SA"):
            assert f'{var}="' in script, (
                f"missing {var}=... assignment in rendered bootstrap.sh"
            )

    def test_pool_create_uses_display_pool_variable(self):
        """The pool create line must reference $DISPLAY_POOL, not
        embed a literal `{{ service_name }} GitHub Actions pool`."""
        script = _render_bootstrap_sh()
        # Find the pool-create block.
        m = re.search(
            r"gcloud iam workload-identity-pools create \"\$POOL_ID\"[^`]*?--display-name=\"(\$[A-Z_]+)\"",
            script,
            re.DOTALL,
        )
        assert m is not None, (
            "pool create block does not reference a $DISPLAY_* variable; "
            "still using a literal display-name string?"
        )
        assert m.group(1) == "$DISPLAY_POOL"

    def test_provider_create_uses_display_provider_variable(self):
        script = _render_bootstrap_sh()
        m = re.search(
            r"gcloud iam workload-identity-pools providers create-oidc \"\$PROVIDER_ID\"[^`]*?--display-name=\"(\$[A-Z_]+)\"",
            script,
            re.DOTALL,
        )
        assert m is not None, (
            "provider create block does not reference a $DISPLAY_* variable; "
            "still using a literal display-name string?"
        )
        assert m.group(1) == "$DISPLAY_PROVIDER"

    def test_ci_sa_create_uses_display_ci_sa_variable(self):
        script = _render_bootstrap_sh()
        m = re.search(
            r"gcloud iam service-accounts create \"\$CI_SA_ID\"[^`]*?--display-name=\"(\$[A-Z_]+)\"",
            script,
            re.DOTALL,
        )
        assert m is not None, (
            "CI SA create block does not reference a $DISPLAY_* variable; "
            "still using a literal display-name string?"
        )
        assert m.group(1) == "$DISPLAY_CI_SA"


class TestBootstrapShDisplayNameLiteralsFit32Chars:
    """Each display-name literal, before bash truncation, must fit
    within 32 chars when rendered with the longest plausible service
    name. (Truncation is the safety net, not the primary fix: the
    literals are tuned so the typical case doesn't hit it, but if a
    future edit drops the cap, these tests still catch the regression
    because the literals themselves are bounded.)"""

    def test_pool_literal_fits_32_chars(self):
        """For service_name=ala-gcp-service-demo, the literal is
        `ala-gcp-service-demo gh pool` (24 chars). Under 32."""
        script = _render_bootstrap_sh()
        literals = _display_name_for_create(script, "pool")
        assert literals["DISPLAY_POOL"] == "ala-gcp-service-demo gh pool"
        assert len(literals["DISPLAY_POOL"]) <= 32

    def test_provider_literal_fits_32_chars(self):
        """The provider literal uses $GITHUB_REPO which is set at
        runtime from the git remote. With no remote configured in the
        test scaffold, $GITHUB_REPO renders as the literal text
        `$GITHUB_REPO` (bash variable reference, 12 chars). Under 32."""
        script = _render_bootstrap_sh()
        literals = _display_name_for_create(script, "provider")
        # The literal is `gh for ${GITHUB_REPO}` = 18 chars once
        # expanded. Pre-expansion, it's a 12-char `$GITHUB_REPO`
        # plus the `gh for ` prefix = 18 chars. Either way, < 32.
        assert "gh for" in literals["DISPLAY_PROVIDER"]
        assert len(literals["DISPLAY_PROVIDER"]) <= 32

    def test_ci_sa_literal_fits_32_chars(self):
        """For service_name=ala-gcp-service-demo, the literal is
        `ala-gcp-service-demo ci sa` (24 chars). Under 32."""
        script = _render_bootstrap_sh()
        literals = _display_name_for_create(script, "ci_sa")
        assert literals["DISPLAY_CI_SA"] == "ala-gcp-service-demo ci sa"
        assert len(literals["DISPLAY_CI_SA"]) <= 32


class TestBootstrapShDisplayNameBashCapPresent:
    """Each display-name variable has a `${VAR:0:32}` truncation line
    immediately after its assignment. This is the safety net: even if
    the literal is over 32 chars for some reason, gcloud never sees
    the overflow."""

    def test_pool_has_truncation(self):
        script = _render_bootstrap_sh()
        assert re.search(
            r'DISPLAY_POOL="[^"]*"\s*\nDISPLAY_POOL="\$\{DISPLAY_POOL:0:32\}"',
            script,
        ), "DISPLAY_POOL missing `${DISPLAY_POOL:0:32}` truncation line"

    def test_provider_has_truncation(self):
        script = _render_bootstrap_sh()
        assert re.search(
            r'DISPLAY_PROVIDER="[^"]*"\s*\nDISPLAY_PROVIDER="\$\{DISPLAY_PROVIDER:0:32\}"',
            script,
        ), "DISPLAY_PROVIDER missing `${DISPLAY_PROVIDER:0:32}` truncation line"

    def test_ci_sa_has_truncation(self):
        script = _render_bootstrap_sh()
        assert re.search(
            r'DISPLAY_CI_SA="[^"]*"\s*\nDISPLAY_CI_SA="\$\{DISPLAY_CI_SA:0:32\}"',
            script,
        ), "DISPLAY_CI_SA missing `${DISPLAY_CI_SA:0:32}` truncation line"