"""Live markdown repo sync (ING-6, SEC-3).

Clones the bot's repo, diffs against the last synced commit, registers changed
text files as new doc versions, archives deleted ones. Credentials come from a
secret scope; the repo host must be on the workspace network allowlist.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .config import BotConfig, PlatformSettings
from .documents import Documents
from .ingestion import BotPaths

ACTOR = "system:git_sync"


def _git(*cmd: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *cmd], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def sync_repo(cfg: BotConfig, settings: PlatformSettings, sql, docs: Documents,
              token: str = "") -> tuple[str, list[str]]:
    """Returns (head_sha, changed doc_ids). Empty list when nothing changed."""
    p = BotPaths(settings, cfg.bot_id)
    text_ext = set(settings.get("ingestion.text_extensions"))
    url = cfg.source_uri
    if token and url.startswith("https://"):
        url = url.replace("https://", f"https://x-access-token:{token}@", 1)
    last = sql.query(f"SELECT max_by(source_ref, uploaded_at) AS sha FROM {p.t('manifest')} "
                     "WHERE source = 'git'")[0]["sha"]
    changed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        _git("clone", "--quiet", "--branch", cfg.git_branch or "main", url, tmp)
        head = _git("rev-parse", "HEAD", cwd=tmp)
        if head == last:
            return head, []
        if last:
            lines = _git("diff", "--name-status", last, head, cwd=tmp).splitlines()
            changes = [(ln.split("\t")[0][0], ln.split("\t")[-1]) for ln in lines if ln]
        else:
            changes = [("A", str(f.relative_to(tmp))) for f in Path(tmp).rglob("*")
                       if f.is_file() and ".git" not in f.parts]
        for status, rel in changes:
            if rel.rsplit(".", 1)[-1].lower() not in text_ext:
                continue
            name = rel.replace("/", "__")  # keep folder context in the doc name
            if status == "D":
                prior = docs.find_by_name(cfg.bot_id, name)
                if prior:
                    docs.archive(cfg.bot_id, prior["doc_id"], ACTOR)
                continue
            res = docs.register(cfg.bot_id, name, (Path(tmp) / rel).read_bytes(), ACTOR,
                                source="git", source_ref=head)
            if res.ok:
                changed.append(docs.find_by_name(cfg.bot_id, name)["doc_id"])
    return head, changed
