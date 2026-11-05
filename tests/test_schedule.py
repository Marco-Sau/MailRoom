"""Run tab → Automatic runs: validation, the LaunchAgent plist, next runs, the background run, the cross-process lock."""
import json
import plistlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mailroom import run_lock, schedule
from mailroom.models import RunResult


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(schedule, "plist_path", lambda: tmp_path / "LaunchAgents" / f"{schedule.LABEL}.plist")
    calls = []

    class R:
        def __init__(self, rc=0):
            self.returncode, self.stdout, self.stderr = rc, "", ""

    monkeypatch.setattr(schedule, "_launchctl", lambda *a: calls.append(a) or R())
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    from mailroom import db
    db.init_db()
    monkeypatch.setattr(schedule, "_notify", lambda text: calls.append(("notify", text)))
    yield calls
    run_lock.release()


JOB = "email-assistant-claude"


def test_validate_rules(data):
    ok = schedule.validate({"enabled": True, "job_id": JOB, "days": [5, 1, 1], "times": ["18:00", "08:00"]})
    assert ok["days"] == [1, 5] and ok["times"] == ["08:00", "18:00"]
    for bad, msg in [({"enabled": True, "job_id": "nope"}, "engine"),
                     ({"enabled": True, "job_id": JOB, "days": []}, "day"),
                     ({"enabled": True, "job_id": JOB, "times": []}, "time"),
                     ({"enabled": True, "job_id": JOB, "times": ["8:00"]}, "08:30"),
                     ({"enabled": True, "job_id": JOB, "lookbackHours": 500}, "168"),
                     ({"enabled": True, "job_id": JOB, "times": ["06:00"],
                       "window": {"enabled": True, "start": "07:00", "end": "20:00"}}, "outside"),
                     ({"enabled": True, "job_id": JOB, "window": {"enabled": True, "start": "20:00", "end": "07:00"}}, "before")]:
        with pytest.raises(schedule.ScheduleError, match=msg):
            schedule.validate(bad)


def test_save_installs_and_removes_launch_agent(data):
    st = schedule.save({"enabled": True, "job_id": JOB, "days": [1, 7], "times": ["08:00", "13:30"]})
    p = schedule.plist_path()
    pl = plistlib.loads(p.read_bytes())
    assert pl["Label"] == "com.mailroom.autorun"
    assert pl["ProgramArguments"][-3:] == ["-m", "mailroom.cli", "autorun"]
    assert {"Weekday": 0, "Hour": 13, "Minute": 30} in pl["StartCalendarInterval"]      # Sunday = 0
    assert len(pl["StartCalendarInterval"]) == 4
    assert any(c[0] == "bootstrap" for c in data)
    assert st["config"]["enabled"] and st["installed"]["file"] and st["installed"]["current"]
    st = schedule.save({"enabled": False})
    assert not p.exists() and not st["config"]["enabled"]
    assert data[-1][0] == "bootout"


def test_install_refused_is_reported_and_config_not_saved(data, monkeypatch):
    class Bad:
        returncode, stdout, stderr = 5, "", "Bootstrap failed: 5: Input/output error"
    monkeypatch.setattr(schedule, "_launchctl", lambda *a: Bad())
    with pytest.raises(schedule.ScheduleError, match="did not accept"):
        schedule.save({"enabled": True, "job_id": JOB})
    assert not schedule.load()["enabled"]


def test_next_runs():
    cfg = {"enabled": True, "days": [1, 3], "times": ["08:00", "18:00"]}
    wed_noon = datetime(2026, 10, 28, 12, 0)            # a Wednesday
    assert schedule.next_runs(cfg, wed_noon) == ["2026-10-28T18:00", "2026-11-02T08:00", "2026-11-02T18:00"]
    assert schedule.next_runs({**cfg, "enabled": False}, wed_noon) == []


class FakeRunner:
    seen = []

    def run(self, spec):
        FakeRunner.seen.append((spec.job_id, spec.hours, spec.name))
        return RunResult(run_id=7, job_id=spec.job_id, status="succeeded", started_at=datetime.now(),
                         stats={"reply_drafts": 3})


def _on(**kw):
    schedule.config_path().write_text(json.dumps({"enabled": True, "job_id": JOB, "days": [1, 2, 3, 4, 5, 6, 7],
                                                  "times": ["08:00"], **kw}))


def test_autorun_runs_job_with_lookback(data, monkeypatch):
    monkeypatch.setattr("mailroom.runners.get_runner", lambda r: FakeRunner())
    FakeRunner.seen.clear()
    _on(lookbackHours=24)
    assert schedule.autorun(datetime(2026, 10, 28, 8, 0)) == 0
    assert FakeRunner.seen == [(JOB, 24, "Claude (automatic)")]
    last = schedule.last_run()
    assert last["status"] == "succeeded" and last["summary"] == "3 reply drafts"
    assert any(c[0] == "notify" and "nothing sent" in c[1] for c in data)
    assert run_lock.held_elsewhere() is None and run_lock._fh is None       # released


