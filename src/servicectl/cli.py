"""CLI entry point for servicectl.

Usage:
    servicectl init <name> --template=<template> [flags]
    servicectl init --from-config=<path.json> [overrides]

Run `servicectl init --help` for the full flag list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import questionary
from questionary import Choice
from rich.console import Console
from rich.panel import Panel

from . import __version__
from . import github_api
from .doctor import (

    EXIT_ERROR,
    EXIT_OK,
    EXIT_WARN,
    render_json,
    render_text,
    run_checks,
)
from .generator import ServiceGenerator, ScaffoldError
from .modifier import ModifierError, ServiceModifier
from .replay import (
    RefreshConflict,
    RefreshConfigMissing,
    RefreshError,
    RefreshNotScaffoldedError,
)
from .sac_config import SacConfig, write_config
from .templates import list_templates, TEMPLATES as _TEMPLATES

# Force UTF-8 so Rich's Unicode glyphs (✓, →) don't choke on Windows cp1252 consoles.
import os as _os
import sys as _sys
import io as _io

if _sys.platform == "win32":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        _sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        # Python < 3.7 or already-detached stream — fall back to a wrapped buffer.
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8")
        _sys.stderr = _io.TextIOWrapper(_sys.stderr.buffer, encoding="utf-8")

    # Detect VT (virtual terminal) support. Windows Terminal sets WT_SESSION;
    # legacy conhost does not. When VT is OFF, the conhost prints Rich's
    # ANSI escape sequences as literal text (you see "[1m" instead of bold),
    # so we tell Rich to use the Win32 console API instead via legacy_windows.
    _HAS_VT = bool(
        _os.environ.get("WT_SESSION")
        or "xterm" in _os.environ.get("TERM", "").lower()
        or "256color" in _os.environ.get("TERM", "").lower()
    )
else:
    _HAS_VT = True

# On a legacy conhost (no VT support), `safe_box=True` (the Rich default)
# silently substitutes `box.ROUNDED` with HEAVY corners because it can't
# prove the encoding handles the Unicode box-drawing chars. We know our
# stream is UTF-8 (reconfigured above), so opt out of the substitution
# and let the round corners through.
console = Console(legacy_windows=not _HAS_VT, safe_box=False)
err_console = Console(
    stderr=True, style="bold red", legacy_windows=not _HAS_VT, safe_box=False,
)


def _safe_ask(questionary_fn, *args, **kwargs):
    """Run a questionary prompt with our abort handling.

    questionary's `.ask()` returns ``None`` on Ctrl-C / EOF / abort.
    We translate that into the same "aborted." + ``sys.exit(1)``
    pattern the rest of the CLI uses, so callers don't have to
    special-case ``None``.

    For ``questionary.select`` the result is the chosen ``Choice``
    value (or the raw string if you passed plain strings). For
    ``questionary.text`` it's the typed string. For
    ``questionary.confirm`` it's ``True`` / ``False``. The caller
    picks the right questionary function for the prompt shape and
    gets the right return type out.

    We pass ``kbi_msg=""`` to ``.ask()`` to suppress questionary's
    default ``"\nCancelled by user\n"`` message. Without this the
    user would see two abort messages on Ctrl-C (questionary's and
    ours); with it, the single ``aborted.`` line is the canonical
    one across the whole CLI.
    """
    try:
        result = questionary_fn(*args, **kwargs).ask(kbi_msg="")
    except (KeyboardInterrupt, EOFError):
        result = None
    if result is None:
        err_console.print("\n[yellow]aborted.[/yellow]")
        sys.exit(1)
    return result


def _write_askpass(token: str) -> str:
    """Write a temporary GIT_ASKPASS script that echoes the token.

    Git invokes GIT_ASKPASS with one of three arguments and expects
    the password on stdout. We don't care which argument git passes;
    we always return the token.

    The caller is responsible for deleting the file after the push
    (the script contents the token in plaintext). The file is
    created with mode 0700 on POSIX (executable by owner only);
    on Windows the OS-level mode semantics are looser but the
    parent directory is the user's temp dir, which is per-user.

    Note: 0700 is *required*, not 0600. Git invokes the askpass via
    `execve()`, which requires the execute bit. Setting 0600
    (rw only) results in `fatal: cannot exec <path>: Permission
    denied` on Linux CI runners (GitHub Actions Ubuntu 24.04).
    That bug broke every `simple_and_clean-updated` integration
    test for ~3 hours on 2026-10-12 before being caught.
    """
    import tempfile
    fd, path = tempfile.mkstemp(prefix="servicectl-askpass-", suffix=".sh")
    try:
        os.write(fd, f"#!/bin/sh\necho {token}\n".encode("utf-8"))
    finally:
        os.close(fd)
    # 0700 = rwx------. Owner can read, write, AND execute; nobody
    # else can do anything. On POSIX this is the standard mode for
    # a private executable. On Windows, `os.chmod` only really
    # honors the read-only flag (it doesn't model the execute bit
    # the way POSIX does), so the chmod is a no-op for the
    # Windows permission model, but git's Windows code path uses
    # `CreateProcess` with the file extension (`.sh`) and the
    # shebang line, not `execve`, so it doesn't need the execute
    # bit anyway.
    os.chmod(path, 0o700)
    return path


@click.group()
@click.version_option(__version__, prog_name="servicectl")
def main() -> None:
    """servicectl — self-service DevOps scaffolding."""


# Choices lists used by --deploy and other Click options are also used
# by the interactive prompts below. Keeping them here (instead of inlining
# in cli.py's @click.option decorators) lets the prompts stay in sync
# with the CLI surface — if a new deploy target is added, both the flag
# and the prompt pick it up.
_DEPLOY_TARGET_CHOICES = ["local", "azure", "gcp-cloud-run", "gcp-gke-autopilot"]
_REGISTRY_CHOICES = ["dockerhub", "ghcr", "ecr", "acr", "gcr", "gar"]
_DB_CHOICES = ["postgres", "mysql", "mssql", "cosmosdb"]


def _stdin_is_tty() -> bool:
    """Return True if stdin is a TTY. Wrapped for testability.

    `Click.testing.CliRunner` replaces `sys.stdin` with a non-TTY
    `TextIOWrapper`, so a test that wants to exercise the interactive
    prompt path can patch this function to return True without
    wrestling with the runner's stdin substitution.
    """
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        # Mock or detached stream — treat as non-TTY.
        return False


def _prompt_missing_init_values(
    *,
    name: str | None,
    template: str | None,
    deploy_target: str,
    azure_region: str,
    gcp_region: str,
    gcp_project_id: str | None,
    registry: str,
    db: str,
    repo: str | None,
    repo_private: bool,
) -> tuple[str, str, str, str, str, str | None, str, str, str | None, bool]:
    """Prompt for any missing 'interesting' init values when running interactively.

    Called from `init` when `stdin` is a TTY and `--no-input` wasn't
    passed. Prompts in a fixed order (most general → most specific) and
    uses the deployed-target choice to gate follow-up region / project
    prompts.

    Returns the (possibly-updated) values. A value is never re-prompted
    if the caller already supplied it (CLI flag or --from-config), so
    the merge order is preserved: prompts are the *last* source of
    truth, not the first.

    Values we never prompt for (they keep their built-in defaults):
      - ci_provider           (default: github-actions)
      - coverage_threshold    (default: 80%)
      - output_dir            (default: .)
      - in_place, no_git, no_readme, git_remote (booleans or rare)
      - from_config, strict_config (developer-facing flags)
      - repo_no_push          (CI-only flag, never prompted)

    On EOF / Ctrl-C / Ctrl-D during a prompt, the function exits with
    code 1 and a friendly message — it does NOT raise, so the test
    suite can call this in a CliRunner and get a clean exit code.
    """
    from .templates import TEMPLATES as _TEMPLATES  # local import; click decorator runs at import time

    # 1. NAME (always prompt if missing — required, no good default)
    if not name:
        console.print(
            Panel(
                "[bold]Welcome to servicectl![/bold]\n\n"
                "Let's scaffold a new service. I'll ask a few questions; "
                "for the multiple-choice ones, use ↑/↓ to pick and Enter "
                "to confirm. For the typed ones, the default is shown in "
                "[cyan][brackets][/cyan] — press Enter to accept.",
                border_style="cyan",
            )
        )
        while not name:
            name = _safe_ask(
                questionary.text, "Service name: ",
            ).strip()
            if not name:
                console.print("[yellow]name cannot be empty[/yellow]")

    # 2. TEMPLATE (always prompt if missing — required, choice)
    if not template:
        template_choices = [
            Choice(tid, description=desc) for tid, desc in _TEMPLATES.items()
        ]
        template = _safe_ask(
            questionary.select,
            "Template — which service framework?",
            choices=template_choices,
        )

    # 3. DEPLOY TARGET (default local — prompt to confirm/change)
    # questionary.select with a `default` that matches a Choice value
    # pre-selects that choice. The user can still change it with arrows.
    deploy_labels = {
        "local": "no remote cloud; just Dockerfile + docker-compose",
        "azure": "Azure App Service (Linux) + Container Registry",
        "gcp-cloud-run": "Google Cloud Run (serverless)",
        "gcp-gke-autopilot": "GKE Autopilot (managed k8s)",
    }
    deploy_target_choices = [
        Choice(d, description=deploy_labels.get(d, ""))
        for d in _DEPLOY_TARGET_CHOICES
    ]
    deploy_target = _safe_ask(
        questionary.select,
        "Deployment target: ",
        choices=deploy_target_choices,
        default=deploy_target,
    )

    # 4. AZURE / GCP-REGION / GCP-PROJECT-ID (only if relevant deploy)
    if deploy_target == "azure":
        azure_region = _safe_ask(
            questionary.text, "Azure region: ", default=azure_region,
        ).strip() or azure_region
    elif deploy_target.startswith("gcp-"):
        gcp_region = _safe_ask(
            questionary.text, "GCP region: ", default=gcp_region,
        ).strip() or gcp_region
        # For gcp_project_id, allow Enter to keep the current value
        # but warn if it's empty (migrations from non-GCP deploys won't
        # have one set).
        prompt_default = gcp_project_id or ""
        value = _safe_ask(
            questionary.text,
            f"GCP project ID (required for --deploy={deploy_target}): ",
            default=prompt_default,
        ).strip()
        if not value:
            console.print(
                f"[yellow]warning:[/yellow] GCP project ID is empty; the next "
                f"CI run with --deploy={deploy_target} will fail until you "
                f"set it in .servicectl.json (or re-run init)."
            )
        gcp_project_id = value or None

    # 5. REGISTRY (default ghcr — prompt to confirm/change)
    registry_notes = {
        "gar": " (recommended for GCP deploys)",
        "gcr": " (legacy; prefer gar)",
        "ghcr": " (default; works with GitHub Actions)",
    }
    registry_choices = [
        Choice(r, description=registry_notes.get(r, "").strip())
        for r in _REGISTRY_CHOICES
    ]
    registry = _safe_ask(
        questionary.select,
        "Container registry: ",
        choices=registry_choices,
        default=registry,
    )

    # 6. DATABASE (default postgres — prompt to confirm/change)
    db = _safe_ask(
        questionary.select,
        "Database: ",
        choices=[Choice(d) for d in _DB_CHOICES],
        default=db,
    )

    # 7. GITHUB REPO CREATION (yes/no prompt; if yes, ask for owner/name)
    # The user can skip this (it defaults to no), or if they already
    # passed --repo on the command line we don't re-ask.
    if repo is None:
        wants_repo = _safe_ask(
            questionary.confirm,
            "Create a GitHub repo for this service now?",
            default=False,
        )
        if wants_repo:
            while not repo:
                value = _safe_ask(
                    questionary.text, "Repo (format: OWNER/NAME): ",
                ).strip()
                if "/" in value and not value.startswith("/") and not value.endswith("/"):
                    repo = value
                else:
                    console.print("[yellow]must be in OWNER/NAME format (e.g. SaCScaffolder/my-service)[/yellow]")
            repo_private = _safe_ask(
                questionary.confirm, "Make this repo private?", default=False,
            )
        # else repo stays None and we proceed without GitHub repo creation

    return (name, template, deploy_target, azure_region, gcp_region, gcp_project_id, registry, db, repo, repo_private)


def _prompt_refresh_overrides(
    *,
    config: SacConfig,
) -> SacConfig:
    """Walk the user through the deploy-impacting fields of a refresh.

    Each field shows its current value (from `.servicectl.json`) as the
    default. Press Enter to keep; type a new value to change. This is
    the "I came here to flip one field" UX — most prompts just hit
    Enter. The common case is something like ``servicectl refresh``
    after a deploy-target change: you want to keep 6 of 7 fields the
    same and update one.

    Fields that don't affect deploy (``ci_provider``,
    ``coverage_threshold``, ``service_name``) are intentionally NOT
    prompted; the refresh preserves them as-is.

      - ``service_name`` is excluded because changing it would rename
        the package and invalidate every import path inside the
        service. That's a destructive operation best done via
        ``servicectl init`` in a fresh directory, not via refresh.
      - ``ci_provider`` and ``coverage_threshold`` are preserved
        because they describe the *how* of testing, not the *what* of
        deployment, and refreshing shouldn't silently toggle them.

    Returns a ``SacConfig`` with the (possibly) updated fields. The
    caller is responsible for telling the user what changed. The
    caller does NOT write the new config back to disk; ``refresh``
    threads the result into ``replay_refresh()`` via the new
    ``config_override`` parameter, so the source repo's working
    tree stays pristine until the user accepts the post-refresh
    merge prompt and the worktree branch is fast-forwarded into
    the source branch.

    GCP exposure note: when the user picks a ``gcp-*`` deploy target
    we re-prompt for ``gcp_region`` and ``gcp_project_id`` (with the
    current value as the default). When they pick ``gar`` for the
    registry, we print the resolved GAR image path so the user can see
    where the next CI run will publish. This is the "I just learned
    this" detail that makes the prompt actually useful during a
    GCP migration.
    """
    from .templates import TEMPLATES as _TEMPLATES  # local import; see _prompt_missing_init_values

    # 1. TEMPLATE (default = current value; choice of TEMPLATES)
    # questionary.select shows the current value pre-highlighted via
    # the `default` kwarg (matched against Choice values).
    template_choices = [
        Choice(tid, description=desc) for tid, desc in _TEMPLATES.items()
    ]
    template = _safe_ask(
        questionary.select,
        "Template — which service framework?",
        choices=template_choices,
        default=config.template,
    )

    # 2. DEPLOY TARGET (default = current value)
    deploy_labels = {
        "local": "no remote cloud; just Dockerfile + docker-compose",
        "azure": "Azure App Service (Linux) + Container Registry",
        "gcp-cloud-run": "Google Cloud Run (serverless)",
        "gcp-gke-autopilot": "GKE Autopilot (managed k8s)",
    }
    deploy_target_choices = [
        Choice(d, description=deploy_labels.get(d, ""))
        for d in _DEPLOY_TARGET_CHOICES
    ]
    deploy_target = _safe_ask(
        questionary.select,
        "Deployment target: ",
        choices=deploy_target_choices,
        default=config.deploy_target,
    )

    # 3. REGION / PROJECT — only prompt for the ones the new target uses.
    # If the user changed deploy_target from azure to gcp-cloud-run we
    # stop prompting for azure_region and start prompting for
    # gcp_region + gcp_project_id. This mirrors init's behavior and is
    # the most intuitive flow during a migration.
    azure_region = config.azure_region
    gcp_region = config.gcp_region
    gcp_project_id = config.gcp_project_id
    if deploy_target == "azure":
        value = _safe_ask(
            questionary.text, "Azure region: ", default=azure_region,
        ).strip()
        azure_region = value or azure_region
    elif deploy_target.startswith("gcp-"):
        value = _safe_ask(
            questionary.text, "GCP region: ", default=gcp_region,
        ).strip()
        gcp_region = value or gcp_region
        # For gcp_project_id, allow Enter to keep the current value
        # but warn if it's empty (migrations from non-GCP deploys won't
        # have one set).
        prompt_default = gcp_project_id or ""
        value = _safe_ask(
            questionary.text,
            f"GCP project ID (required for --deploy={deploy_target}): ",
            default=prompt_default,
        ).strip()
        if not value:
            console.print(
                f"[yellow]warning:[/yellow] GCP project ID is empty; the next "
                f"CI run with --deploy={deploy_target} will fail until you "
                f"set it in .servicectl.json (or re-run refresh with --no-input "
                f"after editing the file)."
            )
        gcp_project_id = value or None

    # 4. REGISTRY (default = current value; print GAR image path if gar)
    registry_notes = {
        "gar": "recommended for GCP deploys",
        "gcr": "legacy; prefer gar",
        "ghcr": "default; works with GitHub Actions",
    }
    registry_choices = [
        Choice(r, description=registry_notes.get(r, ""))
        for r in _REGISTRY_CHOICES
    ]
    registry = _safe_ask(
        questionary.select,
        "Container registry: ",
        choices=registry_choices,
        default=config.registry,
    )

    # 4a. GCP exposure: if the user picked gar, show the resolved
    # image path so they can see where the next CI run will publish.
    # This is the kind of "I just learned this" detail that's worth a
    # one-liner; without it the user picks gar without understanding
    # the path change.
    if registry == "gar" and gcp_project_id:
        image = config.service_name or "<service-name>"
        console.print(
            f"  [dim]GAR image path:[/dim] [cyan]{gcp_region}-docker.pkg.dev/"
            f"{gcp_project_id}/{config.service_name or '<repo>'}/{image}:${{SHA}}[/cyan]"
        )
    elif registry == "gar":
        console.print(
            f"  [dim]GAR image path:[/dim] [yellow]cannot resolve -- gcp_project_id is empty[/yellow]"
        )

    # 5. DATABASE (default = current value)
    db = _safe_ask(
        questionary.select,
        "Database: ",
        choices=[Choice(d) for d in _DB_CHOICES],
        default=config.db,
    )

    # Build a new SacConfig preserving the non-prompted fields. We
    # use the dataclass replace() so unknown fields (sac_version,
    # sac_base_commit) are carried over verbatim.
    import dataclasses
    return dataclasses.replace(
        config,
        template=template,
        deploy_target=deploy_target,
        azure_region=azure_region,
        gcp_region=gcp_region,
        gcp_project_id=gcp_project_id,
        registry=registry,
        db=db,
    )


def _summarize_refresh_changes(
    before: SacConfig,
    after: SacConfig,
) -> list[str]:
    """Return a list of human-readable change descriptions for the refresh prompt.

    Only fields actually affected by the prompt (template,
    deploy_target, azure_region, gcp_region, gcp_project_id, registry,
    db) are compared. Returns [] if nothing changed.
    """
    changes: list[str] = []
    fields = [
        ("template", "template"),
        ("deploy_target", "deploy target"),
        ("azure_region", "Azure region"),
        ("gcp_region", "GCP region"),
        ("gcp_project_id", "GCP project ID"),
        ("registry", "registry"),
        ("db", "database"),
    ]
    for attr, label in fields:
        old = getattr(before, attr)
        new = getattr(after, attr)
        if old != new:
            changes.append(f"  {label}: [yellow]{old}[/yellow] → [green]{new}[/green]")
    return changes


@main.command()
@click.argument(
    "name",
    required=False,  # Optional when --from-config provides it.
)
@click.option(
    "--template",
    "template",
    required=False,  # Required when --from-config is not used; checked after merge.
    type=click.Choice(list_templates(), case_sensitive=False),
    help="Service template to scaffold.",
)
@click.option(
    "--ci",
    "ci_provider",
    type=click.Choice(["github-actions", "azure-devops"], case_sensitive=False),
    default="github-actions",
    show_default=True,
    help="CI provider.",
)
@click.option(
    "--deploy",
    "deploy_target",
    # NOTE: `azure-container-apps` is on the roadmap but not yet implemented.
    # Until the overlay exists, leaving the option would produce a broken
    # App Service scaffold that calls `az webapp restart` against a
    # non-existent Container App. Re-add when the full overlay ships.
    #
    # GCP deploy targets added 2026-10-03:
    #   - gcp-cloud-run     : Cloud Run service (serverless container parity with App Service)
    #   - gcp-gke-autopilot : GKE Autopilot cluster (Kubernetes parity, no node management)
    type=click.Choice(
        ["local", "azure", "gcp-cloud-run", "gcp-gke-autopilot"],
        case_sensitive=False,
    ),
    default="local",
    show_default=True,
    help="Deployment target.",
)
@click.option(
    "--azure-region",
    "azure_region",
    type=str,
    default="eastus",
    show_default=True,
    help="Azure region for deploy targets that need one.",
)
@click.option(
    "--gcp-region",
    "gcp_region",
    type=str,
    default="us-central1",
    show_default=True,
    help="GCP region for deploy targets that need one.",
)
@click.option(
    "--gcp-project-id",
    "gcp_project_id",
    type=str,
    default=None,
    help="GCP project ID (required for --deploy=gcp-*). Rendered into the Terraform variables file.",
)
@click.option(
    "--coverage",
    "coverage_threshold",
    type=click.IntRange(min=0, max=100),
    default=80,
    show_default=True,
    help="Minimum test coverage threshold (percent).",
)
@click.option(
    "--registry",
    "registry",
    type=click.Choice(
        ["dockerhub", "ghcr", "ecr", "acr", "gcr", "gar"],
        case_sensitive=False,
    ),
    default="ghcr",
    show_default=True,
    help="Container registry. `gar` = Google Artifact Registry (recommended for GCP deploys); `gcr` is the legacy Container Registry.",
)
@click.option(
    "--db",
    "db",
    type=click.Choice(["postgres", "mysql", "mssql", "cosmosdb"], case_sensitive=False),
    default="postgres",
    show_default=True,
    help="Database backend (postgres = PostgreSQL Flexible Server, mysql = Azure Database for MySQL Flexible Server, mssql = Azure SQL Database, cosmosdb = Azure Cosmos DB).",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Directory in which to create the service folder.",
)
@click.option("--no-git", "no_git", is_flag=True, help="Skip `git init` after scaffolding.")
@click.option("--no-readme", "no_readme", is_flag=True, help="Skip README generation (not recommended).")
@click.option("--git-remote", "git_remote", type=str, default=None, help="HTTPS URL of a git remote to add as `origin` and push the initial commit to. Requires --no-git NOT to be set. Must start with https://.")
@click.option("--repo", "repo", type=str, default=None, help="Create the GitHub repo as part of init. Format: OWNER/NAME. After the local scaffold and initial commit, init POSTs to GitHub to create the empty repo, then adds it as `origin` and pushes. Mutually exclusive with --git-remote.")
@click.option("--repo-private", "repo_private", is_flag=True, help="When used with --repo, create the GitHub repo as private. Default is public.")
@click.option("--repo-token", "repo_token", type=str, default=None, help="GitHub token to use for the --repo creation. Falls back to $GH_TOKEN, then `gh auth token`. The token is never written to disk by this command.")
@click.option("--repo-no-push", "repo_no_push", is_flag=True, help="When used with --repo, create the GitHub repo and add it as `origin`, but do NOT push the initial commit. The caller is expected to add more commits (e.g. cherry-picked from a fixture) and push manually. This is the integration-test workflow path where the SAC baseline is one of several commits applied to the repo.")
@click.option("--in-place", "in_place", is_flag=True, help="Scaffold directly into --output-dir instead of creating a <name> subfolder. Refuses to overwrite a non-empty directory.")
@click.option(
    "--from-config",
    "from_config",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Load init flags from a JSON file. JSON keys are the same as the CLI flag names "
         "(e.g. 'template', 'ci_provider', 'deploy_target'). CLI flags override JSON values; "
         "JSON values override built-in defaults. Schema: a flat JSON object. Use "
         "--strict-config to error on unknown keys.",
)
@click.option(
    "--strict-config",
    "strict_config",
    is_flag=True,
    help="With --from-config, error on unknown JSON keys instead of silently dropping them.",
)
@click.option(
    "--no-input",
    "no_input",
    is_flag=True,
    help=(
        "Disable interactive prompts. By default, `servicectl init` will "
        "prompt for any missing required values when stdin is a TTY, so "
        "you can run `servicectl init` with no arguments and walk through "
        "the choices. Pass --no-input (e.g. in CI or scripts) to skip "
        "prompts and fail with a clear error instead if a required value "
        "(name, template, --gcp-project-id for gcp- deploys) is missing. "
        "Honored values supplied via CLI flags or --from-config are never "
        "re-prompted."
    ),
)
def init(
    name: str,
    template: str,
    ci_provider: str,
    deploy_target: str,
    azure_region: str,
    gcp_region: str,
    gcp_project_id: str | None,
    coverage_threshold: int,
    registry: str,
    db: str,
    output_dir: Path,
    in_place: bool,
    no_git: bool,
    no_readme: bool,
    git_remote: str | None,
    repo: str | None,
    repo_private: bool,
    repo_token: str | None,
    repo_no_push: bool,
    from_config: Path | None,
    strict_config: bool,
    no_input: bool,
) -> None:
    """Scaffold a new service named NAME."""
    # Early validation that doesn't depend on --from-config merging. The
    # flag-vs-config merge happens below; the mutually-exclusive and
    # required-arg checks have to run before that merge so a bad
    # combo is rejected before the user waits for a config parse.
    if repo and git_remote:
        err_console.print(
            "[bold red]error:[/bold red] --repo and --git-remote are mutually "
            "exclusive. --repo creates the GitHub repo and pushes to it; "
            "--git-remote pushes to a repo you've already created. "
            "Pick one."
        )
        sys.exit(2)
    if repo and no_git:
        err_console.print(
            "[bold red]error:[/bold red] --repo requires git (it creates the "
            "repo, adds it as origin, and pushes). Don't pass --no-git with --repo."
        )
        sys.exit(2)
    if repo_private and not repo:
        err_console.print(
            "[bold red]error:[/bold red] --repo-private is only meaningful with "
            "--repo. It controls the visibility of the GitHub repo being created."
        )
        sys.exit(2)
    if repo_no_push and not repo:
        err_console.print(
            "[bold red]error:[/bold red] --repo-no-push is only meaningful with "
            "--repo. It suppresses the final `git push -u origin main`, which "
            "is the only push that --repo does."
        )
        sys.exit(2)

    # Merge --from-config JSON values with CLI flags.
    # Precedence (highest first): CLI flag (explicit) > JSON value > CLI default.
    # We use Click's parameter_source API to know which CLI flags were
    # passed explicitly by the user vs left at their default.
    if from_config is not None:
        try:
            with from_config.open(encoding="utf-8") as f:
                file_cfg = json.load(f)
        except json.JSONDecodeError as e:
            err_console.print(
                f"[bold red]error:[/bold red] --from-config {from_config} is not valid JSON: {e}"
            )
            sys.exit(2)
        except OSError as e:
            err_console.print(
                f"[bold red]error:[/bold red] could not read --from-config {from_config}: {e}"
            )
            sys.exit(2)

        if not isinstance(file_cfg, dict):
            err_console.print(
                f"[bold red]error:[/bold red] --from-config {from_config} must contain a JSON object, "
                f"got {type(file_cfg).__name__}"
            )
            sys.exit(2)

        # Detect v2 spec shape and translate to v1 flat keys before the
        # rest of the merge logic runs. v2 specs nest related fields
        # under `service` / `ci` / `deploy` objects; the v1 path below
        # only understands top-level keys (it would silently drop the
        # nested object and default `deploy_target` to "local").
        #
        # Detection rule (two-tier, both win):
        #   (a) An explicit `schema_version: 2` opts in.
        #   (b) Absent that, ANY v2-only top-level key (`service`, `ci`,
        #       `deploy`, `overlays`) implies v2. This catches specs that
        #       are structurally v2 but lack the `schema_version` field
        #       -- which is the common case for hand-written specs and
        #       for any spec produced before #95 was merged. The previous
        #       rule (schema_version only) silently dropped these.
        #   (c) Old v1 flat specs (no `schema_version`, none of the
        #       v2-only keys) still fall through to the v1 path.
        #
        # Either signal wins; both routes use the same translator.
        _V2_ONLY_TOP_LEVEL_KEYS = frozenset({"service", "ci", "deploy", "overlays"})
        is_v2_spec = (
            file_cfg.get("schema_version") == 2
            or bool(set(file_cfg.keys()) & _V2_ONLY_TOP_LEVEL_KEYS)
        )
        if is_v2_spec:
            try:
                translated = SacConfig.from_v2_dict(file_cfg)
            except KeyError as e:
                err_console.print(
                    f"[bold red]error:[/bold red] --from-config {from_config} is a v2 spec "
                    f"but is invalid: {e}"
                )
                sys.exit(2)
            # Project the SacConfig back onto the v1 flat shape the
            # rest of this function expects. Drops `schema_version` and
            # other bookkeeping fields that v1 has no slot for.
            v1_flat = translated.to_dict()
            v1_flat.pop("schema_version", None)
            v1_flat.pop("sac_version", None)
            v1_flat.pop("sac_base_commit", None)
            # `SacConfig` does not store `name` (it's the directory name,
            # resolved separately). Recover it from the v2 spec's
            # `service.name` so the rest of the merge loop can find it
            # in `file_cfg`.
            v1_flat["name"] = file_cfg.get("service", {}).get("name")
            file_cfg = v1_flat

        # Map of CLI option name -> ServiceGenerator kwarg name. Must stay
        # in sync with the options above. If you add a new option, add it here too.
        cli_to_kwarg = {
            "name": "name",
            "template": "template",
            "ci_provider": "ci_provider",
            "deploy_target": "deploy_target",
            "azure_region": "azure_region",
            "gcp_region": "gcp_region",
            "gcp_project_id": "gcp_project_id",
            "coverage_threshold": "coverage_threshold",
            "registry": "registry",
            "db": "db",
            "output_dir": "output_dir",
            "in_place": "in_place",
            "git_remote": "git_remote",
        }
        allowed_keys = set(cli_to_kwarg.keys())
        unknown_keys = set(file_cfg.keys()) - allowed_keys
        if unknown_keys:
            if strict_config:
                err_console.print(
                    f"[bold red]error:[/bold red] --from-config contains unknown keys: "
                    f"{sorted(unknown_keys)}. Allowed: {sorted(allowed_keys)}."
                )
                sys.exit(2)
            # Non-strict: silently drop. (Keeps the JSON future-proof against
            # new options being added before this CLI catches up.)
            for k in unknown_keys:
                del file_cfg[k]

        # Click tracks which params were passed on the command line vs which
        # took their default. CLI flag wins; otherwise JSON value wins.
        ctx = click.get_current_context()
        for cli_name, kwarg_name in cli_to_kwarg.items():
            if ctx.get_parameter_source(cli_name) != click.core.ParameterSource.DEFAULT:
                # Explicit CLI flag wins; leave ctx.params[cli_name] alone.
                continue
            if kwarg_name in file_cfg:
                ctx.params[cli_name] = file_cfg[kwarg_name]

        # Re-bind locals to the (possibly updated) ctx.params values so the
        # rest of init() sees the merged result. We do this by reading them
        # back out of ctx.params.
        name = ctx.params["name"]
        template = ctx.params["template"]
        ci_provider = ctx.params["ci_provider"]
        deploy_target = ctx.params["deploy_target"]
        azure_region = ctx.params["azure_region"]
        gcp_region = ctx.params["gcp_region"]
        gcp_project_id = ctx.params["gcp_project_id"]
        coverage_threshold = ctx.params["coverage_threshold"]
        registry = ctx.params["registry"]
        db = ctx.params["db"]
        output_dir = ctx.params["output_dir"]
        in_place = ctx.params["in_place"]
        git_remote = ctx.params["git_remote"]
        repo = ctx.params["repo"]
        repo_private = ctx.params["repo_private"]
        repo_token = ctx.params["repo_token"]
        repo_no_push = ctx.params["repo_no_push"]

        # (Mutually-exclusive and required-arg validation moved to the top
        # of init() so it runs even when --from-config isn't used.)

        # After merging JSON + defaults, validate that required keys landed.
        # Click's `required=True` only catches missing CLI flags; it doesn't
        # know whether --from-config supplied the value.
        if not name:
            err_console.print(
                "[bold red]error:[/bold red] 'name' is required. Pass it as the NAME "
                "argument or include it in --from-config JSON."
            )
            sys.exit(2)
        if not template:
            err_console.print(
                "[bold red]error:[/bold red] --template is required (or include it in "
                "--from-config JSON)."
            )
            sys.exit(2)

    # Interactive prompt for any missing "interesting" values when stdin
    # is a TTY and --no-input wasn't passed. This is the local-dev
    # ergonomics path: a human can run `servicectl init` with no
    # arguments and walk through the choices. CI / scripts pass
    # --no-input and get a clean error if a required value is missing.
    #
    # "Interesting" means: the field is required OR has a choice the
    # user is likely to want to make (deploy target, db, registry).
    # Universal defaults (ci_provider, coverage_threshold, output_dir,
    # in_place, no_git, no_readme) are not prompted; they keep their
    # built-in defaults silently. Values supplied via CLI flags or
    # --from-config are never re-prompted.
    if not no_input and _stdin_is_tty() and not os.environ.get("SERVICECTL_NO_INPUT"):
        name, template, deploy_target, azure_region, gcp_region, gcp_project_id, registry, db, repo, repo_private = _prompt_missing_init_values(
            name=name,
            template=template,
            deploy_target=deploy_target,
            azure_region=azure_region,
            gcp_region=gcp_region,
            gcp_project_id=gcp_project_id,
            registry=registry,
            db=db,
            repo=repo,
            repo_private=repo_private,
        )
    else:
        # Non-interactive path: fail with a clear message if a required
        # value is missing. The exceptions are: --gcp-project-id is
        # *always* required for gcp- deploys (no good default), so we
        # check that here regardless of whether the prompt ran.
        if not name:
            err_console.print(
                "[bold red]error:[/bold red] 'name' is required. Pass it as the NAME "
                "argument, include it in --from-config JSON, or run interactively "
                "(without --no-input) to be prompted."
            )
            sys.exit(2)
        if not template:
            err_console.print(
                "[bold red]error:[/bold red] --template is required (or include it in "
                "--from-config JSON), or run interactively (without --no-input) to be "
                "prompted."
            )
            sys.exit(2)
        if deploy_target.startswith("gcp-") and not gcp_project_id:
            err_console.print(
                f"[bold red]error:[/bold red] --gcp-project-id is required when "
                f"--deploy={deploy_target}. Pass it on the command line, in "
                f"--from-config JSON, or run interactively (without --no-input) "
                f"to be prompted."
            )
            sys.exit(2)

    try:
        gen = ServiceGenerator(
            name=name,
            template=template,
            ci_provider=ci_provider,
            deploy_target=deploy_target,
            azure_region=azure_region,
            gcp_region=gcp_region,
            gcp_project_id=gcp_project_id,
            coverage_threshold=coverage_threshold,
            registry=registry,
            db=db,
            output_dir=output_dir,
            with_git=not no_git,
            with_readme=not no_readme,
            in_place=in_place,
            git_remote=git_remote,
        )
        created = gen.run(console=console)
    except ScaffoldError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)

    # If --repo was passed, create the GitHub repo and push to it.
    # This is the "faithful recreation of the user experience" path: a
    # human running `servicectl init --repo=X/Y` does this in one
    # command, and the integration test workflow in
    # SaCScaffolder/sac's `integration-test-modified.yml` bootstrap path
    # also shells out to this command for the same effect.
    created_repo_url: str | None = None
    if repo:
        from . import github_api  # local import; only needed on this path
        owner_str, _, name_str = repo.partition("/")
        if not owner_str or not name_str:
            err_console.print(
                f"[bold red]error:[/bold red] --repo must be OWNER/NAME, got {repo!r}"
            )
            sys.exit(2)
        # Token resolution: --repo-token > $GH_TOKEN > `gh auth token`
        token = repo_token or os.environ.get("GH_TOKEN") or github_api.get_auth_token_from_gh_cli()
        if not token:
            err_console.print(
                "[bold red]error:[/bold red] no GitHub token available for --repo. "
                "Provide one via:\n"
                "  --repo-token <PAT>   (explicit, never stored)\n"
                "  $GH_TOKEN            (env var)\n"
                "  gh auth login        (then re-run; init will pick up the token via `gh auth token`)\n"
                "See https://github.com/settings/tokens to mint a PAT. The token needs `repo` scope for the org."
            )
            sys.exit(2)
        # The scaffolded service lives in `created` (a Path). All git
        # commands run from there.
        try:
            response = github_api.create_org_repo(
                owner_str,
                name_str,
                token=token,
                private=repo_private,
            )
        except github_api.GitHubAPIError as e:
            err_console.print(
                f"[bold red]error:[/bold red] failed to create GitHub repo {repo}: "
                f"HTTP {e.status}\n{e.body}"
            )
            sys.exit(2)
        # GitHub's REST API returns 201 from POST /repos before the
        # git daemon has caught up. Without this wait, the
        # subsequent `git push` can fail with "repository not found"
        # in a small but real fraction of runs (verified: ~10-50%
        # depending on time of day). The integration test workflow
        # had this wait inline; we centralize it here so every
        # caller of `--repo` benefits. Default ceiling: 12 attempts
        # x 5s = 60s.
        if not github_api.wait_for_git_daemon(owner_str, name_str, token=token):
            console.print(
                "  [bold yellow]![/bold yellow] git daemon still not visible after "
                "60s; the push may fail with 'repository not found'."
            )
        created_repo_url = response.get("html_url") or response.get("clone_url")
        clone_url = response.get("clone_url")
        if not clone_url:
            err_console.print(
                f"[bold red]error:[/bold red] GitHub created the repo but didn't return a clone_url. Response: {response}"
            )
            sys.exit(2)
        console.print(f"  [bold green]✓[/bold green] created GitHub repo [cyan]{repo}[/cyan]")
        # Add the newly-created repo as origin and push. Reuse the same
        # git command pattern as the generator's --git-remote path.
        try:
            subprocess.run(
                ["git", "-C", str(created), "remote", "add", "origin", clone_url],
                check=True,
                capture_output=True,
            )
            console.print(f"  ran      [bold]git remote add origin[/bold] {clone_url}")
        except subprocess.CalledProcessError as e:
            err_console.print(
                f"[bold red]error:[/bold red] `git remote add origin` failed: "
                f"{e.stderr.decode(errors='replace')}"
            )
            sys.exit(2)
        try:
            # Authenticate the push. On a local dev machine, the user
            # probably has a credential helper configured. On a CI
            # runner (like SaCScaffolder/sac's integration test
            # workflow), there is no helper and no TTY, so we need
            # an ASKPASS script. We use the token both ways: if
            # GIT_ASKPASS isn't set, we write a temporary one and
            # clean it up after the push. If it is already set
            # (caller-provided), we leave it alone.
            #
            # `git -c credential.helper=` clears any inherited
            # helper so the ASKPASS is what runs.
            #
            # Skip the push entirely if --repo-no-push was passed.
            # This is the integration-test path where the workflow
            # needs to do additional work (e.g. cherry-pick fixture
            # commits) on top of the SAC baseline before pushing.
            if repo_no_push:
                console.print(
                    "  [bold yellow]o[/bold yellow] skipped `git push` (--repo-no-push); "
                    "the remote origin is set, push manually when ready"
                )
            else:
                askpass_path: str | None = None
                if "GIT_ASKPASS" not in os.environ:
                    askpass_path = _write_askpass(token)
                # Order matters: the literal GIT_ASKPASS key (if we
                # just wrote one) overrides anything in os.environ, so
                # our temporary script is the one git sees. The
                # `os.environ` entries are merged in second so user-set
                # vars survive (PATH, LANG, etc.).
                env_for_push = {**os.environ}
                if askpass_path is not None:
                    env_for_push["GIT_ASKPASS"] = askpass_path
                try:
                    subprocess.run(
                        ["git", "-C", str(created), "-c", "credential.helper=",
                         "push", "-u", "origin", "main"],
                        check=True,
                        capture_output=True,
                        env=env_for_push,
                    )
                    console.print("  ran      [bold]git push -u origin main[/bold]")
                finally:
                    if askpass_path is not None:
                        try:
                            os.unlink(askpass_path)
                        except OSError:
                            pass
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors='replace').strip() or e.stdout.decode(errors='replace').strip()
            err_console.print(
                f"[bold red]error:[/bold red] `git push -u origin main` failed: {stderr}"
            )
            sys.exit(2)

    # Build the deploy hint based on the deploy target.
    if deploy_target == "local":
        deploy_hint = "  docker compose -f docker-compose.dev.yml up"
    elif deploy_target == "azure":
        deploy_hint = (
            "  az login\n"
            "  az bicep build --file infra/main.bicep --outfile infra/main.json\n"
            "  az deployment group create --resource-group <rg> \\\n"
            "      --template-file infra/main.bicep \\\n"
            "      --parameters infra/dev.bicepparam"
        )
    elif deploy_target == "gcp-cloud-run":
        deploy_hint = (
            "  # One-time per project: bootstrap WIF + service account\n"
            "  gcloud auth login\n"
            "  gcloud config set project <your-gcp-project-id>\n"
            "  bash infra/bootstrap.sh\n"
            "\n"
            "  # Then push: CI uses WIF, no JSON keys needed.\n"
            "  git remote add origin <your-repo-url> && git push -u origin main"
        )
    elif deploy_target == "gcp-gke-autopilot":
        deploy_hint = (
            "  # One-time per project: bootstrap WIF + GKE Autopilot cluster\n"
            "  gcloud auth login\n"
            "  gcloud config set project <your-gcp-project-id>\n"
            "  bash infra/bootstrap.sh\n"
            "\n"
            "  # Then push: CI uses WIF to build, push, and kustomize-apply.\n"
            "  git remote add origin <your-repo-url> && git push -u origin main"
        )
    else:
        deploy_hint = ""

    console.print(
        Panel(
            f"[bold green]✓[/bold green] Created service [cyan]{name}[/cyan] "
            f"at [cyan]{created}[/cyan]\n"
            f"  template:    {template}\n"
            f"  ci:          {ci_provider}\n"
            f"  deploy:      {deploy_target}"
            + (
                f" ({azure_region})"
                if deploy_target.startswith("azure")
                else f" ({gcp_region}, project {gcp_project_id or '<set --gcp-project-id>'})"
                if deploy_target.startswith("gcp-")
                else ""
            )
            + f"\n"
            f"  coverage:    {coverage_threshold}%\n"
            f"  registry:    {registry}\n"
            f"  db:          {db}\n"
            + (
                f"  github:      {created_repo_url}\n"
                if created_repo_url
                else ""
            )
            + f"\n"
            f"[bold]Next steps:[/bold]\n"
            f"  cd {name}\n"
            + (
                ""
                if no_git
                else (
                    ""
                    if repo
                    else f"  git add . && git commit -m \"feat: scaffold service\"\n  git remote add origin <your-repo-url>\n  git push -u origin main\n"
                )
            )
            + f"  {deploy_hint}",
            title="service scaffolded",
            border_style="green",
        )
    )


@main.command()
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=Path("."),
    required=False,
)
@click.option(
    "--db",
    "db",
    type=click.Choice(["postgres", "mysql", "mssql", "cosmosdb"], case_sensitive=False),
    default=None,
    help="Database backend to switch to.",
)
@click.option(
    "--show",
    "show_only",
    is_flag=True,
    help="Print the current settings and exit without making changes.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would change without writing any files.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
def modify(
    path: Path,
    db: str | None,
    show_only: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Modify settings on an existing scaffolded service.

    For v1 only --db is supported. Other fields (--ci, --registry,
    --azure-region, --coverage) will be added in subsequent versions.

    Re-renders the relevant files with the new value and shows a unified diff
    before writing. Use --dry-run to preview without writing.
    """
    try:
        mod = ServiceModifier(path=path, new_db=db or "postgres")
        mod.inspect()
    except ModifierError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)

    if show_only:
        console.print(
            Panel(
                f"  template:    {mod.template or 'unknown'}\n"
                f"  deploy:      {mod.deploy_target}\n"
                f"  db:          {mod.current_db or 'unknown'}\n",
                title=f"settings for {path}",
                border_style="cyan",
            )
        )
        return

    if db is None:
        err_console.print(
            "[bold red]error:[/bold red] no --db flag passed. "
            "Currently `servicectl modify` only supports --db. "
            "Use --show to inspect current settings without changing anything."
        )
        sys.exit(2)

    if mod.current_db == "unknown":
        err_console.print(
            f"[bold red]error:[/bold red] could not detect current --db at {path}. "
            "Was this service scaffolded by servicectl?"
        )
        sys.exit(2)

    if mod.current_db == db:
        console.print(
            f"[green]✓[/green] {path} is already using --db={db}. No changes needed."
        )
        return

    try:
        diffs = mod.diff()
    except ModifierError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)

    if not diffs:
        console.print(
            f"[green]✓[/green] Re-rendering --db={mod.current_db} -> --db={db} produced no changes."
        )
        return

    console.print(f"[bold]Pending changes: {mod.current_db} -> {db}[/bold]")
    for rel, diff_text in diffs:
        console.print(f"\n[cyan]--- {rel}[/cyan]")
        for line in diff_text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                console.print(f"[bold]{line}[/bold]")
            elif line.startswith("+"):
                console.print(f"[green]{line}[/green]")
            elif line.startswith("-"):
                console.print(f"[red]{line}[/red]")
            else:
                console.print(line)

    if dry_run:
        console.print("\n[yellow]--dry-run set; no files written.[/yellow]")
        return

    if not yes:
        proceed = _safe_ask(
            questionary.confirm,
            f"Apply these changes to {path}?",
            default=False,
        )
        if not proceed:
            console.print("[yellow]Aborted; no files written.[/yellow]")
            return

    try:
        written = mod.run(dry_run=False)
    except ModifierError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)

    console.print(
        f"\n[bold green]✓[/bold green] Updated {len(written)} file(s) under {path}:"
    )
    for rel in written:
        console.print(f"  - {rel}")


