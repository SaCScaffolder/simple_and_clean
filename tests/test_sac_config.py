"""Tests for sac_config (.servicectl.json read/write)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from servicectl.sac_config import (  # noqa: E402
    CONFIG_FILENAME,
    SacConfig,
    read_config,
    resolve_sac_base_commit,
    write_config,
)


class SacConfigDataclassTests(unittest.TestCase):
    def test_defaults(self):
        c = SacConfig()
        self.assertEqual(c.schema_version, 1)
        self.assertEqual(c.template, "python-flask")
        self.assertEqual(c.ci_provider, "github-actions")
        self.assertEqual(c.deploy_target, "local")
        self.assertEqual(c.coverage_threshold, 80)
        self.assertEqual(c.registry, "ghcr")
        self.assertEqual(c.db, "postgres")

    def test_to_generator_kwargs_excludes_bookkeeping(self):
        c = SacConfig(template="go-webapi", deploy_target="azure", sac_version="0.1.0")
        kwargs = c.to_generator_kwargs()
        # Bookkeeping fields must not leak into ServiceGenerator construction.
        self.assertNotIn("schema_version", kwargs)
        self.assertNotIn("sac_version", kwargs)
        # Real config fields must be there.
        self.assertEqual(kwargs["template"], "go-webapi")
        self.assertEqual(kwargs["deploy_target"], "azure")

    def test_from_generator_factory(self):
        c = SacConfig.from_generator(
            template="node-react-web",
            ci_provider="azure-devops",
            deploy_target="gcp-cloud-run",
            azure_region="westus2",
            gcp_region="europe-west1",
            gcp_project_id="my-gcp-proj",
            coverage_threshold=85,
            registry="gar",
            db="cosmosdb",
            sac_version="0.1.0",
        )
        self.assertEqual(c.template, "node-react-web")
        self.assertEqual(c.deploy_target, "gcp-cloud-run")
        self.assertEqual(c.gcp_project_id, "my-gcp-proj")
        self.assertEqual(c.coverage_threshold, 85)
        self.assertEqual(c.registry, "gar")
        self.assertEqual(c.db, "cosmosdb")
        self.assertEqual(c.sac_version, "0.1.0")

    def test_round_trip_via_dict(self):
        """from_dict(to_dict()) yields an equivalent config (excluding bookkeeping)."""
        original = SacConfig(
            template="dotnet-webapi",
            ci_provider="github-actions",
            deploy_target="azure",
            azure_region="centralus",
            coverage_threshold=92,
            registry="acr",
            db="mssql",
            sac_version="0.2.0",
        )
        d = original.to_dict()
        restored = SacConfig.from_dict(d)
        self.assertEqual(restored.template, original.template)
        self.assertEqual(restored.deploy_target, original.deploy_target)
        self.assertEqual(restored.azure_region, original.azure_region)
        self.assertEqual(restored.coverage_threshold, original.coverage_threshold)
        self.assertEqual(restored.registry, original.registry)
        self.assertEqual(restored.db, original.db)

    def test_from_dict_drops_unknown_keys(self):
        """Unknown keys (forward-compat) are dropped silently."""
        d = {"template": "python-flask", "future_field_we_dont_know_about": "ignored"}
        c = SacConfig.from_dict(d)
        self.assertEqual(c.template, "python-flask")


class SacConfigFileIoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_round_trip(self):
        c = SacConfig(template="go-webapi", deploy_target="azure", coverage_threshold=85)
        path = write_config(self.dir, c)
        self.assertEqual(path.name, CONFIG_FILENAME)
        self.assertTrue(path.exists())
        # File content is sorted JSON.
        text = path.read_text()
        loaded = json.loads(text)
        self.assertEqual(loaded["template"], "go-webapi")
        self.assertEqual(loaded["coverage_threshold"], 85)
        # Sort by key so diffs stay stable across versions.
        keys = list(loaded.keys())
        self.assertEqual(keys, sorted(keys), f"keys not sorted: {keys}")
        # The bookkeeping fields must be present.
        self.assertIn("schema_version", loaded)
        self.assertIn("sac_version", loaded)

    def test_write_then_read_back_is_equal(self):
        c = SacConfig(template="dotnet-webapi", deploy_target="azure", coverage_threshold=92)
        write_config(self.dir, c)
        restored = read_config(self.dir)
        self.assertEqual(restored.template, c.template)
        self.assertEqual(restored.deploy_target, c.deploy_target)
        self.assertEqual(restored.coverage_threshold, c.coverage_threshold)

    def test_read_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            read_config(self.dir)

    def test_read_malformed_json_raises(self):
        (self.dir / CONFIG_FILENAME).write_text("not json{")
        with self.assertRaises(json.JSONDecodeError):
            read_config(self.dir)

    def test_read_top_level_array_rejected(self):
        """A list or scalar at the top level would crash the unpacker downstream."""
        (self.dir / CONFIG_FILENAME).write_text("[1, 2, 3]")
        with self.assertRaises(ValueError):
            read_config(self.dir)

    def test_write_returns_path(self):
        c = SacConfig()
        p = write_config(self.dir, c)
        self.assertEqual(p, self.dir / CONFIG_FILENAME)


class ResolveSacBaseCommitTests(unittest.TestCase):
    """Tests for resolve_sac_base_commit() — env var vs config field precedence."""

    def setUp(self):
        # Clean any inherited SAC_BASE_COMMIT so the tests are deterministic.
        self._saved = os.environ.pop("SAC_BASE_COMMIT", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["SAC_BASE_COMMIT"] = self._saved
        else:
            os.environ.pop("SAC_BASE_COMMIT", None)

    def test_returns_none_when_unset(self):
        """No env var, no config field -> None."""
        c = SacConfig()
        self.assertIsNone(resolve_sac_base_commit(c))

    def test_config_field_returns_when_set(self):
        """Config file field is the fallback when env var is absent."""
        c = SacConfig(sac_base_commit="abc1234")
        self.assertEqual(resolve_sac_base_commit(c), "abc1234")

    def test_env_var_overrides_config(self):
        """Env var wins. Useful for one-off refresh without editing config."""
        c = SacConfig(sac_base_commit="abc1234")
        os.environ["SAC_BASE_COMMIT"] = "deadbeef"
        self.assertEqual(resolve_sac_base_commit(c), "deadbeef")

    def test_env_var_only_no_config_field(self):
        """Env var works even if config has no sac_base_commit."""
        c = SacConfig()  # no field
        os.environ["SAC_BASE_COMMIT"] = "feedface"
        self.assertEqual(resolve_sac_base_commit(c), "feedface")

    def test_whitespace_stripped_from_env(self):
        """Trailing newline from `export SAC_BASE_COMMIT=...\\n` is fine."""
        c = SacConfig()
        os.environ["SAC_BASE_COMMIT"] = "  feedface  \n"
        self.assertEqual(resolve_sac_base_commit(c), "feedface")

    def test_whitespace_stripped_from_config(self):
        """The config field gets stripped too (defensive against JSON typos)."""
        c = SacConfig(sac_base_commit="  feedface  ")
        self.assertEqual(resolve_sac_base_commit(c), "feedface")

    def test_empty_string_treated_as_unset(self):
        """Empty env var value => None (don't return empty string to git)."""
        c = SacConfig()
        os.environ["SAC_BASE_COMMIT"] = ""
        self.assertIsNone(resolve_sac_base_commit(c))

    def test_empty_config_field_treated_as_unset(self):
        """Empty config field => None."""
        c = SacConfig(sac_base_commit="")
        self.assertIsNone(resolve_sac_base_commit(c))


if __name__ == "__main__":
    unittest.main()