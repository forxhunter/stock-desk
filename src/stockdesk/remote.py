"""Keep an always-on machine in step with this one, over ssh (key login only, never a password).

What goes across: the program itself, the watchlist, the non-secret settings, the plans and the
imported broker reports. Secrets are not part of the sync; ``scripts/push_secrets.py`` moves those
when the user runs it. Everything is driven by ``remote.host`` / ``remote.dir`` in the config.
"""
from __future__ import annotations

import io
import logging
import subprocess
import tarfile
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
CODE = ["src", "config.default.yaml", "watchlist.sample.yaml", "requirements-server.txt"]
MARK = "# stock-desk"


def settings(cfg: dict | None) -> dict:
    c = dict(cfg or {})
    return {"host": str(c.get("host") or "").strip(), "dir": str(c.get("dir") or "stock-desk").strip().strip("/"),
            "auto_sync": bool(c.get("auto_sync", True))}


def user_files() -> list[Path]:
    files = [paths.WATCHLIST, paths.USER_CONFIG, paths.DATA_DIR / "plans.yaml"]
    files += sorted(p for p in paths.RH_IMPORT_DIR.glob("*") if p.suffix.lower() in (".csv", ".xlsx"))
    return [f for f in files if f.is_file()]


def stamp(files: list[Path] | None = None) -> str:
    """Changes whenever one of the synced user files does."""
    return "|".join(f"{f.name}:{f.stat().st_mtime_ns}:{f.stat().st_size}" for f in (files or user_files()))


