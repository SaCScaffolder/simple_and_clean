"""Tests for the go-webapi template's /notes handler and migration
runner (PR 5 of the GKE Autopilot + Cloud SQL milestone).

Scope (PR 5):

- The Go template must:
  - Add a /notes endpoint with POST (create) and GET (list) when
    --db=postgres. The handlers use *sql.DB via the pgx driver.
  - Add a /readyz handler that pings the DB when one is
    configured, returning 503 if the DB is unreachable.
  - Add a `migrate` subcommand to the binary that opens a
    *sql.DB against $DATABASE_URL and runs the embedded
    migrations.
  - Include a `migrations/001_notes.sql` file that creates the
    `notes` table and an index.
  - Add `github.com/jackc/pgx/v5` to go.mod when --db=postgres.
  - Skip all of the above when --db != postgres.

Tests exercise the real `ServiceGenerator.run()` codepath the
scaffolder uses at runtime, then read the rendered Go files
directly. We don't compile or run the Go code here -- the
CI matrix (Go 1.22 / 1.27 on ubuntu + windows) is the
source of truth for that. These tests catch:
  - missing handlers or methods
  - wrong conditional gating (postgres-only code in the
    no-postgres render)
  - migrations file being skipped on the no-postgres path
  - go.mod missing the pgx dependency

Run with: pytest tests/test_go_webapi_notes.py -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.generator import ServiceGenerator  # noqa: E402


@pytest.fixture
def scaffold_target(request):
    """Yield a Path for the scaffold output. Cleaned up after the
    test. Uses mkdtemp + manual cleanup so the returned path is
    valid for the entire test (a `with TemporaryDirectory()` block
    would clean up before the test sees the path)."""
    tmp = Path(tempfile.mkdtemp(prefix="servicectl-test-"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _scaffold(scaffold_target: Path, db: str) -> Path:
    gen = ServiceGenerator(
        name="demo",
        template="go-webapi",
        ci_provider="github-actions",
        deploy_target="gcp-gke-autopilot",
        azure_region="eastus",
        coverage_threshold=80,
        registry="gar",
        db=db,
        output_dir=scaffold_target,
        with_git=False,
        with_readme=True,
        gcp_region="us-central1",
        gcp_project_id="sacscaffolder",
    )
    return gen.run()


class TestPostgresTemplateRendersNotes:
    """The postgres render includes the /notes handlers and migration logic."""

    def test_migrations_dir_exists(self, scaffold_target):
        t = _scaffold(scaffold_target, "postgres")
        assert (t / "migrations").is_dir()
        assert (t / "migrations" / "001_notes.sql").is_file()

    def test_migrations_file_has_create_table(self, scaffold_target):
        t = _scaffold(scaffold_target, "postgres")
        text = (t / "migrations" / "001_notes.sql").read_text()
        assert "CREATE TABLE" in text
        assert "notes" in text
        # Idempotent: every CREATE must use IF NOT EXISTS.
        assert "IF NOT EXISTS" in text

    def test_server_go_has_notes_handlers(self, scaffold_target):
        t = _scaffold(scaffold_target, "postgres")
        text = (t / "internal" / "demo" / "server.go").read_text()
        # The /notes handler
        assert 'mux.HandleFunc("/notes"' in text
        # The POST/GET dispatcher
        assert "createNote" in text
        assert "listNotes" in text
        # The DB-INSERT and DB-SELECT queries
        assert "INSERT INTO notes" in text
        assert "SELECT" in text and "FROM notes" in text

    def test_server_go_has_run_migrations(self, scaffold_target):
        t = _scaffold(scaffold_target, "postgres")
        text = (t / "internal" / "demo" / "server.go").read_text()
        assert "func RunMigrations" in text
        # The migration SQL is embedded as a Go const, not loaded
        # from disk at runtime (so the binary is self-contained).
        assert "migration001Notes" in text
        assert "CREATE TABLE IF NOT EXISTS notes" in text

    def test_server_go_readyz_pings_db(self, scaffold_target):
        """The /readyz handler must do a real DB ping when a DB
        is configured, not just return 200."""
        t = _scaffold(scaffold_target, "postgres")
        text = (t / "internal" / "demo" / "server.go").read_text()
        assert "PingContext" in text
        # The 503 path is the meaningful one -- without it, a
        # broken DB would silently pass the readiness check.
        assert "StatusServiceUnavailable" in text

    def test_main_go_has_migrate_subcommand(self, scaffold_target):
        t = _scaffold(scaffold_target, "postgres")
        text = (t / "cmd" / "server" / "main.go").read_text()
        assert '"migrate"' in text
        assert "runMigrate" in text
        # The migrate subcommand must call into the package's
        # RunMigrations (not a no-op).
        assert ".RunMigrations" in text

    def test_main_go_opens_db_from_database_url(self, scaffold_target):
        t = _scaffold(scaffold_target, "postgres")
        text = (t / "cmd" / "server" / "main.go").read_text()
        # The server binary should open a *sql.DB from $DATABASE_URL.
        assert "DATABASE_URL" in text
        # The pgx driver must be registered as the database/sql driver.
        assert "pgx" in text

    def test_go_mod_has_pgx(self, scaffold_target):
        t = _scaffold(scaffold_target, "postgres")
        text = (t / "go.mod").read_text()
        assert "github.com/jackc/pgx" in text


class TestNonPostgresTemplateSkipsNotes:
    """A non-postgres render must NOT include the /notes handlers or migration code."""

    def test_no_migrations_dir(self, scaffold_target):
        t = _scaffold(scaffold_target, "mysql")
        # The empty-render-skip means the file isn't written, and
        # the parent dir isn't created. So migrations/ should
        # not exist at all.
        assert not (t / "migrations").exists()

    def test_server_go_omits_notes_handlers(self, scaffold_target):
        t = _scaffold(scaffold_target, "mysql")
        text = (t / "internal" / "demo" / "server.go").read_text()
        # The handlers themselves should not be present.
        assert "createNote" not in text
        assert "listNotes" not in text
        assert "RunMigrations" not in text
        # The mux registration is gated on db == 'postgres'.
        assert 'mux.HandleFunc("/notes"' not in text

    def test_main_go_skips_migrate_subcommand(self, scaffold_target):
        sub = scaffold_target / "mysql"
        sub.mkdir(parents=True, exist_ok=True)
        t = _scaffold(sub, "mysql")
        text = (t / "cmd" / "server" / "main.go").read_text()
        # The migrate path's call into RunMigrations must not be
        # there for non-postgres. (The "migrate" subcommand
        # string itself is fine to mention in comments, but the
        # call site `.RunMigrations` should be absent.)
        assert ".RunMigrations" not in text
        # And the pgx / database/sql imports must be absent too --
        # otherwise go.mod tidy would pull them in. The earlier
        # bug (2026-10-18) was that pgx was unconditionally
        # imported, breaking go mod tidy for the mysql render.
        assert "pgx" not in text
        assert "database/sql" not in text

    def test_go_mod_omits_pgx(self, scaffold_target):
        t = _scaffold(scaffold_target, "mysql")
        text = (t / "go.mod").read_text()
        assert "pgx" not in text


class TestServerScaffoldConsistency:
    """The postgres and non-postgres renders should be consistent
    in their handling of NewServer, NewMux, and the no-DB path."""

    def test_newserver_takes_sql_db(self, scaffold_target):
        """NewServer's signature should be `func NewServer(*sql.DB) *Server`
        for both the postgres and non-postgres renders. The
        difference is what gets passed in (a real *sql.DB vs nil)."""
        for db in ("postgres", "mysql"):
            # Each db render needs its own subdir so they don't
            # collide on `target directory already exists`.
            sub = scaffold_target / db
            sub.mkdir(parents=True, exist_ok=True)
            t = _scaffold(sub, db)
            text = (t / "internal" / "demo" / "server.go").read_text()
            assert "func NewServer(db *sql.DB)" in text, (
                f"NewServer signature changed in {db} render"
            )

    def test_newmux_returns_no_db_server(self, scaffold_target):
        """NewMux() must return a server with no DB (so the
        scaffold's no-DB smoke test in main_test.go keeps working)."""
        for db in ("postgres", "mysql"):
            sub = scaffold_target / db
            sub.mkdir(parents=True, exist_ok=True)
            t = _scaffold(sub, db)
            text = (t / "internal" / "demo" / "server.go").read_text()
            assert "func NewMux()" in text
            # NewMux's body should call NewServer(nil) -- no DB.
            assert "NewServer(nil)" in text


class TestPostgresTestFileCoversCodePaths:
    """The scaffolded server_test.go and main_test.go must include
    Go tests that exercise the postgres code paths. Without these
    tests, the postgres render's coverage drops to ~22% on
    internal/<svc> and ~43% on cmd/server -- well below the 80%
    coverage gate.

    Regression guard for the failure on PR #139's integration test
    (run 39173691560, 2026-10-18): the postgres code paths were
    implemented but not tested, and the per-PR CI's
    `go test -cover` step failed the 80% gate at 31.7% total
    coverage.
    """

    def test_postgres_server_test_has_readyz_with_db(self, scaffold_target):
        """The postgres render of server_test.go must include
        TestReadyzWithDB, which exercises the /readyz ping path
        against a real DB."""
        t = _scaffold(scaffold_target, db="postgres")
        text = (t / "internal" / "demo" / "server_test.go").read_text()
        assert "TestReadyzWithDB" in text, (
            "postgres server_test.go missing TestReadyzWithDB -- "
            "the /readyz ping path will be uncovered and the "
            "coverage gate will fail"
        )

    def test_postgres_server_test_has_notes_round_trip(self, scaffold_target):
        """The postgres render of server_test.go must include
        TestNotesPostgresRoundTrip, which exercises the createNote
        + listNotes success paths end-to-end."""
        t = _scaffold(scaffold_target, db="postgres")
        text = (t / "internal" / "demo" / "server_test.go").read_text()
        assert "TestNotesPostgresRoundTrip" in text, (
            "postgres server_test.go missing TestNotesPostgresRoundTrip -- "
            "the /notes handlers will be uncovered and the "
            "coverage gate will fail"
        )

    def test_postgres_server_test_has_notes_validation(self, scaffold_target):
        """The postgres render of server_test.go must include the
        400-paths for POST /notes (empty body, too long, malformed
        JSON). These cover the validation branches of createNote
        that don't need a real DB but aren't hit by the round-trip
        test."""
        t = _scaffold(scaffold_target, db="postgres")
        text = (t / "internal" / "demo" / "server_test.go").read_text()
        assert "TestNotesPostgresValidation" in text, (
            "postgres server_test.go missing TestNotesPostgresValidation"
        )

    def test_postgres_server_test_has_notes_method_not_allowed(self, scaffold_target):
        """The postgres render must include the 405-path test
        (PUT/DELETE/PATCH on /notes)."""
        t = _scaffold(scaffold_target, db="postgres")
        text = (t / "internal" / "demo" / "server_test.go").read_text()
        assert "TestNotesPostgresMethodNotAllowed" in text, (
            "postgres server_test.go missing TestNotesPostgresMethodNotAllowed"
        )

    def test_postgres_server_test_has_run_migrations(self, scaffold_target):
        """The postgres render must include TestRunMigrations, which
        calls RunMigrations against a real DB and asserts the notes
        table + index are created. Without this, the migration SQL
        is uncovered."""
        t = _scaffold(scaffold_target, db="postgres")
        text = (t / "internal" / "demo" / "server_test.go").read_text()
        assert "TestRunMigrations" in text, (
            "postgres server_test.go missing TestRunMigrations"
        )

    def test_postgres_main_test_has_migrate_subcommand(self, scaffold_target):
        """The postgres render of main_test.go must include the
        TestRunMigrate* tests, which exercise the `migrate`
        subcommand's wiring (DATABASE_URL read, sql.Open,
        PingContext, RunMigrations call)."""
        t = _scaffold(scaffold_target, db="postgres")
        text = (t / "cmd" / "server" / "main_test.go").read_text()
        assert "TestRunMigrateSuccess" in text, (
            "postgres main_test.go missing TestRunMigrateSuccess"
        )
        assert "TestRunMigrateNoDatabaseURL" in text, (
            "postgres main_test.go missing TestRunMigrateNoDatabaseURL"
        )
        assert "TestRunMigrateBadDSN" in text, (
            "postgres main_test.go missing TestRunMigrateBadDSN"
        )

    def test_non_postgres_server_test_omits_db_aware_tests(self, scaffold_target):
        """The non-postgres render of server_test.go must NOT
        include the postgres-aware tests (they would dangle --
        referencing `sql.DB` and `pgx` in a scaffold that doesn't
        import them). The non-postgres render keeps just the
        smoke tests (TestHealthz, TestReadyz, TestIndex,
        TestRoutesExist)."""
        import re
        t = _scaffold(scaffold_target, db="mysql")
        text = (t / "internal" / "demo" / "server_test.go").read_text()
        # Postgres-only test functions should not appear in the
        # mysql render. We use a regex anchored on `func <Name>(`
        # to avoid false positives from comments that mention
        # the function name in prose.
        for fn in (
            "TestReadyzWithDB",
            "TestNotesPostgresRoundTrip",
            "TestNotesPostgresValidation",
            "TestNotesPostgresMethodNotAllowed",
            "TestRunMigrations",
        ):
            pattern = re.compile(rf"\bfunc {re.escape(fn)}\b")
            assert not pattern.search(text), (
                f"non-postgres server_test.go should not define {fn} "
                f"(no pgx / sql.DB in the mysql render)"
            )

    def test_non_postgres_main_test_omits_migrate_tests(self, scaffold_target):
        """The non-postgres render of main_test.go must NOT include
        the migrate-subcommand tests (runMigrate is a no-op stub
        on the non-postgres path and the tests would not run)."""
        t = _scaffold(scaffold_target, db="mysql")
        text = (t / "cmd" / "server" / "main_test.go").read_text()
        for fn in (
            "TestRunMigrateSuccess",
            "TestRunMigrateNoDatabaseURL",
            "TestRunMigrateBadDSN",
        ):
            assert fn not in text, (
                f"non-postgres main_test.go should not contain {fn}"
            )
