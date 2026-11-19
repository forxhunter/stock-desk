r"""Install / update the headless runner on the always-on machine named in ``remote.host``.

    .venv\Scripts\python.exe scripts\deploy_remote.py            copy program + your files, install, add the cron job
    .venv\Scripts\python.exe scripts\deploy_remote.py --status   show the last runs over there

Uses ssh key login only. Secrets are not copied; run scripts\push_secrets.py for those.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stockdesk import remote                     # noqa: E402
from stockdesk.config import Config              # noqa: E402

PY = "$( [ -x $HOME/miniconda3/bin/python ] && echo $HOME/miniconda3/bin/python || echo python3 )"


def main() -> int:
    s = remote.settings(Config.load().get("remote", {}))
    if not s["host"]:
        print("data/config.yaml 里还没有 remote.host")
        return 1
    d = s["dir"]
    if "--status" in sys.argv:
        print(remote.run(s["host"], f"tail -n 8 {d}/data/cron.log; crontab -l | grep '{remote.MARK}'"))
        return 0
    remote.push_code(s)
    n = remote.push_user_files(s)
    print(f"已复制程序和 {n} 个用户文件到 {s['host']}:~/{d}")
    print(remote.run(s["host"], f"cd {d} && ( [ -x .venv/bin/python ] || {PY} -m venv .venv ) && "
                                ".venv/bin/python -m pip install -q -r requirements-server.txt && "
                                ".venv/bin/python -c 'import pandas, yfinance, scipy, yaml; print(\"依赖已装好\")'", timeout=900))
    line = remote.cron_line(s)
    remote.run(s["host"], f"( crontab -l 2>/dev/null | grep -v '{remote.MARK}$'; cat ) | crontab -", (line + "\n").encode())
    print("cron 已设置：每 10 分钟跑一轮")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
