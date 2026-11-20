"""Optional e-mail notification of selected alerts (off until the user sets it up).

Mail goes only to the address the user typed into the settings page, through the SMTP server and
account they chose. The SMTP secret is kept in the OS credential vault, never in a file or a log.
The message contains market signals only: no holdings, amounts or P&L.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from . import credentials
from .i18n import L

KEY_SMTP_SECRET = "smtp_secret"
FOOTER = L("\n\n——\nStock Desk 自动发送。这是按固定规则算出的信号提醒，不是投资建议；"
           "程序只读取行情，不会下单。要停止接收，在 分析中心 → 数据源与设置 → 邮件提醒 里关闭。",
           "\n\n--\nSent automatically by Stock Desk. This is a signal alert from fixed rules, "
           "not investment advice; the program only reads market data and never places orders. "
           "To stop receiving it, turn it off under Analysis hub → Sources & settings → E-mail alerts.")


class NotifyError(Exception):
    pass


def settings(cfg: dict | None) -> dict:
    c = dict(cfg or {})
    return {"enabled": bool(c.get("enabled")), "to": str(c.get("to") or "").strip(),
            "host": str(c.get("smtp_host") or "").strip(), "port": int(c.get("smtp_port") or 587),
            "user": str(c.get("username") or "").strip(),
            "kinds": list(c.get("kinds") or ["bottoming", "plan_level", "flow", "index_level", "congress", "range"])}


def ready(s: dict) -> str | None:
    """None when mail can be sent, otherwise what is missing."""
    missing = [label for key, label in (("to", L("收件地址", "recipient address")), ("host", L("SMTP 服务器", "SMTP server")),
                                ("user", L("SMTP 用户名", "SMTP username"))) if not s[key]]
    if not credentials.has_secret(KEY_SMTP_SECRET):
        missing.append(L("SMTP 授权码", "SMTP password"))
    return L("还缺：", "Missing: ") + L("、", ", ").join(missing) if missing else None


def send(s: dict, subject: str, body: str, smtp_factory=None, secret: str | None = None) -> None:
    secret = secret if secret is not None else credentials.get_secret(KEY_SMTP_SECRET)
    if not (s["to"] and s["host"] and s["user"] and secret):
        raise NotifyError(ready(s) or L("邮件设置不完整", "E-mail settings are incomplete"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, s["user"], s["to"]
    msg.set_content(body + FOOTER)
    try:
        if smtp_factory is not None:
            server = smtp_factory(s["host"], s["port"])
        elif s["port"] == 465:
            server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(s["host"], s["port"], timeout=20)
        with server:
            if smtp_factory is None and s["port"] != 465:
                server.starttls(context=ssl.create_default_context())
            server.login(s["user"], secret)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise NotifyError(L("登录被拒绝：用户名或授权码不对（很多邮箱要用“应用专用密码 / 授权码”，不是登录密码）", "Login rejected: wrong username or password (many mailboxes need an app password / authorisation code, not the login password)")) from None
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(credentials.redact(L(f"发送失败：{type(e).__name__}", f"Sending failed: {type(e).__name__}"), secret)) from None


def alert_mail(alerts: list) -> tuple[str, str]:
    subject = alerts[0].title if len(alerts) == 1 else L(f"Stock Desk：{len(alerts)} 条新提醒", f"Stock Desk: {len(alerts)} new alerts")
    return subject, "\n\n".join(L(f"【{a.title}】\n{a.body}", f"[{a.title}]\n{a.body}") for a in alerts)
