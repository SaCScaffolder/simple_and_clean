"""Unit tests for the low-level GitHub API helpers in servicectl.github_api.

These tests cover the helper functions that are called by the CLI but
not by the integration test workflow (which goes through the CLI).
The high-level happy path of `--repo` is covered by
test_init_creates_repo.py.
"""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from servicectl import github_api


FAKE_REPO = {
    "id": 999,
    "name": "thing",
    "full_name": "SaCScaffolder/thing",
    "html_url": "https://github.com/SaCScaffolder/thing",
    "clone_url": "https://github.com/SaCScaffolder/thing.git",
    "private": False,
}


def _mock_response(status: int, body: str | dict = "{}") -> MagicMock:
    """Build a mock that mimics urllib's context-manager response."""
    body_str = body if isinstance(body, str) else json.dumps(body)
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body_str.encode("utf-8")
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda *a: False
    return resp


def test_create_org_repo_targets_org_endpoint():
    """create_org_repo hits POST /orgs/{org}/repos."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(201, FAKE_REPO)
        result = github_api.create_org_repo("SaCScaffolder", "thing", token="ghp_x")
    assert result["full_name"] == "SaCScaffolder/thing"
    call = mock_urlopen.call_args
    req = call.args[0]
    assert req.method == "POST"
    assert req.full_url == "https://api.github.com/orgs/SaCScaffolder/repos"
    body = json.loads(req.data.decode("utf-8"))
    assert body["name"] == "thing"
    assert body["private"] is False
    assert body["auto_init"] is False


def test_create_org_repo_includes_description_and_private():
    """Optional kwargs are forwarded to the API."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(201, FAKE_REPO)
        github_api.create_org_repo(
            "SaCScaffolder", "thing", token="ghp_x", private=True,
            description="my test repo",
        )
    req = mock_urlopen.call_args.args[0]
    body = json.loads(req.data.decode("utf-8"))
    assert body["private"] is True
    assert body["description"] == "my test repo"


def test_create_org_repo_raises_on_non_2xx():
    """A non-2xx response raises GitHubAPIError with the response body
    included so the CLI can surface a useful error message."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        # Build a fake HTTPError via the actual class -- it's the only
        # way to satisfy the constructor's many positional args.
        body = json.dumps({"message": "name already exists"}).encode("utf-8")
        err = urllib.error.HTTPError(
            url="https://api.github.com/orgs/SaCScaffolder/repos",
            code=422,
            msg="Unprocessable Entity",
            hdrs={"Content-Type": "application/json"},
            fp=__import__("io").BytesIO(body),
        )
        mock_urlopen.side_effect = err
        with pytest.raises(github_api.GitHubAPIError) as excinfo:
            github_api.create_org_repo("SaCScaffolder", "thing", token="ghp_x")
    assert excinfo.value.status == 422
    assert "name already exists" in excinfo.value.body


def test_create_user_repo_targets_user_endpoint():
    """create_user_repo hits POST /user/repos (not /users/{name}/repos)."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(201, FAKE_REPO)
        github_api.create_user_repo("anyone", "thing", token="ghp_x")
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == "https://api.github.com/user/repos"


def test_pick_create_endpoint_picks_org_for_lowercase_slug():
    """Lowercase slugs with hyphens/underscores/dots are org-shaped."""
    assert github_api._pick_create_endpoint("sa-cscaffolder", "https://api.github.com") == \
        "https://api.github.com/orgs/sa-cscaffolder/repos"
    assert github_api._pick_create_endpoint("acme.corp", "https://api.github.com") == \
        "https://api.github.com/orgs/acme.corp/repos"
    assert github_api._pick_create_endpoint("my_org", "https://api.github.com") == \
        "https://api.github.com/orgs/my_org/repos"


def test_pick_create_endpoint_picks_user_for_mixed_case():
    """Mixed-case (typical GitHub username) goes to /user/repos."""
    assert github_api._pick_create_endpoint("HenryOrsborn", "https://api.github.com") == \
        "https://api.github.com/user/repos"


def test_pick_create_endpoint_handles_empty_string():
    """Empty owner defaults to /user/repos (safer than guessing an org)."""
    assert github_api._pick_create_endpoint("", "https://api.github.com") == \
        "https://api.github.com/user/repos"


