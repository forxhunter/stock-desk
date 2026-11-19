import json
import time
from types import SimpleNamespace

from stockdesk import credentials, flow, headless, notify, remote

from test_engine_misc import env  # noqa: F401  (fixture)

MAIL = {"enabled": True, "to": "me@example.com", "smtp_host": "smtp.example.com", "smtp_port": 587,
        "username": "bot@example.com"}


def at(hh, mm, weekday=3):
    """A timestamp on a Thursday (weekday=3) or the given weekday, local time."""
    t = time.struct_time((2026, 11, 16 + weekday, hh, mm, 0, 0, 0, -1))
    return time.mktime(t)


def test_only_the_chosen_machine_sends_mail(env, monkeypatch):
    eng, _, store, _ = env
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, subject, body, **k: sent.append(subject))
    eng.cfg.set("notify.email", {**MAIL, "send_from": "remote"})
    eng.cfg.set("remote.host", "me@box")
    eng._flow = flow.FlowReport(ts=time.time(), items=[flow.FlowItem("polymarket", "k1", "Fed cuts?", severity="high",
                                                                     tier="macro", score=5.0, headline="h")])
    monkeypatch.delenv("STOCKDESK_ROLE", raising=False)
    snap = eng.refresh(force=True)
    assert sent == [] and "me@box" in snap.sources["email"][1]          # the desktop stays quiet
    monkeypatch.setenv("STOCKDESK_ROLE", "remote")
    eng.build_snapshot()
    eng.build_snapshot()
    assert len(sent) == 1 and "Fed cuts?" in sent[0]                    # the runner sends it, once


def test_tick_sends_one_digest_a_day_after_the_set_time(env, monkeypatch):
    eng, _, store, _ = env
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, subject, body, **k: sent.append((subject, body)))
    monkeypatch.setenv("STOCKDESK_ROLE", "remote")
    eng.cfg.set("flow.enabled", False)
    eng.cfg.set("notify.email", {**MAIL, "send_from": "remote", "daily_at": "15:30"})
    headless.tick(eng, at(15, 0))
    assert sent == []
    headless.tick(eng, at(15, 40))
    headless.tick(eng, at(16, 40))
    assert len(sent) == 1
    subject, body = sent[0]
    assert "每日信号" in subject and "【量化信号】" in body and "AAA" in body and "综合信号" in body
    assert "市值" not in body and "盈亏" not in body and "股 ·" not in body      # signals only, no holdings
    headless.tick(eng, at(16, 0, weekday=5))                                    # Saturday
    assert len(sent) == 1
    assert store.get_meta("headless:tick")["at"] == at(16, 0, weekday=5)
    eng.cfg.set("notify.email.send_from", "local")
    assert headless.digest_due(eng.cfg, store, at(16, 0, weekday=4)) is True    # due, but tick checks the role first
    headless.tick(eng, at(16, 0, weekday=4))
    assert len(sent) == 1


def test_secrets_file_is_used_only_when_named(tmp_path, monkeypatch):
    f = tmp_path / "secrets.json"
    f.write_text(json.dumps({"smtp_secret": "abc"}), encoding="utf-8")
    monkeypatch.setenv("STOCKDESK_SECRETS_FILE", str(f))
    assert credentials.get_secret("smtp_secret") == "abc" and credentials.get_secret("other") is None
    assert credentials.set_secret("x", "y") is False and "x" not in f.read_text(encoding="utf-8")
    monkeypatch.setenv("STOCKDESK_SECRETS_FILE", str(tmp_path / "missing.json"))
    assert credentials.get_secret("smtp_secret") is None


