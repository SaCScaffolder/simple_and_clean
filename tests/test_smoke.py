"""Smoke tests for servicectl generator + template registry.

Run with: pytest tests/ (or just `python tests/test_smoke.py`).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# Make the installed package importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator, ScaffoldError  # noqa: E402
from servicectl.modifier import ModifierError, ServiceModifier  # noqa: E402
from servicectl.templates import list_templates  # noqa: E402
import subprocess  # noqa: E402


def _scaffold(tmp: Path, name: str, template: str, **overrides) -> Path:
    gen = ServiceGenerator(
        name=name,
        template=template,
        ci_provider=overrides.get("ci_provider", "github-actions"),
        deploy_target=overrides.get("deploy_target", "local"),
        azure_region=overrides.get("azure_region", "eastus"),
        coverage_threshold=overrides.get("coverage_threshold", 80),
        registry=overrides.get("registry", "ghcr"),
        db=overrides.get("db", "postgres"),
        output_dir=tmp,
        with_git=False,
        with_readme=True,
    )
    return gen.run()


def test_all_templates_registered():
    expected = {"node-express", "node-react-web", "python-flask", "dotnet-webapi", "go-webapi"}
    assert set(list_templates()) == expected


def test_node_express_scaffolds():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-node", "node-express")
        assert (target / "package.json").exists()
        assert (target / "Dockerfile").exists()
        assert (target / "docker-compose.dev.yml").exists()
        assert (target / ".github" / "workflows" / "ci.yml").exists()
        assert (target / "azure-pipelines.yml").exists()
        assert (target / ".env.example").exists()
        assert (target / "README.md").exists()
        pkg = (target / "package.json").read_text()
        assert '"name": "demo-node"' in pkg
        assert '"lines": 80' in pkg


def test_python_flask_scaffolds_with_custom_coverage():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-py", "python-flask", coverage_threshold=90)
        pyproject = (target / "pyproject.toml").read_text()
        assert "--cov-fail-under=90" in pyproject
        assert 'name = "demo-py"' in pyproject


def test_dotnet_pascal_cases_filename():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-dotnet", "dotnet-webapi")
        # Jinja-substituted filenames should land as PascalCase.
        assert (target / "DemoDotnet.csproj").exists()
        assert (target / "tests" / "DemoDotnet.Tests" / "DemoDotnet.Tests.csproj").exists()
        assert (target / "tests" / "DemoDotnet.Tests" / "UnitTest1.cs").exists()


def test_go_webapi_scaffolds():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-go", "go-webapi", coverage_threshold=85)
        # Required static-ish files (some are .j2 but render to no-suffix names).
        assert (target / "Dockerfile").exists()
        assert (target / "docker-compose.dev.yml").exists()
        assert (target / ".github" / "workflows" / "ci.yml").exists()
        assert (target / "azure-pipelines.yml").exists()
        assert (target / ".env.example").exists()
        assert (target / "README.md").exists()
        assert (target / ".gitleaks.toml").exists()
        assert (target / ".dockerignore").exists()
        assert (target / ".gitignore").exists()
        # Go module + entrypoint + handler package.
        assert (target / "go.mod").exists()
        assert (target / "cmd" / "server" / "main.go").exists()
        # Jinja-substituted snake_case directory.
        assert (target / "internal" / "demo_go" / "server.go").exists()
        assert (target / "internal" / "demo_go" / "server_test.go").exists()
        # Substitution sanity: the service name should appear in the rendered files.
        main = (target / "cmd" / "server" / "main.go").read_text()
        assert "demo-go" in main
        server = (target / "internal" / "demo_go" / "server.go").read_text()
        assert "demo-go" in server
        # go.mod module path should reflect the service name.
        gomod = (target / "go.mod").read_text()
        assert "demo-go" in gomod
        # README should reference the coverage threshold.
        readme = (target / "README.md").read_text()
        assert "85%" in readme


def test_go_webapi_dashed_name_substitutes_correctly():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "billing.api", "go-webapi")
        # snake_case for Go package paths.
        assert (target / "internal" / "billing_api" / "server.go").exists()
        assert (target / "internal" / "billing_api" / "server_test.go").exists()
        # Service name should appear in main.go.
        main = (target / "cmd" / "server" / "main.go").read_text()
        assert "billing.api" in main


def test_node_react_web_scaffolds():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-react", "node-react-web", coverage_threshold=80)
        # Build / scaffold-essential files (post-render, .j2 suffix stripped).
        assert (target / "package.json").exists()
        assert (target / "tsconfig.json").exists()
        assert (target / "tsconfig.node.json").exists()
        assert (target / "vite.config.ts").exists()
        assert (target / "tailwind.config.ts").exists()
        assert (target / "postcss.config.js").exists()
        assert (target / "components.json").exists()
        assert (target / "index.html").exists()
        assert (target / "Dockerfile").exists()
        assert (target / "nginx.conf").exists()
        assert (target / ".env.example").exists()
        assert (target / ".gitleaks.toml").exists()
        assert (target / ".gitignore").exists()
        assert (target / ".dockerignore").exists()
        assert (target / "README.md").exists()
        # CI workflows.
        assert (target / ".github" / "workflows" / "ci.yml").exists()
        assert (target / "azure-pipelines.yml").exists()
        # src/ tree.
        assert (target / "src" / "main.tsx").exists()
        assert (target / "src" / "App.tsx").exists()
        assert (target / "src" / "index.css").exists()
        assert (target / "src" / "test-setup.ts").exists()
        assert (target / "src" / "lib" / "utils.ts").exists()
        assert (target / "src" / "components" / "Layout.tsx").exists()
        assert (target / "src" / "components" / "ui" / "button.tsx").exists()
        assert (target / "src" / "pages" / "HomePage.tsx").exists()
        assert (target / "src" / "pages" / "NotFoundPage.tsx").exists()
        assert (target / "src" / "api" / "client.ts").exists()
        # axios baseURL must fall back to "/api" when VITE_API_BASE_URL is unset.
        # Vite substitutes missing VITE_* vars with the string "undefined" (not
        # actual undefined), so a naive `?? "/api"` check returns the string
        # "undefined". The client must explicitly handle that case or every
        # request becomes `/undefined/...` in production builds.
        client = (target / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        assert 'VITE_API_BASE_URL' in client, "client must read VITE_API_BASE_URL"
        assert '"undefined"' in client or "'undefined'" in client, (
            "client must guard against Vite's string 'undefined' substitution "
            "for missing VITE_* env vars"
        )
        # tests/ tree.
        assert (target / "tests" / "App.test.tsx").exists()
        assert (target / "tests" / "utils.test.ts").exists()
        # Service-name substitutions. Some files have UTF-8 characters
        # (smart quotes, em dashes) so read with explicit encoding.
        pkg = (target / "package.json").read_text(encoding="utf-8")
        assert '"name": "demo-react"' in pkg
        # main.tsx imports App but doesn't reference the name directly; verify the
        # service name shows up in App.tsx, HomePage.tsx, and Layout.tsx instead.
        for rel in ("src/App.tsx", "src/pages/HomePage.tsx", "src/components/Layout.tsx"):
            text = (target / rel).read_text(encoding="utf-8")
            assert "demo-react" in text, f"service name not substituted in {rel}"
        # Coverage gate is opt-in via COVERAGE_THRESHOLD env var, not unconditional.
        # (See commit 5808d00 — unconditional 80% gate was a lie on a contract
        # that ships with no tests.)
        vite = (target / "vite.config.ts").read_text(encoding="utf-8")
        assert "COVERAGE_THRESHOLD" in vite, "coverage gate should be opt-in via COVERAGE_THRESHOLD env var"
        assert "lines: 80" not in vite, "coverage gate should not be unconditional by default"
        # Triple-slash reference must be present so `tsc -b` sees the `test` config.
        # Without it, defineConfig() rejects the `test` block with TS2769.
        assert '/// <reference types="vitest" />' in vite, (
            "vite.config.ts must have /// <reference types=\"vitest\" /> at the top "
            "so tsc -b accepts the vitest `test:` block (otherwise build fails with "
            "TS2769: 'test' does not exist in type 'UserConfigExport')."
        )
        # README mentions coverage.
        readme = (target / "README.md").read_text(encoding="utf-8")
        assert "80%" in readme


def test_node_react_web_no_db_in_scaffold():
    """A frontend SPA shouldn't ship Postgres / DB plumbing. This is a
    regression guard against accidentally adding database deps to the
    frontend template (which would inflate bundle size and confuse users)."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-react", "node-react-web")
        pkg = (target / "package.json").read_text(encoding="utf-8")
        # No pg, mysql, or other DB drivers in dependencies.
        for db in ('"pg"', '"mysql"', '"mongodb"', '"sqlite3"', '"sequelize"', '"prisma"'):
            assert db not in pkg, f"{db} should not appear in node-react-web deps"
        # No POSTGRES_* in .env.example.
        env_example = (target / ".env.example").read_text(encoding="utf-8")
        assert "POSTGRES" not in env_example
        # No docker-compose.dev.yml for an SPA (no service-to-service deps).
        assert not (target / "docker-compose.dev.yml").exists(), (
            "node-react-web is an SPA; docker-compose.dev.yml should not be "
            "scaffolded (no companion services to run alongside)"
        )


