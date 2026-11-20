"""Drawdown scenarios and the playbook: the record of each rung, the stress arithmetic, the user's own actions."""
import numpy as np
import pandas as pd

from stockdesk import playbook
from stockdesk.quant import scenario as sc

from conftest import make_bars
from test_engine_misc import env  # noqa: F401  (fixture)
from test_keylevels import _index_market


def path_of(*legs, start=100.0):
    """A close series made of straight legs: (target, sessions) ..."""
    out, px = [], start
    for target, n in legs:
        out += list(np.linspace(px, target, n + 1)[1:])
        px = target
    return pd.Series(out, index=pd.bdate_range("2000-01-03", periods=len(out)))


def test_episodes_rungs_and_what_followed():
    c = path_of((100, 300), (88, 30), (101, 60),            # -12 %, recovered
                (120, 200), (93.6, 40), (121, 120),         # -22 %, recovered
                (140, 200), (122, 30), (141, 60),           # -12.9 %, recovered
                (160, 200), (140, 25))                      # open: -12.5 %
    eps = sc.episodes(c)
    assert len(eps) == 4 and [round(e.depth) for e in eps] == [-12, -22, -13, -12] and eps[-1].end is None
    assert set(eps[1].cross) == {10, 15, 20} and all(eps[1].cross[a] < eps[1].cross[b] for a, b in ((10, 15), (15, 20)))
    st = sc.study(c)
    assert st.n == 3 and st.current is not None and 10 in st.dates and 15 not in st.dates
    r10, r15, r20 = st.rungs[10], st.rungs[15], st.rungs[20]
    assert (r10.n, r15.n, r20.n) == (3, 1, 1) and r10.next_rung == 15 and r10.deeper == 1 / 3 and r15.deeper == 1.0
    assert r10.more[0] < 0 and r10.to_low > 0 and r10.to_peak > r10.to_low
    assert r10.fwd[126][2] == 1.0 and r10.fwd[21][0] == 3            # all three were higher half a year on
    assert st.rungs[25].n == 0 and sc.study(c.iloc[:200]).n == 0
    assert sc.ladder(-12.5) == [10, 15, 20, 25] and sc.ladder(-16.5) == [15, 20, 25, 30] and sc.ladder(-3.0) == [10, 15, 20, 25]


def test_beta_and_stress_are_plain_arithmetic():
    idx = pd.bdate_range("2024-01-01", periods=300)
    rng = np.random.default_rng(5)
    r = rng.normal(0, 0.01, 300)
    index = pd.Series(100 * np.exp(np.cumsum(r)), index=idx)
    double = pd.Series(100 * np.exp(np.cumsum(2 * r)), index=idx)
    assert abs(sc.beta(double, index) - 2.0) < 0.1 and abs(sc.beta(double, index, weekly=True) - 2.0) < 0.2
    assert sc.beta(double.tail(20), index) is None
    exps = [sc.Exposure("AAA", 0.6, 2.0, "daily", 100.0), sc.Exposure("BBB", 0.3, 0.5, "weekly", 50.0, "EUR"), sc.Exposure("CCC", 0.1, None, "daily", 10.0)]
    total, rows = sc.stress(exps, -0.05)
    assert total == (0.6 * -0.10 + 0.3 * -0.025) and rows[0] == ("AAA", -0.10, 90.0) and rows[2] == ("CCC", None, None)
    assert sc.stress([], -0.05) == (None, [])


def test_playbook_file_is_the_users_and_is_never_filled_in(tmp_path):
    p = tmp_path / "playbook.yaml"
    assert playbook.load(p) == {} and playbook.ensure_template(["^GSPC", "^IXIC"], p)
    text = p.read_text(encoding="utf-8")
    assert "由你自己写" in text and "不会下单" in text
    book = playbook.load(p)
    assert book["rules"] == "" and book["^GSPC"] == {10: "", 15: "", 20: "", 25: "", 30: ""}        # empty: nothing is suggested
    p.write_text("playbook:\n  rules: 现金不低于两成\n  '^GSPC':\n    10: 不加仓，先核对底线价\n    '15': 减到半仓\n    bad: x\n", encoding="utf-8")
    assert not playbook.ensure_template(["^GSPC"], p)                                              # never overwritten
    book = playbook.load(p)
    assert book["rules"] == "现金不低于两成" and playbook.action(book, "^GSPC", 10.0) == "不加仓，先核对底线价"
    assert playbook.action(book, "^GSPC", 15) == "减到半仓" and playbook.action(book, "^GSPC", 20) == "" and playbook.action(book, "^IXIC", 10) == ""
    p.write_text(": not yaml :::", encoding="utf-8")
    assert playbook.load(p) == {}


def test_engine_builds_the_playbook_and_quotes_it_in_the_alert(env, monkeypatch):
    from stockdesk import engine as engine_mod, remote
    from stockdesk.ui import render, theme as T
    eng, market, store, tmp = env
    _index_market(market)                                         # the S&P closes 11.5 % under its high
    monkeypatch.setattr(engine_mod.keylevels, "session_live", lambda *a, **k: False)
    snap = eng.refresh(force=True)
    assert playbook.path().exists() and playbook.path().parent == tmp / "data"
    al = next(a for a in snap.alerts if a.kind == "index_level" and ":down:" in a.id)
    assert "【你的预案】" in al.body and "你还没有写预案" in al.body
    pb = snap.playbook
    sp = pb.indices[0]
    assert sp.symbol == "^GSPC" and [r.rung for r in sp.rungs] == [10, 15, 20, 25] and sp.rungs[0].touched and not sp.rungs[1].touched
    assert sp.rungs[0].action == "" and round(sp.depth_pct, 1) == -11.5
    assert [e.symbol for e in pb.exposures] and pb.stress and pb.stress[0][0] == 15 and pb.stress[0][1] < 0
    assert playbook.path() in remote.user_files()                 # goes to the always-on machine with the other user files

    playbook.path().write_text("playbook:\n  rules: 总仓位不超过七成\n  '^GSPC':\n    10: 不新开仓；核对每只股票的底线价\n    15: ''\n    12: 自己加的一档\n",
                               encoding="utf-8")
    snap = eng.refresh(force=True)
    al = next(a for a in snap.alerts if a.kind == "index_level" and ":down:" in a.id)
    assert "你在预案里写的是：不新开仓；核对每只股票的底线价" in al.body
    assert snap.indices["^GSPC"].level("dd12") is not None        # a rung of the user's own becomes a key level
    html = render.playbook_html(snap.playbook, T.Palette())
    for part in ("大盘预案", "不新开仓；核对每只股票的底线价", "总仓位不超过七成", "（未填写）", "持仓合计", "−15%", "你的持仓大约会怎样"):
        assert part in html
    assert "不是预测，也不是买卖建议" in html
    eng.cfg.set("playbook.enabled", False)
    assert eng.refresh(force=True).playbook is None
    assert "还没有指数数据" in render.playbook_html(None, T.Palette())
