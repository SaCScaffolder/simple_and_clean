"""CLI entry point for servicectl.

Usage:
    servicectl init <name> --template=<template> [flags]

Run `servicectl init --help` for the full flag list.
"""

from __future__ import annotations

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
@click.argument("name")
@click.option(
    "--template",
    "template",
    required=True,
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
    type=click.Choice(["local", "azure"], case_sensitive=False),
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
    type=click.Choice(["dockerhub", "ghcr", "ecr", "acr", "gcr"], case_sensitive=False),
    default="ghcr",
    show_default=True,
    help="Container registry.",
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
def init(
    name: str,
    template: str,
    ci_provider: str,
    deploy_target: str,
    azure_region: str,
    coverage_threshold: int,
    registry: str,
    output_dir: Path,
    no_git: bool,
    no_readme: bool,
) -> None:
    """Scaffold a new service named NAME."""
    try:
        gen = ServiceGenerator(
            name=name,
            template=template,
            ci_provider=ci_provider,
            deploy_target=deploy_target,
            azure_region=azure_region,
            coverage_threshold=coverage_threshold,
            registry=registry,
            output_dir=output_dir,
            with_git=not no_git,
            with_readme=not no_readme,
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
    else:
        deploy_hint = ""

    console.print(
        Panel(
            f"[bold green]✓[/bold green] Created service [cyan]{name}[/cyan] "
            f"at [cyan]{created}[/cyan]\n"
            f"  template:    {template}\n"
            f"  ci:          {ci_provider}\n"
            f"  deploy:      {deploy_target}"
            + (f" ({azure_region})" if deploy_target.startswith("azure") else "")
            + f"\n"
            f"  coverage:    {coverage_threshold}%\n"
            f"  registry:    {registry}\n\n"
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
    "--strict",
    is_flag=True,
    help="Treat warnings as errors (exit 2 if any warnings exist).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output JSON instead of human-readable text (useful in CI).",
)
@click.option(
    "--pause",
    is_flag=True,
    help="Wait for a keypress before exiting (useful when launched from a shortcut so the window doesn't close immediately).",
)
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


if __name__ == "__main__":
    main()