def test_db_default_is_postgres():
    """If --db is not specified, the scaffold uses Postgres (preserves existing behavior)."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-py", "python-flask")
        compose = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "postgres:16-alpine" in compose
        assert "POSTGRES_HOST" in compose
        # MySQL-specific env vars must NOT be present when db is postgres.
        assert "MYSQL_HOST" not in compose


def test_db_mysql_emits_mysql_image_and_env():
    """--db=mysql swaps the docker-compose image + env vars to MySQL."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-py", "python-flask", db="mysql")
        compose = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "mysql:8.0" in compose
        assert "MYSQL_HOST" in compose
        assert "MYSQL_DATABASE: demo-py" in compose
        # Postgres-specific env vars must NOT be present.
        assert "POSTGRES_HOST" not in compose
        assert "postgres:16-alpine" not in compose


def test_db_mssql_emits_mssql_image_and_env():
    """--db=mssql swaps to Azure SQL Edge (the local-dev SQL Server)."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-py", "python-flask", db="mssql")
        compose = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "azure-sql-edge" in compose
        assert "MSSQL_HOST" in compose
        assert "MSSQL_DATABASE: demo-py" in compose
        assert "POSTGRES_HOST" not in compose


def test_db_cosmosdb_emits_emulator():
    """--db=cosmosdb uses the Azure Cosmos DB Linux emulator (heavyweight)."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-py", "python-flask", db="cosmosdb")
        compose = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "azure-cosmos-emulator" in compose
        assert "COSMOS_ENDPOINT" in compose
        assert "COSMOS_DATABASE: demo-py" in compose
        # None of the SQL flavors' env vars should be present.
        assert "POSTGRES_HOST" not in compose
        assert "MYSQL_HOST" not in compose
        assert "MSSQL_HOST" not in compose