def test_autorun_skips_outside_window_off_and_when_busy(data, monkeypatch):
    monkeypatch.setattr("mailroom.runners.get_runner", lambda r: FakeRunner())
    FakeRunner.seen.clear()
    schedule.autorun(datetime(2026, 10, 28, 8, 0))                         # never configured: off
    _on(window={"enabled": True, "start": "07:00", "end": "20:00"})
    schedule.autorun(datetime(2026, 10, 28, 3, 15))                        # catch-up after waking at night
    assert schedule.last_run()["status"] == "skipped" and "allowed hours" in schedule.last_run()["reason"]
    assert run_lock.try_acquire("app")                                     # a manual run holds the lock
    schedule.autorun(datetime(2026, 10, 28, 9, 0))
    assert FakeRunner.seen == []


def test_lock_is_seen_from_another_open(data):
    import fcntl
    p = run_lock.path()
    fh = open(p, "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fh.write(json.dumps({"owner": "automatic", "pid": 1}))
    fh.flush()
    try:
        assert run_lock.held_elsewhere()["owner"] == "automatic"
        assert not run_lock.try_acquire("app")
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    assert run_lock.held_elsewhere() is None


def test_api_and_stop_for_external_run(data, monkeypatch):
    from mailroom.api import routes_jobs
    from mailroom.main import app
    from mailroom.runners import control
    c = TestClient(app)
    r = c.put("/api/schedule", json={"enabled": True, "job_id": JOB, "days": [1], "times": ["06:45"]})
    assert r.status_code == 200 and r.json()["config"]["times"] == ["06:45"]
    assert c.put("/api/schedule", json={"enabled": True, "job_id": JOB, "times": ["7"]}).status_code == 400
    monkeypatch.setattr(routes_jobs.run_lock, "held_elsewhere", lambda: {"owner": "automatic", "pid": 9})
    s = c.get("/api/jobs/status").json()
    assert s["busy"] and s["automatic"]
    assert c.post(f"/api/jobs/{JOB}/run").status_code == 409
    assert c.post("/api/jobs/stop").status_code == 200
    assert control._stop_file().exists()
    control.end()
    assert not control._stop_file().exists()


def test_history_and_unseen(data, monkeypatch):
    monkeypatch.setattr("mailroom.runners.get_runner", lambda r: FakeRunner())
    _on(window={"enabled": True, "start": "07:00", "end": "20:00"})
    schedule.autorun(datetime(2099, 10, 28, 8, 0))
    schedule.autorun(datetime(2099, 10, 28, 23, 0))                         # skipped
    h = schedule.history()
    assert [x["status"] for x in h] == ["skipped", "succeeded"]
    assert schedule.unseen()["count"] == 2
    assert schedule.mark_seen()["count"] == 0
    schedule.autorun(datetime(2099, 10, 29, 9, 0))
    u = schedule.unseen()
    assert u["count"] == 1 and u["items"][0]["summary"] == "3 reply drafts"
    from mailroom.main import app
    c = TestClient(app)
    assert c.get("/api/schedule/news").json()["count"] == 1
    assert c.post("/api/schedule/seen").json()["count"] == 0


def test_history_falls_back_to_last_run(data):
    schedule.last_path().write_text(json.dumps({"status": "succeeded", "at": "2026-10-28T12:10:02", "job": JOB}))
    assert schedule.history()[0]["at"] == "2026-10-28T12:10:02" and schedule.unseen()["count"] == 1


def test_stale_running_rows_become_interrupted(data):
    from mailroom import db, run_state
    from mailroom.runners import control
    rid = db.start_run(JOB, "Claude (automatic)", "assistant", "claude_cli", "m")
    live = db.start_run("email-assistant-chatgpt", "ChatGPT", "assistant", "codex_cli", "m")
    assert run_lock.try_acquire("app")
    assert run_state.reconcile() == []                    # this process holds the lock, no run id yet
    control.begin(live, "assistant")
    try:
        assert [r["id"] for r in run_state.reconcile()] == [rid]   # the live run is kept
    finally:
        control.end()
        run_lock.release()
    assert db.get_run(rid)["status"] == "interrupted" and db.get_run(live)["status"] == "running"
    h = schedule.history()[0]
    assert h["status"] == "interrupted" and h["run_id"] == rid
    assert schedule.unseen()["count"] == 1


def test_launch_starts_a_detached_worker(data, monkeypatch):
    seen = {}
    monkeypatch.setattr(schedule.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, **kw))
    assert schedule.launch() == 0
    assert seen["argv"][-2:] == ["autorun", "--worker"] and seen["start_new_session"] is True
    assert schedule.build_plist(schedule.load())["AbandonProcessGroup"] is True