def test_create_repo_dispatches_based_on_owner_shape():
    """The high-level create_repo() picks the right endpoint based on the
    owner string. The integration test workflow should call
    create_org_repo() directly to bypass this heuristic, but the
    public create_repo() is convenient for ad-hoc use."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(201, FAKE_REPO)
        github_api.create_repo("acme-corp", "thing", token="ghp_x")
    req = mock_urlopen.call_args.args[0]
    assert "/orgs/acme-corp/repos" in req.full_url


def test_get_auth_token_from_gh_cli_returns_token():
    """When `gh auth token` succeeds, return its stdout."""
    import subprocess
    fake_proc = MagicMock()
    fake_proc.stdout = b"  ghp_fake_token_xxxxx\n"
    with patch("subprocess.run", return_value=fake_proc):
        tok = github_api.get_auth_token_from_gh_cli()
    assert tok == "ghp_fake_token_xxxxx"


def test_get_auth_token_from_gh_cli_returns_none_when_gh_missing():
    """If `gh` isn't on PATH, return None (don't raise)."""
    import subprocess
    with patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
        assert github_api.get_auth_token_from_gh_cli() is None


def test_get_auth_token_from_gh_cli_returns_none_when_gh_fails():
    """If `gh auth token` exits non-zero, return None."""
    import subprocess
    fake_proc = MagicMock()
    fake_proc.stdout = b""
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
        assert github_api.get_auth_token_from_gh_cli() is None


def test_wait_for_git_daemon_returns_true_on_first_200():
    """If the REST endpoint returns 200 immediately, return True
    after a single attempt (no sleeping)."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, FAKE_REPO)
        result = github_api.wait_for_git_daemon("SaCScaffolder", "thing", token="***")
    assert result is True
    assert mock_urlopen.call_count == 1


def test_wait_for_git_daemon_retries_through_404():
    """If the first N attempts return 404, the helper keeps polling
    and returns True when a 200 finally arrives."""
    import urllib.error
    from io import BytesIO
    with patch("urllib.request.urlopen") as mock_urlopen, \
         patch("time.sleep") as mock_sleep:
        # First two calls 404, third call 200.
        not_found = urllib.error.HTTPError(
            "https://api.github.com/repos/SaCScaffolder/thing",
            404, "Not Found", {"Content-Type": "application/json"},
            fp=BytesIO(b'{"message":"Not Found"}'),
        )
        mock_urlopen.side_effect = [not_found, not_found, _mock_response(200, FAKE_REPO)]
        result = github_api.wait_for_git_daemon("SaCScaffolder", "thing", token="***")
    assert result is True
    assert mock_urlopen.call_count == 3
    assert mock_sleep.call_count == 2  # slept between the 3 attempts


def test_wait_for_git_daemon_returns_false_on_timeout():
    """If every attempt returns 404, the helper returns False after
    the configured number of attempts."""
    import urllib.error
    from io import BytesIO
    not_found = urllib.error.HTTPError(
        "https://api.github.com/repos/SaCScaffolder/thing",
        404, "Not Found", {"Content-Type": "application/json"},
        fp=BytesIO(b'{"message":"Not Found"}'),
    )
    with patch("urllib.request.urlopen", side_effect=not_found) as mock_urlopen, \
         patch("time.sleep") as mock_sleep:
        result = github_api.wait_for_git_daemon(
            "SaCScaffolder", "thing", token="***", max_attempts=3, sleep_seconds=0.1,
        )
    assert result is False
    assert mock_urlopen.call_count == 3


def test_wait_for_git_daemon_treats_network_errors_as_retry():
    """A URLError (network blip) is treated as 'not yet' rather than
    failing the helper. This is important because a transient DNS
    hiccup shouldn't cause the integration test to fail when the
    repo is actually there."""
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns")), \
         patch("time.sleep"), \
         patch.object(github_api, "DEFAULT_BASE_URL", "https://api.github.com"):
        # 3 attempts, all fail with URLError, no 200 ever arrives.
        result = github_api.wait_for_git_daemon(
            "SaCScaffolder", "thing", token="***", max_attempts=3, sleep_seconds=0.1,
        )
    assert result is False