def test_db_dotnet_uses_correct_connection_string_key():
    """dotnet-webapi uses ASP.NET-style ConnectionStrings__<DB> env vars per flavor."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-dotnet", "dotnet-webapi", db="mssql")
        compose = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "ConnectionStrings__SqlServer" in compose
        assert "Server=mssql" in compose
        # Postgres connection-string key must NOT be present.
        assert "ConnectionStrings__Postgres" not in compose


def test_db_go_webapi_supports_all_flavors():
    """The Go template's docker-compose branches on --db for all 4 flavors."""
    for db_flavor in ("postgres", "mysql", "mssql", "cosmosdb"):
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), "demo-go", "go-webapi", db=db_flavor)
            compose = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
            # Each flavor has its own prefix; verify the active one is present
            # and the others are not (catches accidental cross-pollination).
            prefix_map = {
                "postgres": "POSTGRES_HOST",
                "mysql": "MYSQL_HOST",
                "mssql": "MSSQL_HOST",
                "cosmosdb": "COSMOS_ENDPOINT",
            }
            active = prefix_map[db_flavor]
            assert active in compose, f"{db_flavor}: expected {active} in compose"
            for other_prefix in prefix_map.values():
                if other_prefix == active:
                    continue
                assert other_prefix not in compose, (
                    f"{db_flavor}: {other_prefix} should not be present when --db={db_flavor}"
                )


def test_azure_overlay_emits_db_param_in_bicepparam():
    """--deploy=azure --db=<flavor> sets the db param in the bicepparam files."""
    for db_flavor in ("postgres", "mysql", "mssql", "cosmosdb"):
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(
                Path(td),
                "demo-svc",
                "python-flask",
                deploy_target="azure",
                db=db_flavor,
            )
            for env in ("dev", "staging", "prod"):
                bicepparam = (target / "infra" / f"{env}.bicepparam").read_text(encoding="utf-8")
                assert f"param db = '{db_flavor}'" in bicepparam, (
                    f"db={db_flavor} missing param in {env}.bicepparam"
                )


def test_azure_bicep_uses_conditional_resource_blocks():
    """--deploy=azure --db=<flavor> emits the right conditional resource blocks in main.bicep."""
    with tempfile.TemporaryDirectory() as td:
        # Postgres-only block: postgresDb should be conditional on db == 'postgres'.
        target = _scaffold(
            Path(td), "demo-pg", "python-flask", deploy_target="azure", db="postgres"
        )
        bicep = (target / "infra" / "main.bicep").read_text(encoding="utf-8")
        assert "if (db == 'postgres')" in bicep
        # Cosmos-only block should also be conditional (not deployed when db=postgres).
        assert "if (db == 'cosmosdb')" in bicep
        # Cosmos's autoscaleThroughput param should be declared.
        assert "param cosmosDbThroughput int" in bicep


def test_db_validation_rejects_unknown_flavor():
    """Passing an unknown --db value should raise ScaffoldError at scaffold time."""
    with tempfile.TemporaryDirectory() as td:
        try:
            _scaffold(Path(td), "demo-bad", "python-flask", db="oracle")
        except ScaffoldError as e:
            assert "unsupported --db" in str(e)
        else:
            raise AssertionError("expected ScaffoldError for unknown --db value")


def test_modify_show_prints_current_settings():
    """`servicectl modify --show` should report current template, deploy, and db without changing anything."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-show", "python-flask", db="mysql")
        mod = ServiceModifier(path=target, new_db="postgres")
        mod.inspect()
        assert mod.template == "python-flask"
        assert mod.deploy_target == "local"
        assert mod.current_db == "mysql"


def test_modify_postgres_to_mysql_rerenders_compose():
    """Modify --db=postgres -> --db=mysql rewrites docker-compose.dev.yml to use the mysql image + env vars."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-pg2my", "python-flask", db="postgres")
        # Sanity: scaffold has POSTGRES_HOST.
        compose_before = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "POSTGRES_HOST" in compose_before
        # Modify.
        mod = ServiceModifier(path=target, new_db="mysql")
        mod.run(dry_run=False)
        compose_after = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        # Now has MYSQL_HOST and no POSTGRES_HOST.
        assert "MYSQL_HOST" in compose_after
        assert "POSTGRES_HOST" not in compose_after
        assert "mysql:8.0" in compose_after


def test_modify_mysql_to_postgres_rerenders_compose():
    """Reverse direction: --db=mysql -> --db=postgres also works."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-my2pg", "python-flask", db="mysql")
        mod = ServiceModifier(path=target, new_db="postgres")
        mod.run(dry_run=False)
        compose_after = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "POSTGRES_HOST" in compose_after
        assert "MYSQL_HOST" not in compose_after


def test_modify_to_cosmos_rerenders_compose():
    """--db=postgres -> --db=cosmosdb uses the Azure Cosmos DB emulator image."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-pg2cosmos", "python-flask", db="postgres")
        mod = ServiceModifier(path=target, new_db="cosmosdb")
        mod.run(dry_run=False)
        compose_after = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert "azure-cosmos-emulator" in compose_after
        assert "COSMOS_ENDPOINT" in compose_after
        assert "POSTGRES_HOST" not in compose_after


