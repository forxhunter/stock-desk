"""The English interface: nothing Chinese may be left in any page, alert or mail."""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_no_chinese_left_in_english_mode():
    env = {**os.environ, "STOCKDESK_LANG": "en", "PYTHONIOENCODING": "utf-8", "QT_QPA_PLATFORM": "offscreen"}
    r = subprocess.run([sys.executable, str(HERE / "en_smoke.py")], capture_output=True, env=env, timeout=300)
    out = r.stdout.decode("utf-8", "replace").strip()
    assert r.returncode == 0 and out.endswith("OK"), out + "\n" + r.stderr.decode("utf-8", "replace")[-2000:]


def test_language_choice(monkeypatch, tmp_path):
    from stockdesk import i18n, paths
    monkeypatch.setenv("STOCKDESK_LANG", "en")
    assert i18n.detect() == "en"
    monkeypatch.setenv("STOCKDESK_LANG", "")
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(paths, "USER_CONFIG", cfg)
    cfg.write_text("app:\n  language: zh\n", encoding="utf-8")
    assert i18n.detect() == "zh"
    cfg.write_text("app:\n  language: en\n", encoding="utf-8")
    assert i18n.detect() == "en"
    cfg.write_text("app:\n  language: auto\n", encoding="utf-8")
    assert i18n.detect() in ("zh", "en")                    # falls back to the system locale
    assert i18n.L("中", "EN") == ("EN" if i18n.LANG == "en" else "中")