@main.command()
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=Path("."),
    required=False,
)
@click.option("--strict", is_flag=True, help="Treat warnings as errors (exit 2 if any warnings exist).")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of human-readable text (useful in CI).")
@click.option("--pause", is_flag=True, help="Wait for a keypress before exiting (useful when launched from a shortcut so the window doesn't close immediately).")
def doctor(path: Path, strict: bool, as_json: bool, pause: bool) -> None:
    """Validate an existing scaffolded service against servicectl standards.

    Checks for: required files (Dockerfile, .gitleaks.toml, README.md, etc.),
    Dockerfile quality (multi-stage, non-root user), CI configuration,
    coverage threshold, and Azure overlay files when applicable.

    Exit codes: 0 = clean, 1 = warnings, 2 = errors (or warnings if --strict).
    """
    report = run_checks(path)
    if as_json:
        click.echo(render_json(report))
    else:
        click.echo(render_text(report))

    if pause:
        # Keep the window open so the user can read the output before it closes.
        # Skip when stdin isn't a TTY (e.g. piped from another command).
        if sys.stdin.isatty() and sys.stdout.isatty():
            click.echo("")
            click.echo("Press any key to exit...")
            try:
                import msvcrt  # Windows-only; standard library.
                msvcrt.getch()
            except ImportError:
                # Non-Windows fallback: read a line from stdin.
                input()

    if strict and report.has_warnings and not report.has_errors:
        sys.exit(EXIT_ERROR)
    else:
        sys.exit(report.exit_code())