def test_modify_with_azure_overlay_rerenders_bicepparam():
    """When the service was scaffolded with --deploy=azure, modify --db rewrites the bicepparam db param too."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(
            Path(td), "demo-azure-pg2my", "python-flask",
            deploy_target="azure", db="postgres",
        )
        # Sanity: dev bicepparam says db = 'postgres'.
        bicepparam_before = (target / "infra" / "dev.bicepparam").read_text(encoding="utf-8")
        assert "param db = 'postgres'" in bicepparam_before
        # Modify.
        mod = ServiceModifier(path=target, new_db="mysql")
        mod.run(dry_run=False)
        bicepparam_after = (target / "infra" / "dev.bicepparam").read_text(encoding="utf-8")
        assert "param db = 'mysql'" in bicepparam_after
        assert "param db = 'postgres'" not in bicepparam_after


def test_modify_same_db_is_noop():
    """Modifying to the same --db should be a no-op (no files written, no diff)."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-noop", "python-flask", db="postgres")
        compose_before = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        mod = ServiceModifier(path=target, new_db="postgres")
        mod.inspect()
        assert mod.current_db == "postgres"
        # diff() should return empty when there's nothing to change.
        assert mod.diff() == []
        # run() should return empty list.
        assert mod.run(dry_run=False) == []
        # File content should be unchanged.
        compose_after = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        assert compose_before == compose_after


def test_modify_rejects_unknown_db():
    """Passing an unknown --db value to ServiceModifier should raise ModifierError."""
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-bad-mod", "python-flask", db="postgres")
        mod = ServiceModifier(path=target, new_db="oracle")
        try:
            mod.run(dry_run=False)
        except ModifierError as e:
            assert "unsupported --db" in str(e)
        else:
            raise AssertionError("expected ModifierError for unknown --db value")


def test_modify_rejects_non_scaffolded_directory():
    """Pointing modify at a directory that doesn't have docker-compose.dev.yml should raise ModifierError."""
    with tempfile.TemporaryDirectory() as td:
        mod = ServiceModifier(path=Path(td), new_db="mysql")
        try:
            mod.run(dry_run=False)
        except ModifierError as e:
            assert "no docker-compose.dev.yml" in str(e)
        else:
            raise AssertionError("expected ModifierError for non-scaffolded directory")


def test_docker_compose_has_top_level_volumes_block():
    """Regression test: every scaffolded docker-compose.dev.yml must have a top-level
    `volumes:` block (column 0), NOT indented under `services:`. The previous bug had
    `  volumes:` indented 2 spaces, which made docker compose interpret it as a service
    called `volumes` and fail with: `services.volumes additional properties 'pgdata' not allowed`.
    """
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-compose-volumes", "python-flask", db="postgres")
        compose = (target / "docker-compose.dev.yml").read_text(encoding="utf-8")
        # Top-level volumes block must exist at column 0.
        assert "volumes:\n" in compose or compose.endswith("volumes:\n"), (
            "expected top-level `volumes:` block in docker-compose.dev.yml\n"
            f"got:\n{compose}"
        )
        # And there must NOT be an indented `  volumes:` (which would be the bug).
        # Allow 4-space indented `volumes:` (per-service mount list) but not 2-space.
        bad_lines = [line for line in compose.splitlines() if line == "  volumes:"]
        assert not bad_lines, (
            "found indented `  volumes:` block in docker-compose.dev.yml "
            "(should be at column 0)\n"
            f"got: {bad_lines}"
        )


def test_scaffolded_services_ship_githooks_pre_commit():
    """Regression test: every scaffolded service must ship a `.githooks/pre-commit`
    hook that runs the language-appropriate smoke test, with install instructions.
    Closes the gap where simple_and_clean's own `.githooks/pre-commit` protected the
    scaffolder but not the services it generated.
    """
    template_test_commands = {
        "go-webapi": ["go vet ./...", "go test -race"],
        "node-express": ["npm run lint", "npm test"],
        "node-react-web": ["npm run lint", "npm test"],
        "python-flask": ["pytest"],
        "dotnet-webapi": ["dotnet test"],
    }
    for tpl, expected_lines in template_test_commands.items():
        with tempfile.TemporaryDirectory() as td:
            target = _scaffold(Path(td), f"demo-{tpl}", tpl)
            hook = target / ".githooks" / "pre-commit"
            assert hook.exists(), (
                f"template {tpl!r} did not ship .githooks/pre-commit. "
                f"Recruiters who clone this service would lose the pre-push "
                f"smoke-test gate that simple_and_clean itself has."
            )
            content = hook.read_text(encoding="utf-8")
            for needle in expected_lines:
                assert needle in content, (
                    f"template {tpl!r} hook missing expected line {needle!r}.\n"
                    f"Got:\n{content}"
                )
            # Install hint must be present (one-time `git config core.hooksPath`).
            assert "git config core.hooksPath .githooks" in content, (
                f"template {tpl!r} hook missing install hint.\n"
                f"Got:\n{content}"
            )