def _tar(files: list[Path], root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            tar.add(f, arcname=f.relative_to(root).as_posix(),
                    filter=lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti)
    return buf.getvalue()


def run(host: str, command: str, data: bytes | None = None, timeout: float = 120, runner=subprocess.run) -> str:
    r = runner(SSH + [host, command], input=data, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[-300:] or f"ssh exit {r.returncode}")
    return (r.stdout or b"").decode("utf-8", "replace")


def push_user_files(s: dict, runner=subprocess.run) -> int:
    files = user_files()
    run(s["host"], f"mkdir -p {s['dir']}/data && tar xzf - -C {s['dir']}", _tar(files, paths.ROOT), runner=runner)
    return len(files)


def push_code(s: dict, runner=subprocess.run) -> None:
    files = [paths.ROOT / c for c in CODE if (paths.ROOT / c).exists()]
    run(s["host"], f"mkdir -p {s['dir']} && rm -rf {s['dir']}/src && tar xzf - -C {s['dir']}", _tar(files, paths.ROOT),
        runner=runner)


def cron_line(s: dict, minutes: int = 10) -> str:
    d = f"$HOME/{s['dir']}"
    env = f"STOCKDESK_HOME={d} STOCKDESK_ROLE=remote STOCKDESK_SECRETS_FILE=$HOME/.config/stockdesk/secrets.json PYTHONPATH={d}/src"
    return (f"*/{minutes} * * * * cd {d} && flock -n data/tick.lock env {env} .venv/bin/python -m stockdesk.headless tick "
            f">> data/cron.log 2>&1 {MARK}")


def sync_if_changed(cfg: dict | None, last: str | None, runner=subprocess.run) -> str | None:
    """Push the user files when they changed since ``last``; returns the new stamp (or ``last`` on failure)."""
    s = settings(cfg)
    if not s["host"] or not s["auto_sync"]:
        return last
    now = stamp()
    if now == last:
        return last
    try:
        n = push_user_files(s, runner)
        log.info("synced %d user files to the remote runner", n)
        return now
    except Exception as e:                       # offline, asleep, no key: try again next cycle
        log.warning("remote sync failed: %s", type(e).__name__)
        return last


# ---- verified sync: compare every file by content, repair what differs --------------------------

CHECK_EVERY = 1800          # seconds between full comparisons when nothing changed locally
STALE_AFTER = 1800          # the remote runner should have run within this many seconds


def code_files() -> list[Path]:
    files = [p for p in (paths.ROOT / "src").rglob("*.py") if "__pycache__" not in p.parts]
    files += [paths.ROOT / c for c in CODE[1:] if (paths.ROOT / c).is_file()]
    return sorted(files)


def local_manifest() -> dict[str, str]:
    import hashlib
    return {f.relative_to(paths.ROOT).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in code_files() + user_files()}


def remote_state(s: dict, runner=subprocess.run) -> tuple[dict[str, str], float | None, str]:
    """(path -> sha256 on the remote, seconds since its last run or None, last line of its run log)."""
    cmd = (f"cd {s['dir']} && {{ find src -name '*.py' -type f; "
           "ls config.default.yaml requirements-server.txt watchlist.sample.yaml watchlist.yaml data/config.yaml "
           "data/plans.yaml 2>/dev/null; "
           "find data/imports/robinhood -maxdepth 1 -type f \\( -iname '*.csv' -o -iname '*.xlsx' \\) 2>/dev/null; } "
           "| sort | xargs -d '\\n' sha256sum; echo ---; stat -c %Y data/cron.log 2>/dev/null || echo 0; date +%s; "
           "tail -n 1 data/cron.log 2>/dev/null")
    head, _, tail = run(s["host"], cmd, runner=runner).partition("---")
    found = {}
    for line in head.splitlines():
        h, _, p = line.partition("  ")
        if len(h) == 64 and p:
            found[p.strip().lstrip("*").removeprefix("./")] = h
    t = [x for x in tail.strip().splitlines()]
    try:
        last, now = float(t[0]), float(t[1])
        age = now - last if last > 0 else None
    except (IndexError, ValueError):
        age = None
    return found, age, (t[2] if len(t) > 2 else "")


def diff(local: dict[str, str], found: dict[str, str]) -> tuple[list[str], list[str]]:
    """(paths that are missing or different on the remote, paths that only exist on the remote)."""
    return (sorted(p for p, h in local.items() if found.get(p) != h), sorted(p for p in found if p not in local))


def repair(s: dict, changed: list[str], extra: list[str], runner=subprocess.run) -> None:
    code = {f.relative_to(paths.ROOT).as_posix() for f in code_files()}
    if any(p in code for p in changed) or any(p.startswith("src/") for p in extra):
        push_code(s, runner)
        if "requirements-server.txt" in changed:
            run(s["host"], f"cd {s['dir']} && .venv/bin/python -m pip install -q -r requirements-server.txt",
                timeout=900, runner=runner)
    if any(p not in code for p in changed):
        push_user_files(s, runner)
    gone = [p for p in extra if p.startswith("data/imports/robinhood/") and "'" not in p]
    if gone:                                       # a report the user removed here
        run(s["host"], f"cd {s['dir']} && rm -f -- " + " ".join(f"'{p}'" for p in gone), runner=runner)


def keep_in_sync(cfg: dict | None, state: dict | None, runner=subprocess.run, now: float | None = None) -> dict | None:
    """Make the remote copy identical to this one and report it. Cheap unless something changed here or
    ``CHECK_EVERY`` seconds passed. Returns the new state: {stamp, checked_at, status: (level, message)}."""
    import time as _time
    s = settings(cfg)
    if not s["host"] or not s["auto_sync"]:
        return None
    now = now or _time.time()
    st = stamp(code_files() + user_files())
    if state and state.get("stamp") == st and now - state.get("checked_at", 0) < CHECK_EVERY \
            and state.get("status", ("", ""))[0] == "ok":
        return state
    host = s["host"]
    try:
        local = local_manifest()
        found, age, _ = remote_state(s, runner)
        changed, extra = diff(local, found)
        fixed = 0
        if changed or [p for p in extra if p.startswith(("src/", "data/imports/"))]:
            repair(s, changed, extra, runner)
            fixed = len(changed)
            found, age, _ = remote_state(s, runner)
            changed, extra = diff(local, found)
        when = f"{_time.strftime('%H:%M', _time.localtime(now))} 核对"
        if changed:
            status = ("error", f"远程同步：{host} 上有 {len(changed)} 个文件和这台电脑不一致（{'、'.join(changed[:3])}），下一轮重试")
        elif age is None or age > STALE_AFTER:
            ago = "还没有运行记录" if age is None else f"已 {age / 60:.0f} 分钟没有运行"
            status = ("error", f"远程同步：文件一致（{len(local)} 个），但 {host} 的定时任务{ago}，提醒邮件可能发不出")
        else:
            status = ("ok", f"远程同步：与 {host} 完全一致（{len(local)} 个文件逐个比对，{when}"
                            + (f"，刚更新了 {fixed} 个" if fixed else "") + f"）；那边 {age / 60:.0f} 分钟前跑过一轮")
        log.info("remote check: %s", status[1])
        return {"stamp": st, "checked_at": now, "status": status}
    except Exception as e:                           # offline, asleep, no key: say so and retry next cycle
        log.warning("remote check failed: %s", type(e).__name__)
        return {"stamp": None, "checked_at": now,
                "status": ("error", f"远程同步：连不上 {host}（{type(e).__name__}），下一轮重试；连上后会自动补齐")}