def test_remote_sync_pushes_user_files_only_when_changed(env, monkeypatch, tmp_path):
    import stockdesk.paths as paths
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "WATCHLIST", tmp_path / "watchlist.yaml")
    monkeypatch.setattr(paths, "USER_CONFIG", tmp_path / "data" / "config.yaml")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    calls = []

    def runner(cmd, input=None, **kw):
        calls.append((cmd, input))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    assert remote.sync_if_changed({}, None, runner) is None and calls == []     # no host configured
    cfg = {"host": "me@box", "dir": "sd"}
    s1 = remote.sync_if_changed(cfg, None, runner)
    assert len(calls) == 1 and calls[0][0][-2] == "me@box" and "BatchMode=yes" in calls[0][0]
    assert "tar xzf - -C sd" in calls[0][0][-1] and calls[0][1][:2] == b"\x1f\x8b"
    assert remote.sync_if_changed(cfg, s1, runner) == s1 and len(calls) == 1    # unchanged: nothing sent
    (tmp_path / "data" / "plans.yaml").write_text("plans: {}\n", encoding="utf-8")
    assert remote.sync_if_changed(cfg, s1, runner) != s1 and len(calls) == 2
    bad = lambda cmd, **kw: SimpleNamespace(returncode=255, stdout=b"", stderr=b"offline")
    assert remote.sync_if_changed(cfg, "old", bad) == "old"                     # failure: retried next cycle
    line = remote.cron_line(remote.settings(cfg))
    assert line.startswith("*/10 ") and "STOCKDESK_ROLE=remote" in line and line.endswith(remote.MARK)
    assert "secrets" not in " ".join(f.name for f in remote.user_files())


def test_keep_in_sync_compares_contents_and_repairs(env, monkeypatch, tmp_path):
    import hashlib
    import stockdesk.paths as paths
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "WATCHLIST", tmp_path / "watchlist.yaml")
    monkeypatch.setattr(paths, "USER_CONFIG", tmp_path / "data" / "config.yaml")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "watchlist.yaml").write_text("tickers: []\n", encoding="utf-8")
    sha = lambda b: hashlib.sha256(b).hexdigest()
    there = {"src/pkg/a.py": sha(b"x = 0\n"), "src/pkg/old.py": sha(b"")}      # stale copy + a leftover file
    age = [120]
    calls = []

    def runner(cmd, input=None, **kw):
        c = cmd[-1]
        calls.append(c)
        if "sha256sum" in c:
            out = "".join(f"{h}  {p}\n" for p, h in sorted(there.items())) + f"---\n{1000 - age[0]}\n1000\nlast line\n"
            return SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b"")
        if "tar xzf" in c:                                                     # a push makes the remote match
            if "rm -rf" in c:
                there.pop("src/pkg/old.py", None)
            there.update(remote.local_manifest())
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    cfg = {"host": "me@box", "dir": "sd"}
    st = remote.keep_in_sync(cfg, None, runner, now=5000.0)
    assert st["status"][0] == "ok" and "完全一致" in st["status"][1] and "刚更新了 2 个" in st["status"][1]
    assert there == remote.local_manifest()
    n = len(calls)
    assert remote.keep_in_sync(cfg, st, runner, now=5100.0) is st and len(calls) == n      # nothing changed: no ssh
    assert remote.keep_in_sync(cfg, st, runner, now=5000.0 + remote.CHECK_EVERY + 1)["checked_at"] > 5000   # periodic re-check
    (tmp_path / "watchlist.yaml").write_bytes(b"tickers: [{symbol: TSM}]\n")
    st2 = remote.keep_in_sync(cfg, st, runner, now=5200.0)
    assert st2["status"][0] == "ok" and there["watchlist.yaml"] == sha(b"tickers: [{symbol: TSM}]\n")
    age[0] = 4000                                                              # the remote stopped running
    stale = remote.keep_in_sync(cfg, None, runner, now=5300.0)
    assert stale["status"][0] == "error" and "没有运行" in stale["status"][1]
    down = remote.keep_in_sync(cfg, None, lambda cmd, **kw: SimpleNamespace(returncode=255, stdout=b"", stderr=b"x"), now=1.0)
    assert down["status"][0] == "error" and "连不上" in down["status"][1] and down["stamp"] is None
    assert remote.keep_in_sync({}, None, runner) is None