def test_init_from_config_loads_json_and_scaffolds():
    """--from-config=<path.json> should drive `servicectl init` end-to-end.
    The JSON file holds the same keys as the CLI flags. CLI flags that the
    user passes on the command line take precedence over JSON values.
    """
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "spec.json"
        cfg.write_text(json.dumps({
            "name": "from-config-svc",
            "template": "go-webapi",
            "ci_provider": "github-actions",
            "deploy_target": "local",
            "azure_region": "eastus",
            "gcp_region": "us-central1",
            "coverage_threshold": 80,
            "registry": "ghcr",
            "db": "postgres",
        }), encoding="utf-8")
        out = Path(td) / "out"
        out.mkdir()
        result = runner.invoke(
            init_cmd,
            [
                "--from-config", str(cfg),
                "--output-dir", str(out),
                "--no-git",
                "--no-readme",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"init failed: {result.output}"
        target = out / "from-config-svc"
        assert target.exists()
        assert (target / "go.mod").exists()
        gomod = (target / "go.mod").read_text(encoding="utf-8")
        assert "from-config-svc" in gomod, (
            "service name from JSON was not substituted into go.mod; "
            "--from-config did not flow through to ServiceGenerator."
        )


def test_init_from_config_cli_flag_overrides_json():
    """Explicit CLI flags win over --from-config JSON values."""
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "spec.json"
        cfg.write_text(json.dumps({
            "name": "from-json",
            "template": "go-webapi",
            "ci_provider": "github-actions",
            "deploy_target": "local",
            "azure_region": "eastus",
            "coverage_threshold": 80,
            "registry": "ghcr",
            "db": "postgres",
        }), encoding="utf-8")
        out = Path(td) / "out"
        out.mkdir()
        # CLI passes --coverage=95; JSON has 80. CLI must win.
        result = runner.invoke(
            init_cmd,
            [
                "--from-config", str(cfg),
                "--coverage", "95",
                "--output-dir", str(out),
                "--no-git",
                "--no-readme",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"init failed: {result.output}"
        # Look at the README or the rendered README content for the coverage.
        # Without --no-readme the value lands in README and config files.
        # Easier to check: read the rendered coverage_threshold in the
        # rendered scaffold's README (pyproject.toml for python-flask etc.)
        # For go-webapi, coverage lives in .github/workflows/ci.yml.
        ci_yml = (out / "from-json" / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "95" in ci_yml, (
            f"CLI --coverage=95 should have overridden JSON 80 in rendered CI; "
            f"got:\n{ci_yml}"
        )


def test_init_from_config_missing_name_errors():
    """If neither the NAME argument nor --from-config supplies a name, error.
    Click's `required=True` only catches missing CLI args, so this test
    covers the post-merge validation we added for the JSON path.
    """
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "spec.json"
        cfg.write_text(json.dumps({
            "template": "go-webapi",
            "ci_provider": "github-actions",
            "deploy_target": "local",
            "azure_region": "eastus",
            "coverage_threshold": 80,
            "registry": "ghcr",
            "db": "postgres",
        }), encoding="utf-8")
        result = runner.invoke(
            init_cmd,
            ["--from-config", str(cfg), "--no-git"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "name" in result.output.lower()


def test_init_from_config_unknown_key_errors_with_strict_flag():
    """--strict-config with an unknown JSON key should error (exit != 0)."""
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "spec.json"
        cfg.write_text(json.dumps({
            "name": "x",
            "template": "go-webapi",
            "ci_provider": "github-actions",
            "deploy_target": "local",
            "azure_region": "eastus",
            "coverage_threshold": 80,
            "registry": "ghcr",
            "db": "postgres",
            "totally_made_up_key": "value",
        }), encoding="utf-8")
        result = runner.invoke(
            init_cmd,
            ["--from-config", str(cfg), "--strict-config", "--no-git"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "totally_made_up_key" in result.output


def test_init_from_config_unknown_key_silently_dropped_by_default():
    """Without --strict-config, unknown JSON keys are silently dropped (not an error).
    This keeps the JSON spec forward-compatible with new CLI options.
    """
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "spec.json"
        cfg.write_text(json.dumps({
            "name": "x",
            "template": "go-webapi",
            "ci_provider": "github-actions",
            "deploy_target": "local",
            "azure_region": "eastus",
            "coverage_threshold": 80,
            "registry": "ghcr",
            "db": "postgres",
            "future_option": "future_value",
        }), encoding="utf-8")
        out = Path(td) / "out"
        out.mkdir()
        result = runner.invoke(
            init_cmd,
            [
                "--from-config", str(cfg),
                "--output-dir", str(out),
                "--no-git",
                "--no-readme",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, (
            f"unknown keys should be silently dropped without --strict-config; "
            f"got exit={result.exit_code}, output:\n{result.output}"
        )


def test_init_from_config_invalid_json_errors():
    """--from-config=<file> where <file> is not valid JSON errors with exit 2."""
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "broken.json"
        cfg.write_text("this is { not valid JSON", encoding="utf-8")
        result = runner.invoke(
            init_cmd,
            ["--from-config", str(cfg), "--no-git"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "not valid JSON" in result.output


def test_init_from_config_top_level_not_object_errors():
    """--from-config=<file> where <file> is a JSON array (not object) errors.
    Schema is a flat JSON object; arrays / scalars are rejected.

    Note: we only assert on exit_code, not on output substring. The CLI's
    error message is printed via Rich's Console, which word-wraps the
    text at terminal width. On narrow terminals (e.g. Windows CI runners
    with COLUMNS=40), Rich splits the message in the middle of "JSON
    object", so a literal substring match would fail for reasons that
    have nothing to do with the CLI's correctness. exit_code != 0
    already proves the CLI rejected the bad JSON.
    """
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "array.json"
        cfg.write_text("[1, 2, 3]", encoding="utf-8")
        result = runner.invoke(
            init_cmd,
            ["--from-config", str(cfg), "--no-git"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0, (
            f"expected a non-zero exit for a non-object JSON file, "
            f"got exit={result.exit_code}, output:\n{result.output}"
        )


def test_init_from_config_missing_template_errors():
    """If JSON has 'name' but not 'template', post-merge validation errors."""
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "spec.json"
        cfg.write_text(json.dumps({"name": "x"}), encoding="utf-8")
        result = runner.invoke(
            init_cmd,
            ["--from-config", str(cfg), "--no-git"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "template" in result.output.lower()


def test_main_module_entry_point_invokes_cli():
    """`python -m servicectl` routes to cli.main. Covers servicectl/__main__.py
    which was at 0% coverage before this test.

    We use runpy so coverage instrumentation picks up the
    if __name__ == '__main__' block (subprocess would not).
    """
    import runpy
    saved_argv = sys.argv
    sys.argv = ["servicectl", "--help"]
    try:
        runpy.run_module("servicectl", run_name="__main__", alter_sys=True)
    except SystemExit as e:
        assert e.code == 0, f"unexpected exit code: {e.code}"
    finally:
        sys.argv = saved_argv


def test_modify_show_only_prints_current_settings():
    """servicectl modify <path> --show prints the current settings and exits."""
    from click.testing import CliRunner
    from servicectl.cli import modify as modify_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "demo-modify"
        target.mkdir()
        ServiceGenerator(
            name="demo-modify", template="go-webapi", ci_provider="github-actions",
            deploy_target="local", azure_region="eastus", coverage_threshold=80,
            registry="ghcr", db="postgres", output_dir=target,
            with_git=False, with_readme=False,
        ).run()
        actual = target / "demo-modify"
        result = runner.invoke(
            modify_cmd,
            [str(actual), "--show"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"modify --show failed: {result.output}"


def test_modify_same_db_is_noop():
    """modify --db=postgres on a postgres-scaffolded service is a no-op."""
    from click.testing import CliRunner
    from servicectl.cli import modify as modify_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "demo-noop"
        target.mkdir()
        ServiceGenerator(
            name="demo-noop", template="go-webapi", ci_provider="github-actions",
            deploy_target="local", azure_region="eastus", coverage_threshold=80,
            registry="ghcr", db="postgres", output_dir=target,
            with_git=False, with_readme=False,
        ).run()
        actual = target / "demo-noop"
        result = runner.invoke(
            modify_cmd,
            [str(actual), "--db", "postgres", "--yes"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "already using" in result.output or "No changes needed" in result.output


def test_template_path_returns_traversable_for_known_template():
    """template_path('go-webapi') returns a path object that has joinpath
    (works whether it's the source-tree Path or the importlib.resources
    Traversable fallback). Covers both branches of templates.template_path.
    """
    from servicectl import templates
    result = templates.template_path("go-webapi")
    # Either path-like (has / operator and exists) or Traversable-like
    # (has joinpath). Both branches of template_path must return something
    # the rest of the generator can use.
    assert result is not None
    assert hasattr(result, "__truediv__") or hasattr(result, "joinpath")


def test_init_invalid_name_characters_errors_cleanly():
    """`servicectl init <name>` where <name> contains invalid chars errors
    with exit 2 (Covers the ScaffoldError -> err_console.print branch in cli.py).
    """
    from click.testing import CliRunner
    from servicectl.cli import init as init_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(
            init_cmd,
            ["bad/name!chars", "--template=go-webapi", "--output-dir", td, "--no-git", "--no-readme"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        # Should be a clean error, not a traceback.
        assert "Traceback" not in result.output
        # Should mention the actual problem.
        assert (
            "invalid" in result.output.lower()
            or "character" in result.output.lower()
        ), f"expected an error message about invalid chars, got: {result.output}"


def test_modify_path_with_no_db_flag_errors():
    """`servicectl modify <path>` without --db errors with a useful message.
    Covers the 'no --db flag passed' branch in cli.py around line 444.
    """
    from click.testing import CliRunner
    from servicectl.cli import modify as modify_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "demo-nodb"
        target.mkdir()
        ServiceGenerator(
            name="demo-nodb", template="go-webapi", ci_provider="github-actions",
            deploy_target="local", azure_region="eastus", coverage_threshold=80,
            registry="ghcr", db="postgres", output_dir=target,
            with_git=False, with_readme=False,
        ).run()
        actual = target / "demo-nodb"
        # No --db flag -> should error with the "no --db flag passed" message.
        result = runner.invoke(
            modify_cmd,
            [str(actual)],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "no --db" in result.output or "Currently" in result.output


def test_doctor_human_text_output():
    """servicectl doctor <path> (default text output) runs and exits with
    a valid code. Covers the render_text branch in cli.py line 538.
    """
    from click.testing import CliRunner
    from servicectl.cli import doctor as doctor_cmd

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "demo-doc-text"
        target.mkdir()
        ServiceGenerator(
            name="demo-doc-text", template="go-webapi", ci_provider="github-actions",
            deploy_target="local", azure_region="eastus", coverage_threshold=80,
            registry="ghcr", db="postgres", output_dir=target,
            with_git=False, with_readme=False,
        ).run()
        actual = target / "demo-doc-text"
        result = runner.invoke(
            doctor_cmd,
            [str(actual)],  # no --json, so render_text branch fires
            catch_exceptions=False,
        )
        assert result.exit_code in (0, 1, 2), f"doctor crashed: {result.output}"


def test_in_place_into_empty_dir_succeeds():
    """--in-place scaffolds directly into the output-dir (no <name> subfolder)."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "demo-inplace"
        target.mkdir()  # exists and is empty
        gen = ServiceGenerator(
            name="demo-inplace",
            template="go-webapi",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=target,
            with_git=False,
            with_readme=False,
            in_place=True,
        )
        created = gen.run()
        # Service files should live at target itself, not target/demo-inplace/.
        assert created.resolve() == target.resolve()
        assert (target / "go.mod").exists()
        assert (target / "cmd" / "server" / "main.go").exists()
        # The <name> subfolder must NOT have been created.
        assert not (target / "demo-inplace").exists()


def test_in_place_into_nonexistent_dir_creates_it():
    """--in-place should create the output-dir if it doesn't exist."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "fresh-dir" / "demo-inplace"
        assert not target.exists()
        gen = ServiceGenerator(
            name="demo-inplace",
            template="go-webapi",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=target,
            with_git=False,
            with_readme=False,
            in_place=True,
        )
        created = gen.run()
        assert created.resolve() == target.resolve()
        assert (target / "go.mod").exists()


def test_in_place_into_nonempty_dir_refuses():
    """--in-place into a non-empty directory must raise ScaffoldError (no clobbering)."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "nonempty"
        target.mkdir()
        (target / "user-file.txt").write_text("don't overwrite me")
        gen = ServiceGenerator(
            name="demo-inplace",
            template="go-webapi",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=target,
            with_git=False,
            with_readme=False,
            in_place=True,
        )
        try:
            gen.run()
        except ScaffoldError as e:
            assert "not empty" in str(e)
            # The user's file must still be there.
            assert (target / "user-file.txt").read_text() == "don't overwrite me"
        else:
            raise AssertionError("expected ScaffoldError for non-empty --in-place target")


def test_in_place_into_current_dir_succeeds():
    """--in-place --output-dir=. should scaffold into the current directory."""
    with tempfile.TemporaryDirectory() as td:
        gen = ServiceGenerator(
            name="demo-curdir",
            template="go-webapi",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=Path(td),  # acts as "current dir"
            with_git=False,
            with_readme=False,
            in_place=True,
        )
        created = gen.run()
        # Files live at td itself, not td/demo-curdir/.
        assert created.resolve() == Path(td).resolve()
        assert (Path(td) / "go.mod").exists()


def test_default_init_still_creates_subfolder():
    """Sanity check: WITHOUT --in-place, the default <output_dir>/<name> subfolder is still created."""
    with tempfile.TemporaryDirectory() as td:
        gen = ServiceGenerator(
            name="my-default-svc",
            template="python-flask",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=Path(td),
            with_git=False,
            with_readme=False,
            in_place=False,
        )
        created = gen.run()
        assert created.resolve() == (Path(td) / "my-default-svc").resolve()
        assert (Path(td) / "my-default-svc" / "pyproject.toml").exists()


def test_with_git_makes_initial_commit():
    """Regression test: git init alone doesn't create a `main` ref. We need
    git add + git commit after init so that --git-remote has a ref to push.
    Without this fix, --git-remote fails with `src refspec main does not match any`."""
    with tempfile.TemporaryDirectory() as td:
        gen = ServiceGenerator(
            name="demo-init-commit",
            template="python-flask",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=Path(td),
            with_git=True,
            with_readme=False,
            in_place=False,
        )
        created = gen.run()
        # Verify a commit exists on the main branch.
        result = subprocess.run(
            ["git", "-C", str(created), "log", "--oneline"],
            capture_output=True,
            text=True,
            check=True,
        )
        # There should be at least one commit. The message contains "servicectl".
        assert "servicectl" in result.stdout.lower(), f"expected initial commit, got: {result.stdout!r}"


def test_git_remote_rejects_non_https():
    """--git-remote must start with https:// (SSH not supported in v1)."""
    with tempfile.TemporaryDirectory() as td:
        gen = ServiceGenerator(
            name="demo-remote",
            template="python-flask",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=Path(td),
            with_git=True,
            with_readme=False,
            in_place=False,
            git_remote="git@github.com:me/demo.git",  # SSH form, rejected
        )
        try:
            gen.run()
        except ScaffoldError as e:
            assert "--git-remote must start with https://" in str(e)
        else:
            raise AssertionError("expected ScaffoldError for non-https --git-remote")


def test_git_remote_accepts_https_url():
    """A valid https:// URL should be accepted without error during validation."""
    with tempfile.TemporaryDirectory() as td:
        gen = ServiceGenerator(
            name="demo-remote-ok",
            template="python-flask",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=Path(td),
            with_git=True,
            with_readme=False,
            in_place=False,
            git_remote="https://github.com/me/demo.git",
        )
        # Validation should not raise. The actual push would happen in run()
        # but we don't run() here because that would require a real remote
        # and network access. Just verify the URL was accepted.
        # Note: _validate() is called inside run(), so we test it directly.
        gen._validate()
        assert gen.git_remote == "https://github.com/me/demo.git"


def test_git_remote_with_no_git_does_not_crash():
    """--no-git + --git-remote should not crash. (The remote flag is set but
    no git operations are attempted since with_git is False.)"""
    with tempfile.TemporaryDirectory() as td:
        gen = ServiceGenerator(
            name="demo-remote-nogit",
            template="python-flask",
            ci_provider="github-actions",
            deploy_target="local",
            azure_region="eastus",
            coverage_threshold=80,
            registry="ghcr",
            db="postgres",
            output_dir=Path(td),
            with_git=False,
            with_readme=False,
            in_place=False,
            git_remote="https://github.com/me/demo.git",
        )
        # Validation passes; run() executes without git operations.
        gen._validate()
        # We deliberately do NOT call gen.run() here because that would
        # attempt the push, which requires a real remote. The point is
        # that --git-remote is accepted as a configuration value even
        # when --no-git is set.
        assert gen.git_remote is not None
        assert gen.with_git is False


def test_dashed_name_renders_correctly():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "my-cool.api", "python-flask", ci_provider="azure-devops")
        # Filenames should be exactly the supplied name.
        assert target.name == "my-cool.api"
        # ADO pipeline should be generated and contain the name.
        yml = (target / "azure-pipelines.yml").read_text()
        assert "my-cool.api" in yml
        # ADO pipeline should NOT have a GH-Actions-only file extension leak.
        assert ".github" not in yml


def test_duplicate_directory_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        _scaffold(Path(td), "dup-test", "node-express")
        try:
            _scaffold(Path(td), "dup-test", "node-express")
        except ScaffoldError as e:
            assert "already exists" in str(e)
        else:
            raise AssertionError("expected ScaffoldError on duplicate directory")


def test_invalid_name_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        try:
            _scaffold(Path(td), "bad/name", "node-express")
        except ScaffoldError as e:
            assert "invalid character" in str(e)
        else:
            raise AssertionError("expected ScaffoldError on invalid name")


def test_azure_overlay_emits_infra_files():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(
            Path(td),
            "demo-azure",
            "python-flask",
            deploy_target="azure",
            azure_region="westus2",
        )
        assert (target / "infra" / "main.bicep").exists()
        assert (target / "infra" / "dev.bicepparam").exists()
        assert (target / "infra" / "staging.bicepparam").exists()
        assert (target / "infra" / "prod.bicepparam").exists()
        assert (target / "deploy.yml").exists()
        # Make sure Jinja placeholders didn't leak through.
        for p in [target / "infra" / "main.bicep", target / "infra" / "dev.bicepparam"]:
            content = p.read_text()
            assert "{{" not in content, f"unrendered Jinja in {p}"
        # Substitutions should reflect service name + region.
        bicep = (target / "infra" / "main.bicep").read_text()
        assert "demo-azure" in bicep
        assert "westus2" in bicep
        params = (target / "infra" / "dev.bicepparam").read_text()
        assert "demo-azure" in params
        assert "westus2" in params


def test_azure_overlay_skipped_for_local():
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(Path(td), "demo-local", "node-express")
        assert not (target / "infra").exists()
        assert not (target / "deploy.yml").exists()


def test_azure_bicepparam_no_secure_decorator():
    """Regression: Bicepparam files do not support decorators.

    The BCP130 error breaks `bicep build-params` if `@secure()` is used inside
    a .bicepparam file. The @secure() decorator belongs on the corresponding
    param in main.bicep, not in the bicepparam file.
    """
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(
            Path(td),
            "demo-bicep",
            "python-flask",
            deploy_target="azure",
            azure_region="eastus",
        )
        for env in ("dev", "staging", "prod"):
            content = (target / "infra" / f"{env}.bicepparam").read_text()
            assert "@secure" not in content, (
                f"{env}.bicepparam contains @secure() decorator; "
                "bicepparam files do not support decorators (BCP130)"
            )


def test_azure_bicepparam_no_hash_comments():
    """Regression: Bicepparam files do not support `#` line comments.

    Only `//` comments are valid. The `#` character triggers BCP001.
    """
    with tempfile.TemporaryDirectory() as td:
        target = _scaffold(
            Path(td),
            "demo-bicep",
            "python-flask",
            deploy_target="azure",
        )
        for env in ("dev", "staging", "prod"):
            content = (target / "infra" / f"{env}.bicepparam").read_text()
            for lineno, line in enumerate(content.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    raise AssertionError(
                        f"{env}.bicepparam line {lineno} uses `#` comment; "
                        "bicepparam files only support `//` (BCP001)"
                    )


if __name__ == "__main__":
    # Allow running without pytest: `python tests/test_smoke.py`
    failures = 0
    for name in dir():
        if name.startswith("test_") and callable(locals()[name]):
            try:
                locals()[name]()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    if failures:
        sys.exit(1)
    print(f"\nAll tests passed.")