@main.command()
@click.argument(
    "repo",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=Path("."),
    required=False,
)
@click.option("--dry-run", is_flag=True, help="Print the refresh plan and exit without making changes.")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--resolve-on-conflict",
    is_flag=True,
    help=(
        "On a RefreshConflict, automatically create a branch with a "
        "resolution commit and open a PR against the source repo. "
        "The PR URL is printed on stdout so the integration test's CI "
        "can wait on it. Default behavior (no flag) is to surface the "
        "conflict and let the user resolve manually."
    ),
)
@click.option(
    "--no-input",
    "no_input",
    is_flag=True,
    help=(
        "Disable the interactive override prompt. By default, "
        "`servicectl refresh` will walk you through the "
        "deploy-impacting fields (template, deploy target, region / "
        "project, registry, database) when stdin is a TTY, showing "
        "the current value (from .servicectl.json) as the default "
        "for each so you can flip a single field without re-typing "
        "the rest. Pass --no-input (e.g. in CI or scripts) to use "
        "the on-disk config as-is. Honored automatically when stdin "
        "is not a TTY (piped input / CI); honored by env var "
        "SERVICECTL_NO_INPUT=1 as a backdoor. Independent of --yes: "
        "--yes skips the final yes/no confirmation, --no-input skips "
        "the value-collection prompts."
    ),
)
@click.option(
    "--auto-merge/--no-auto-merge",
    "auto_merge",
    default=None,  # None means "prompt the user" (the default UX).
    help=(
        "After a successful refresh, fast-forward the source repo's "
        "base branch into the refresh worktree's branch, then remove "
        "the worktree. Default UX is to ask you (y/N) whether to do "
        "this. Use --auto-merge to say yes without asking, or "
        "--no-auto-merge to say no without asking. Conflicts (non-FF) "
        "are reported and the worktree is preserved so you can resolve "
        "manually. Implies --yes when --auto-merge is given."
    ),
)


