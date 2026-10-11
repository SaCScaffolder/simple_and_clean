"""CLI entry point for servicectl.

Usage:
    servicectl init <name> --template=<template> [flags]
    servicectl init --from-config=<path.json> [overrides]

Run `servicectl init --help` for the full flag list.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from . import __version__
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
from .templates import list_templates

# Force UTF-8 so Rich's Unicode glyphs (✓, →) don't choke on Windows cp1252 consoles.
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

console = Console(force_terminal=None, legacy_windows=False)
err_console = Console(stderr=True, style="bold red")


@click.group()
@click.version_option(__version__, prog_name="servicectl")
def main() -> None:
    """servicectl — self-service DevOps scaffolding."""


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
    from_config: Path | None,
    strict_config: bool,
) -> None:
    """Scaffold a new service named NAME."""
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
            f"  db:          {db}\n\n"
            f"[bold]Next steps:[/bold]\n"
            f"  cd {name}\n"
            + ("" if no_git else f"  git add . && git commit -m \"feat: scaffold service\"\n  git remote add origin <your-repo-url>\n  git push -u origin main\n")
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
        console.print(f"\nApply these changes to {path}? [y/N]")
        try:
            response = input().strip().lower()
        except EOFError:
            response = "n"
        if response != "y":
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
def refresh(repo: Path, dry_run: bool, yes: bool) -> None:
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
    """
    from .replay import refresh as replay_refresh

    # Lazy import to keep `servicectl --help` fast and to avoid a circular
    # import risk (replay -> generator -> sac_trailers at module-load time).
    try:
        run = replay_refresh(repo, dry_run=dry_run)
    except RefreshNotScaffoldedError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)
    except RefreshConfigMissing as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        sys.exit(2)
    except RefreshConflict as e:
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
                f"On conflict, {run.recovery_ref} lets you roll back.\n\n"
                f"Proceed? [y/N]",
                title="refresh confirmation",
                border_style="yellow",
            )
        )
        try:
            response = input().strip().lower()
        except EOFError:
            response = "n"
        if response != "y":
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
    else:
        # Defensive: replay_refresh() raises on failure, so we shouldn't get
        # here. If we do, surface it cleanly.
        err_console.print(
            "[bold red]error:[/bold red] refresh returned a non-success state without raising; "
            "this is a bug in servicectl."
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
