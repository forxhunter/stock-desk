r"""Copy the mail authorisation code (and the IBKR Flex token) from this PC's credential vault to the
always-on machine, so that it can send the alert mails and read the Flex report.

    .venv\Scripts\python.exe scripts\push_secrets.py

Run it yourself: it sends the values straight through ssh into a file only your account there can
read (~/.config/stockdesk/secrets.json). Nothing is printed, logged or written on this PC. Afterwards
it switches mail sending to that machine (notify.email.send_from: remote).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stockdesk import credentials, notify, remote      # noqa: E402
from stockdesk.config import Config                    # noqa: E402


def main() -> int:
    cfg = Config.load()
    s = remote.settings(cfg.get("remote", {}))
    if not s["host"]:
        print("data/config.yaml 里还没有 remote.host")
        return 1
    names = {notify.KEY_SMTP_SECRET: "邮件授权码", credentials.KEY_FLEX_TOKEN: "IBKR Flex token"}
    found = {k: credentials.get_secret(k) for k in names}
    found = {k: v for k, v in found.items() if v}
    if notify.KEY_SMTP_SECRET not in found:
        print("这台电脑的凭据管理器里没有邮件授权码，先在 分析中心 → 数据源与设置 里填好并测试。")
        return 1
    remote.run(s["host"], "umask 077 && mkdir -p ~/.config/stockdesk && cat > ~/.config/stockdesk/secrets.json "
                          "&& chmod 600 ~/.config/stockdesk/secrets.json", json.dumps(found).encode())
    print("已送到 " + s["host"] + "：" + "、".join(names[k] for k in found))
    cfg.set("notify.email.send_from", "remote")
    cfg.save()
    remote.push_user_files(s)
    print("邮件改由那台机器发送，这台电脑不再发（避免重复）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