def refresh(
    repo: Path,
    dry_run: bool,
    yes: bool,
    resolve_on_conflict: bool,
    no_input: bool,
    auto_merge: bool | None,
) -> None:
    """Refresh an existing scaffolded service against the current simple_and_clean.

    Classifies the repo's commits into SAC-managed (filtered out) and
    developer (preserved) buckets, regenerates the scaffold from the
    latest template set, and replays the developer commits on top.

    The original branch is untouched until success; on conflict the
    recovery ref `refs/sac/pre-refresh-<id>` points at the pre-refresh HEAD
    so you can roll back with `git update-ref`.

    Requires a `.servicectl.json` in the service root (written by
    `servicectl init`). Without it, refresh refuses to guess the
    template/deploy/registry flags -- a wrong guess would silently
    change the service.

    With `--resolve-on-conflict`, a `RefreshConflict` triggers
    automatic resolution: a new branch is created on the source
    repo, a resolution commit is pushed, and a PR is opened. The PR
    URL is printed on stdout (and exit code is 0). The integration
    test's CI workflow polls this URL and waits for the PR to merge,
    then re-runs refresh.
    """
    from .replay import refresh as replay_refresh

    # Interactive override prompt: if stdin is a TTY, --no-input wasn't
    # passed, and SERVICECTL_NO_INPUT isn't set, walk the user through
    # the deploy-impacting fields of .servicectl.json. Each field
    # shows its current value as the default so the common case
    # ("flip one field") is Enter-spam-fast. If anything changed,
    # write the new config back to disk so replay_refresh() picks it
    # up; replay_refresh() reads .servicectl.json from disk early, so
    # a config write here propagates naturally.
    #
    # We read the config ourselves rather than going through
    # replay_refresh() so the prompt can run before replay_refresh()
    # does any heavy work. If the config is missing we let
    # replay_refresh() raise RefreshConfigMissing; the user gets the
    # same error as before the prompt existed.
    from .sac_config import read_config as _read_cfg
    current_config: SacConfig | None = None
    cfg_path = repo / ".servicectl.json"
    if cfg_path.exists():
        # Only read the config if the file exists; a missing config
        # is a legitimate refresh-time error and we let the existing
        # RefreshConfigMissing path surface it. (We can't use a bare
        # try/except here because read_config() raises FileNotFoundError
        # for the missing-file case, but it can also raise other
        # exceptions -- e.g. JSON parse errors -- that we DO want to
        # propagate as bugs rather than silently turning into
        # "missing config".)
        current_config = _read_cfg(repo)

    # Declared outside the prompt guard so they're always defined
    # for the `replay_refresh()` call below, even when the prompt
    # path is skipped (--no-input, non-TTY, env-var override, or
    # missing `.servicectl.json` -- in which case the user never
    # got to change anything and `changes` is empty).
    new_config: SacConfig | None = None
    changes: list[str] = []
    if (
        current_config is not None
        and not no_input
        and _stdin_is_tty()
        and not os.environ.get("SERVICECTL_NO_INPUT")
    ):
        console.print(
            Panel(
                f"[bold]Refreshing {current_config.service_name or repo}[/bold]\n\n"
                f"I'm going to walk you through the deploy-impacting "
                f"fields. Each one shows the current value in [cyan]"
                f"[brackets][/cyan]; press Enter to keep, or type a new "
                f"value to change.",
                border_style="cyan",
            )
        )
        new_config = _prompt_refresh_overrides(config=current_config)
        changes = _summarize_refresh_changes(current_config, new_config)
        if changes:
            console.print("\n[bold]Changes:[/bold]")
            for line in changes:
                console.print(line)
            console.print(
                f"\n[dim]refresh will use the new values; the source repo's "
                f".servicectl.json is left untouched until the worktree "
                f"branch is merged.[/dim]"
            )
        else:
            console.print("\n[dim]No changes; refresh will use the on-disk config.[/dim]")

    # Lazy import to keep `servicectl --help` fast and to avoid a circular
    # import risk (replay -> generator -> sac_trailers at module-load time).
    #
    # `config_override` plumbing: when the prompt was interactive AND
    # the user actually changed something, thread the in-memory
    # `new_config` into `replay_refresh()` rather than writing it to
    # disk first. The CLI used to do `_write_cfg(repo, new_config)`
    # here so that `replay_refresh()` could re-read it from disk, but
    # that left the source repo's working tree dirty with a
    # prompt-confirmed `.servicectl.json` -- which has two annoying
    # consequences:
    #   1. The follow-up `git merge --ff-only sac/refresh/<id>` from
    #      inside the source repo refuses with "Your local changes to
    #      the following files would be overwritten by merge:
    #      .servicectl.json" because the on-disk dirty index conflicts
    #      with the merge target's working tree, even when the content
    #      matches the worktree's `.servicectl.json` byte-for-byte.
    #   2. Even after a successful fast-forward, the dirty state
    #      persists in `git status` until the user commits or stashes
    #      -- fast-forward updates `HEAD`/the index but doesn't touch
    #      the working tree's "modified" flag for an already-dirty file.
    # Threading the config in memory (via replay.refresh()'s new
    # `config_override` parameter) keeps the source repo pristine
    # across the refresh regardless of whether the user accepts or
    # declines the post-refresh merge prompt.
    try:
        run = replay_refresh(
            repo,
            dry_run=dry_run,
            config_override=new_config if changes else None,
        )
    except RefreshNotScaffoldedError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)
    except RefreshConfigMissing as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)
    except RefreshConflict as e:
        if not resolve_on_conflict:
            err_console.print(
                Panel(
                    f"[bold red]refresh halted: cherry-pick conflict[/bold red]\n\n"
                    f"  conflicting files ({len(e.conflicting_files)}):\n"
                    + "\n".join(f"    - {f}" for f in e.conflicting_files)
                    + f"\n\n"
                    f"  recovery ref: [cyan]{e.recovery_ref}[/cyan]\n"
                    f"  recover with:  git update-ref refs/heads/<branch> {e.recovery_ref}",
                    title="refresh failed",
                    border_style="red",
                )
            )
            sys.exit(2)
        # Resolve-on-conflict: try to fix the conflict via a PR.
        from .resolve import resolve_conflict as do_resolve
        try:
            # The conflict might be on a config the user has touched;
            # re-read the config from disk in case the in-memory copy
            # is stale. (refresh() reads the config early; if anything
            # else wrote to .servicectl.json between then and now, we'd
            # want to pick that up.)
            from .sac_config import read_config as read_cfg_fresh
            fresh_config = read_cfg_fresh(repo)
            result = do_resolve(
                source_repo=repo,
                conflicting_files=e.conflicting_files,
                config=fresh_config,
                run_id=e.recovery_ref.split("-")[-1],
            )
        except RuntimeError as re_:
            err_console.print(
                Panel(
                    f"[bold red]refresh halted: could not auto-resolve[/bold red]\n\n"
                    f"  conflicting files ({len(e.conflicting_files)}):\n"
                    + "\n".join(f"    - {f}" for f in e.conflicting_files)
                    + f"\n\n"
                    f"  reason: {re_}\n\n"
                    f"  recovery ref: [cyan]{e.recovery_ref}[/cyan]",
                    title="resolution failed",
                    border_style="red",
                )
            )
            sys.exit(2)
        except subprocess.CalledProcessError as cpe:
            # The resolve step ran but a subprocess failed (e.g.
            # `git push` returned non-zero, or `gh pr create` was
            # rejected). The exception's stderr carries the real
            # reason ("403 Forbidden", "no admin on this repo",
            # "fetch first", etc.). Surface it so the user can
            # diagnose without digging through the workflow log.
            err_console.print(
                Panel(
                    f"[bold red]refresh halted: resolution subcommand failed[/bold red]\n\n"
                    f"  command: {cpe.cmd}\n"
                    f"  exit code: {cpe.returncode}\n"
                    f"  stderr: {cpe.stderr or '(empty)'}\n"
                    f"  stdout: {cpe.output or '(empty)'}\n\n"
                    f"  conflicting files ({len(e.conflicting_files)}):\n"
                    + "\n".join(f"    - {f}" for f in e.conflicting_files)
                    + f"\n\n"
                    f"  recovery ref: [cyan]{e.recovery_ref}[/cyan]",
                    title="resolution failed",
                    border_style="red",
                )
            )
            sys.exit(2)
        # Print the PR URL on stdout so the CI can read it. The CI's
        # wait-on-PR step looks for this line.
        print(f"resolution_pr_url: {result.pr_url}")
        console.print(
            Panel(
                f"[bold green]resolution PR created[/bold green]\n\n"
                f"  strategy:    {result.strategy}\n"
                f"  branch:      {result.branch_name}\n"
                f"  commit:      {result.commit_sha[:12]}\n"
                f"  PR:          {result.pr_url}\n\n"
                f"  {result.message}\n\n"
                f"Next: wait for the PR to merge, then re-run refresh.",
                title="refresh conflict resolved",
                border_style="green",
            )
        )
        return

    if dry_run:
        cfg = run.config
        cfg_lines = (
            f"  service:     {cfg.service_name or '<not set; will fall back to dir name>'}\n"
            f"  template:    {cfg.template}\n"
            f"  ci:          {cfg.ci_provider}\n"
            f"  deploy:      {cfg.deploy_target}\n"
            f"  registry:    {cfg.registry}\n"
            f"  coverage:    {cfg.coverage_threshold}%\n"
            f"  db:          {cfg.db}\n"
            if cfg is not None
            else "  config:      <missing --refresh needs .servicectl.json>\n"
        )
        console.print(
            Panel(
                f"[bold]refresh plan (dry run)[/bold]\n\n"
                f"  repo:           {repo}\n"
                f"  base branch:    {run.base_branch}\n"
                f"  worktree:       {run.worktree_dir}\n"
                f"  recovery ref:   {run.recovery_ref}\n"
                f"  scaffold sha:   {run.scaffold_sha or '(would be regenerated)'}\n"
                f"  developer commits: {len(run.developer_commits)}\n\n"
                f"[bold]SAC config (from .servicectl.json):[/bold]\n"
                f"{cfg_lines}\n"
                f"[bold]Next steps:[/bold]\n"
                f"  servicectl refresh {repo}    # for real",
                title="refresh plan",
                border_style="cyan",
            )
        )
        return

    if not yes:
        console.print(
            Panel(
                f"[bold]About to refresh {repo}[/bold]\n\n"
                f"  base branch:    {run.base_branch}\n"
                f"  worktree:       {run.worktree_dir}\n"
                f"  recovery ref:   {run.recovery_ref}\n"
                f"  developer commits: {len(run.developer_commits)}\n\n"
                f"The worktree branch will be fast-forwarded into {run.base_branch} on success.\n"
                f"On conflict, {run.recovery_ref} lets you roll back.",
                title="refresh confirmation",
                border_style="yellow",
            )
        )
        proceed = _safe_ask(questionary.confirm, "Proceed?", default=False)
        if not proceed:
            console.print("[yellow]Aborted; no changes made.[/yellow]")
            return

    if run.succeeded:
        console.print(
            Panel(
                f"[bold green]✓[/bold green] refresh complete\n\n"
                f"  scaffold sha:   {run.scaffold_sha}\n"
                f"  developer commits replayed: {len(run.developer_commits)}\n"
                f"  worktree:       {run.worktree_dir}\n"
                f"  recovery ref:   {run.recovery_ref}\n\n"
                f"The worktree is ready to merge or fast-forward into {run.base_branch}.",
                title="refresh succeeded",
                border_style="green",
            )
        )
        # Post-refresh: offer to merge the worktree branch into the
        # base branch and clean up. The user can answer y/N here, or
        # have pre-decided via --auto-merge / --no-merge. The same
        # panel of git commands is shown either way so the user can
        # copy-paste if they decline.
        _offer_post_refresh_merge(run, auto_merge=auto_merge, no_input=no_input)
    else:
        # Defensive: replay_refresh() raises on failure, so we shouldn't get
        # here. If we do, surface it cleanly.
        err_console.print(
            "[bold red]error:[/bold red] refresh returned a non-success state without raising; "
            "this is a bug in servicectl."
        )
        sys.exit(2)


