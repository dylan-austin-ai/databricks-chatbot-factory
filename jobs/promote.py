"""Promote a bot's approved candidate release to live, then smoke-test it (REL-2, REL-5, REL-11).

Triggered by the app when the Reviewer approves. Steps:
  1. Check the candidate passed the combined gate (EVG-1) and was approved.
  2. Promote: candidate becomes live without re-parsing or re-embedding.
  3. Sync the index, then run a smoke test on the live channel (subset of golden
     questions + health check). If it fails, roll back automatically and alert.

--action rollback   roll the live release back to the previous one instead.
"""
import time

from _bootstrap import ROOT, args, context, vector_client

from factory.lifecycle import State
from factory.provisioning import sync_index
from factory.releases import Releases

a = args("catalog", "bot_id", "actor", "action", "reason", "environment")
spark, sql, settings, cp, w = context(a.catalog)
cfg = cp.get_config(a.bot_id)
bot = cp.get_bot(a.bot_id)
rel = Releases(sql, settings, cp)
vsc = vector_client()
actor = a.actor or "system:promote"


def wait_for_sync(timeout_s: int = 900) -> None:
    """Trigger a sync and wait until the index reports it's online again."""
    from factory.provisioning import index_name_for
    sync_index(vsc, settings, cfg)
    idx = vsc.get_index(settings.get("ai_search.endpoint"), index_name_for(settings, cfg))
    time.sleep(10)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = str(idx.describe().get("status", {}).get("detailed_state", ""))
        if state.startswith("ONLINE") and "UPDATE" not in state and "PROVISION" not in state:
            return
        time.sleep(15)
    raise TimeoutError("Index sync did not finish in time")


if a.action == "rollback":
    target = rel.rollback(cfg, actor, reason=a.reason or "manual rollback")
    wait_for_sync()
    cp.publish(cfg.bot_id)
    print(f"Rolled back {cfg.bot_id} to {target}")
    raise SystemExit(0)

cand = sql.query(f"SELECT * FROM {settings.fq('releases')} WHERE release_id = :r",
                 {"r": bot.get("candidate_release_id")})
if not cand:
    raise SystemExit("No candidate release to promote.")
c = cand[0]
if str(c["eval_passed"]).lower() != "true" or c.get("review_requested_at") is None:
    raise SystemExit("The candidate must pass the go-live checks (EVG-1) and be sent for approval.")

previous_live = bot.get("live_release_id")
rid = rel.promote(cfg, actor)
if bot["state"] == State.PENDING_APPROVAL.value:
    cp.set_state(cfg.bot_id, State.LIVE.value, actor, {"release": rid})
wait_for_sync()
cp.publish(cfg.bot_id)

# Smoke test (REL-11): evaluate the live channel on a subset of questions.
import subprocess  # noqa: E402
import sys  # noqa: E402

smoke = subprocess.run(
    [sys.executable, str(ROOT / "jobs" / "run_evals.py"), f"--catalog={settings.catalog}",
     f"--bot_id={cfg.bot_id}", "--channel=live", "--trigger=smoke", f"--environment={a.environment or 'qa'}",
     f"--limit={settings.get('quality.smoke_questions', 5)}"], capture_output=True, text=True)
passed = smoke.returncode == 0
sql.execute(f"UPDATE {settings.fq('releases')} SET smoke_passed = CAST(:p AS BOOLEAN) WHERE release_id = :r",
            {"p": passed, "r": rid})
if not passed:
    print(smoke.stdout[-4000:], smoke.stderr[-4000:])
    if previous_live:
        rel.rollback(cfg, "system:smoke_test", to_release=previous_live, reason="smoke test failed")
        wait_for_sync()
    else:
        cp.set_state(cfg.bot_id, State.TESTING.value, "system:smoke_test", {"reason": "smoke test failed"})
    cp.publish(cfg.bot_id)
    cp.alert(cfg.bot_id, "smoke_failed", "high",
             f"Release {rid} failed its post-promotion smoke test and was rolled back.",
             dedupe_key=f"smoke:{rid}")
    raise SystemExit("Smoke test failed; rolled back.")
print(f"Promoted {rid}; smoke test passed")
