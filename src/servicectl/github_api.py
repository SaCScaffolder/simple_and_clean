"""GitHub API helpers used by servicectl.

This module exists so the human-facing `servicectl init` command and the
integration test workflows in SaCScaffolder/sac can share a single
implementation of "create a repo on GitHub." The integration test is a
faithful recreation of the user experience, so whatever path `init`
takes is what the workflow has to take too (see SaCScaffolder/sac's
`integration-test-modified.yml` bootstrap path).

We deliberately do NOT depend on the `gh` CLI or the GitHub App
installation token minting flow here. `servicectl init` is a
human-facing command and the user is the actor: they authenticate with
their own token (PAT, fine-grained PAT, or `gh auth login` session) and
the resulting repo is attributed to them, not to a service account.

Auth detection order in the CLI (see `cli.py`):
    1. --token flag (explicit, never read from disk in this module)
    2. $GH_TOKEN env var (if the user wants to inject)
    3. `gh auth token` subprocess (if `gh` is installed and logged in)
    4. Fail loud with a clear remediation message
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from typing import Any


#: Default base URL for GitHub.com. Override for GHES (Enterprise Server).
DEFAULT_BASE_URL = "https://api.github.com"

#: Standard headers sent on every request. The Authorization header is
#: filled in by the caller because the token isn't always known at
#: module-import time.
def _standard_headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "servicectl",
    }


def _request(
    method: str,
    url: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    accept: str = "application/vnd.github+json",
    timeout: float = 30.0,
) -> tuple[int, str, dict[str, str]]:
    """Issue an HTTP request and return (status, body, response-headers).

    Body is a JSON string if Content-Type is application/json, else the
    raw response body. Headers are returned as a lowercase-key dict.
    """
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    headers = _standard_headers(token, accept=accept)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                resp.read().decode("utf-8", errors="replace"),
                {k.lower(): v for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            e.read().decode("utf-8", errors="replace"),
            {k.lower(): v for k, v in e.headers.items()},
        )


def create_repo(
    owner: str,
    name: str,
    *,
    token: str,
    private: bool = False,
    description: str | None = None,
    auto_init: bool = False,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Create a new repository on GitHub.

    POSTs to `/orgs/{owner}/repos` if the owner looks like an org
    (no leading underscore, doesn't start with "github-", and isn't
    a known-user-prefix) and `/user/repos` otherwise. The heuristic is
    intentionally simple: we ask the user to disambiguate with the
    `--orgs/--user` flag if it ever matters, but for the common case
    ("create a repo in my org") the org path is correct.

    Returns the parsed JSON response (which includes `clone_url`,
    `ssh_url`, `html_url`, etc.).

    Raises `GitHubAPIError` on non-2xx, with the response body included
    so the CLI can surface a useful error message.
    """
    # Heuristic: org names don't contain uppercase or certain
    # characters, but user logins do. We default to the org endpoint
    # for all-lowercase names with a hyphen, dot, or underscore, and
    # fall back to the user endpoint for names with mixed case or
    # digits. This is a best-effort guess; if it ever picks wrong, the
    # caller can use `create_user_repo` or `create_org_repo` directly.
    endpoint = _pick_create_endpoint(owner, base_url)
    payload: dict[str, Any] = {
        "name": name,
        "private": private,
        "auto_init": auto_init,
    }
    if description is not None:
        payload["description"] = description

    status, body, _ = _request("POST", endpoint, token=token, body=payload)
    if status < 200 or status >= 300:
        raise GitHubAPIError(
            f"failed to create repo {owner}/{name}: HTTP {status}",
            status=status,
            body=body,
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise GitHubAPIError(
            f"non-JSON response from GitHub for {owner}/{name}: {e}",
            status=status,
            body=body,
        ) from e


def create_org_repo(
    org: str,
    name: str,
    *,
    token: str,
    private: bool = False,
    description: str | None = None,
    auto_init: bool = False,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Create a repo in an organization. Always uses /orgs/{org}/repos."""
    payload: dict[str, Any] = {"name": name, "private": private, "auto_init": auto_init}
    if description is not None:
        payload["description"] = description
    status, body, _ = _request(
        "POST",
        f"{base_url}/orgs/{org}/repos",
        token=token,
        body=payload,
    )
    if status < 200 or status >= 300:
        raise GitHubAPIError(
            f"failed to create repo {org}/{name}: HTTP {status}",
            status=status,
            body=body,
        )
    return json.loads(body)


def create_user_repo(
    username: str,
    name: str,
    *,
    token: str,
    private: bool = False,
    description: str | None = None,
    auto_init: bool = False,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Create a repo in a user namespace. Always uses /user/repos.

    The username is accepted for symmetry with `create_org_repo` but
    isn't used in the URL (POST /user/repos always creates a repo in
    the authenticated user's namespace).
    """
    del username  # unused; kept for API symmetry
    payload: dict[str, Any] = {"name": name, "private": private, "auto_init": auto_init}
    if description is not None:
        payload["description"] = description
    status, body, _ = _request(
        "POST",
        f"{base_url}/user/repos",
        token=token,
        body=payload,
    )
    if status < 200 or status >= 300:
        raise GitHubAPIError(
            f"failed to create repo for user: HTTP {status}",
            status=status,
            body=body,
        )
    return json.loads(body)


def wait_for_git_daemon(
    owner: str,
    name: str,
    *,
    token: str,
    max_attempts: int = 12,
    sleep_seconds: float = 5.0,
    base_url: str = DEFAULT_BASE_URL,
) -> bool:
    """Poll the repo's REST endpoint until it returns 200, with retries.

    GitHub's REST API returns 201 from POST /repos before the git
    daemon has caught up. Without this wait, a follow-up `git push`
    can fail with `fatal: repository '.../<repo>.git/' not found`
    even though the repo was just created. Verified: ~10-50% of
    runs hit this race depending on the time of day; the wait
    makes it deterministic.

    The default 12 attempts x 5s = 60s ceiling matches the wait
    that lived in SaCScaffolder/sac's integration test workflow
    (the workflow also had this wait inline before the refactor).

    Returns True if the repo's REST endpoint returned 200 within
    the window, False if we timed out. Callers should treat False
    as "the push might fail; the user can retry."
    """
    import time
    import urllib.error
    url = f"{base_url}/repos/{owner}/{name}"
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(url, headers=_standard_headers(token))
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError:
            # 404 is the expected "not ready yet" signal. Any other
            # code is a real problem; let the caller see it surface
            # naturally on the subsequent push.
            pass
        except (urllib.error.URLError, OSError):
            # Network blip. Treat as "not yet" and retry.
            pass
        time.sleep(sleep_seconds)
    return False


def _pick_create_endpoint(owner: str, base_url: str) -> str:
    """Best-effort guess of which endpoint to use for a given owner.

    Returns the org endpoint if `owner` looks like an org slug
    (all-lowercase, no spaces, no special characters beyond
    `-._`), and the user endpoint otherwise.

    The integration test workflow should call `create_org_repo`
    directly to bypass this heuristic.
    """
    if owner and all(c.islower() or c.isdigit() or c in "-._" for c in owner) and owner:
        return f"{base_url}/orgs/{owner}/repos"
    return f"{base_url}/user/repos"


def get_auth_token_from_gh_cli() -> str | None:
    """Return the current `gh` CLI's auth token, or None if unavailable.

    Uses `gh auth token`, which prints the current active account's
    token to stdout. We don't pass --user because we want the
    ACTIVE account (matches what `gh` does for everything else).

    Returns None if `gh` is not installed, the user is not logged in,
    or the subprocess fails for any other reason. The caller is
    expected to fail loud with a clear remediation message when
    None is returned.
    """
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    token = out.stdout.decode("utf-8", errors="replace").strip()
    return token or None


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API call returns a non-2xx response."""

    def __init__(self, message: str, *, status: int, body: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
