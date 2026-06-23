"""Optional GitHub publishing + durable state backup.

Two jobs, both via the GitHub API against a SEPARATE branch (PUBLISH_BRANCH,
default "bot-live") so they never trigger a Pages rebuild:

  1. publish the dashboard snapshot (docs/stocks/data/bot.json) so a website
     can show live status, and
  2. back up the bot's state.json and restore it on boot — this lets the bot run
     on a stateless cloud host (e.g. Railway) WITHOUT a disk and still remember
     open positions across redeploys/restarts.

Setup (env vars / bot/.env):
  PUBLISH_DASHBOARD=true
  GITHUB_TOKEN=github_pat_...      # fine-grained PAT: Contents = Read & Write
  GH_REPO=salem0557/binance-stock-bot
  PUBLISH_BRANCH=bot-live

Everything is opt-in: with no token the bot just runs locally as before.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL = HERE.parent / "docs" / "stocks" / "data" / "bot.json"
DASHBOARD_PATH = "docs/stocks/data/bot.json"
STATE_PATH = "state-backup/state.json"
API = "https://api.github.com"


def _api(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stockbot-publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _get_sha(repo, branch, token, path):
    url = f"{API}/repos/{repo}/contents/{path}?ref={branch}"
    try:
        return _api("GET", url, token).get("sha")
    except Exception:
        return None  # file (or branch) doesn't exist yet


def put_file(repo, branch, token, path, content_bytes, message):
    """Create/update a file on ``branch``. Returns True on success."""
    if not (repo and branch and token):
        return False
    body = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": branch,
    }
    sha = _get_sha(repo, branch, token, path)
    if sha:
        body["sha"] = sha
    try:
        _api("PUT", f"{API}/repos/{repo}/contents/{path}", token, body)
        return True
    except Exception:
        return False


def get_file(repo, branch, token, path):
    """Fetch a file's bytes from ``branch`` (or None if missing)."""
    if not (repo and branch and token):
        return None
    url = f"{API}/repos/{repo}/contents/{path}?ref={branch}"
    try:
        meta = _api("GET", url, token)
        return base64.b64decode(meta["content"])
    except Exception:
        return None


# --- convenience wrappers used by the bot ---
def publish(repo, branch, token):
    """Push the current dashboard snapshot."""
    try:
        content = LOCAL.read_bytes()
    except Exception:
        return False
    return put_file(repo, branch, token, DASHBOARD_PATH, content,
                    "Update bot dashboard snapshot")


def backup_state(repo, branch, token, state_file):
    """Push the bot's state.json so it survives a stateless redeploy."""
    try:
        content = Path(state_file).read_bytes()
    except Exception:
        return False
    return put_file(repo, branch, token, STATE_PATH, content,
                    "Backup bot state")


def restore_state(repo, branch, token, state_file):
    """Restore state.json from GitHub on boot if it isn't present locally."""
    data = get_file(repo, branch, token, STATE_PATH)
    if not data:
        return False
    try:
        Path(state_file).write_bytes(data)
        return True
    except Exception:
        return False