def _merge_and_cleanup_worktree(
    source_repo: Path,
    base_branch: str,
    branch_name: str,
    worktree_dir: Path,
) -> bool:
    """Fast-forward `base_branch` into `branch_name` and clean up.

    Returns True on full success, False if any step failed (in which
    case the worktree is preserved so the user can investigate). Each
    step prints its own status so the user can see exactly what
    happened.

    The merge uses `--ff-only` so a non-fast-forward (e.g. someone
    else pushed to main while refresh was running) surfaces as a
    clear error rather than creating a surprise merge commit. On
    non-FF, the worktree branch is left in place; the user can rebase
    and re-run, or merge manually with a real merge commit.
    """
    console.print(
        f"\n[bold]Merging {branch_name} into {base_branch}[/bold]"
    )

    # Step 1: fast-forward merge.
    try:
        merge_result = subprocess.run(
            ["git", "-C", str(source_repo), "merge", "--ff-only", branch_name],
            capture_output=True,
            text=True,
            check=False,  # we'll inspect the result ourselves
        )
    except FileNotFoundError:
        err_console.print(
            "[bold red]error:[/bold red] `git` not found on PATH; "
            "can't auto-merge. Run the commands below by hand."
        )
        return False

    if merge_result.returncode != 0:
        err_console.print(
            Panel(
                f"[bold red]merge failed[/bold red]\n\n"
                f"  {merge_result.stderr.strip() or '(no stderr)'}\n\n"
                f"The worktree is preserved at [cyan]{worktree_dir}[/cyan] "
                f"on branch [cyan]{branch_name}[/cyan]. You can:\n"
                f"  - rebase onto current {base_branch} and re-run refresh, or\n"
                f"  - merge with a real merge commit (drop --ff-only), or\n"
                f"  - inspect the diff and discard with:\n"
                f"      git worktree remove {worktree_dir}\n"
                f"      git branch -D {branch_name}",
                border_style="red",
            )
        )
        return False
    console.print(f"  [green]merged[/green] {branch_name} into {base_branch}")

    # Step 2: remove the worktree.
    wt_result = subprocess.run(
        ["git", "-C", str(source_repo), "worktree", "remove", str(worktree_dir), "--force"],
        capture_output=True,
        text=True,
        check=False,
    )
    if wt_result.returncode != 0:
        err_console.print(
            Panel(
                f"[bold yellow]merge OK, worktree cleanup failed[/bold yellow]\n\n"
                f"  {wt_result.stderr.strip() or '(no stderr)'}\n\n"
                f"Your branch is updated, but the worktree is still at:\n"
                f"  [cyan]{worktree_dir}[/cyan]\n\n"
                f"Remove it by hand with:\n"
                f"  git worktree remove {worktree_dir} --force",
                border_style="yellow",
            )
        )
        return False
    console.print(f"  [green]removed[/green] worktree at {worktree_dir}")

    # Step 3: delete the refresh branch. We use -d (safe-delete)
    # which only succeeds if the branch is fully merged; since we
    # just --ff-only'd it, the branch is fully merged, so -d works.
    branch_result = subprocess.run(
        ["git", "-C", str(source_repo), "branch", "-d", branch_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if branch_result.returncode != 0:
        # -d refused (which would mean the branch isn't fully merged,
        # which shouldn't happen post --ff-only). Surface but don't
        # fail; the merge + worktree cleanup already succeeded.
        err_console.print(
            f"[bold yellow]warning:[/bold yellow] couldn't delete branch "
            f"{branch_name} (probably still checked out somewhere):\n"
            f"  {branch_result.stderr.strip() or '(no stderr)'}\n"
            f"Delete it by hand with: git branch -d {branch_name}"
        )
        return False
    console.print(f"  [green]deleted[/green] branch {branch_name}")

    console.print(
        f"\n[bold green]All done.[/bold green] {base_branch} is now at "
        f"the refreshed scaffold. The recovery ref is still in your "
        f"reflog if you need to roll back."
    )
    return True


def _print_manual_merge_commands(
    source_repo: Path,
    base_branch: str,
    branch_name: str,
    worktree_dir: Path,
) -> None:
    """Print the git commands for the user to run by hand."""
    console.print(
        Panel(
            f"To land this refresh, run these from [cyan]{source_repo}[/cyan]:\n\n"
            f"  [bold]git merge --ff-only {branch_name}[/bold]\n"
            f"  git worktree remove {worktree_dir} --force\n"
            f"  git branch -d {branch_name}\n\n"
            f"Or to do a real merge commit instead of fast-forward, drop "
            f"the --ff-only. The recovery ref is still in your reflog if "
            f"you need to roll back: refs/sac/pre-refresh-<run_id>.",
            title=f"merge {branch_name} into {base_branch}",
            border_style="cyan",
        )
    )


def _offer_post_refresh_merge(
    run,
    *,
    auto_merge: bool | None,
    no_input: bool,
) -> None:
    """After a successful refresh, offer to merge + clean up.

    Decision tree:
      - auto_merge is True  -> do it (no prompt)
      - auto_merge is False -> print the manual commands (no prompt)
      - auto_merge is None, TTY, not no_input  -> ask y/N
      - auto_merge is None, not TTY (or no_input)  -> print manual commands

    The merge helper is conservative: it uses --ff-only and won't
    silently create merge commits. On failure it leaves the worktree
    in place and tells you how to recover.
    """
    if not run.succeeded:
        return  # defensive: don't offer to merge a failed refresh

    if not run.branch_name:
        _print_manual_merge_commands(
            run.source_repo, run.base_branch, "<unknown-branch>", run.worktree_dir
        )
        return

    if auto_merge is False:
        _print_manual_merge_commands(
            run.source_repo, run.base_branch, run.branch_name, run.worktree_dir
        )
        return

    if auto_merge is True:
        _merge_and_cleanup_worktree(
            run.source_repo, run.base_branch, run.branch_name, run.worktree_dir
        )
        return

    if no_input or not _stdin_is_tty() or os.environ.get("SERVICECTL_NO_INPUT"):
        _print_manual_merge_commands(
            run.source_repo, run.base_branch, run.branch_name, run.worktree_dir
        )
        return

    console.print(
        f"\n[bold]Refresh succeeded.[/bold] {run.branch_name} is ready "
        f"to fast-forward into {run.base_branch}."
    )
    merge = _safe_ask(
        questionary.confirm,
        f"Merge {run.branch_name} into {run.base_branch} and clean up the worktree?",
        default=False,
    )
    if merge:
        _merge_and_cleanup_worktree(
            run.source_repo, run.base_branch, run.branch_name, run.worktree_dir
        )
    else:
        _print_manual_merge_commands(
            run.source_repo, run.base_branch, run.branch_name, run.worktree_dir
        )

if __name__ == "__main__":
    main()
