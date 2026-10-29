# -*- coding: utf-8 -*-
"""云端版每日基金看板生成器（纯 Python + 东方财富免费接口，不依赖万得/WorkBuddy）。

取数：东方财富基金历史净值接口（单位净值 DWJZ / 累计净值 LJJZ），免费、无需鉴权。
分析：基于近期动量、自买入日盈亏、最大回撤的规则化加减仓建议 + 候选基金筛选推荐。
产出：自包含离线 HTML（内联 Chart.js），输出 index.html，供 GitHub Pages 发布。

用法：python3 gen_dashboard.py
"""
import os, json, datetime, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "fund_dashboard_template.html")
OUTPUT = os.path.join(HERE, "index.html")
CONFIG = os.path.join(HERE, "funds_config.json")

PALETTE = ['#e23b3b', '#f46036', '#7b2cbf', '#2e86ab', '#1faa59', '#e8a33d', '#16a085']

# 推荐池（与现有组合互补：债券压舱石 + A股宽基），代码均经接口验证有效
RECS = [
    ("511260", "国泰上证10年国债ETF", "债券·利率压舱石"),
    ("003376", "广发中债7-10年国开债指数C", "债券·稳健配置"),
    ("005827", "易方达蓝筹精选混合", "宽基·A股核心资产"),
]


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def fetch_nav(code, start_date="2026-06-01"):
    """东方财富历史净值（分页拉取，单页上限约20条）。返回升序 (dates, dwjz, ljjz)。"""
    rows = []
    page = 1
    while page <= 80:
        ts = int(time.time() * 1000)
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=%d&pageSize=20&_=%d"
               % (code, page, ts))
        cmd = ["curl", "-sS", "-L", url,
               "-H", "Referer: http://fundf10.eastmoney.com/",
               "-H", "User-Agent: Mozilla/5.0"]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        if p.returncode != 0 or not p.stdout.strip():
            break
        try:
            data = json.loads(p.stdout)
        except Exception:
            break
        lst = (data.get("Data") or {}).get("LSJZList") or []
        if not lst:
            break
        got_old = False
        for it in lst:
            try:
                d = it["FSRQ"]
                dw = float(it["DWJZ"])
                lj = float(it["LJJZ"])
            except Exception:
                continue
            if not d:
                continue
            rows.append((d, dw, lj))
            if d <= start_date:
                got_old = True
        if got_old or len(lst) < 20:
            break
        page += 1
    rows.sort(key=lambda x: x[0])
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def find_index(dates, target):
    for i, d in enumerate(dates):
        if d >= target:
            return i
    return 0


def max_drawdown(series):
    if not series:
        return 0.0
    peak = series[0]
    mdd = 0.0
    for v in series:
        if v > peak:
            peak = v
        dd = v / peak - 1
        if dd < mdd:
            mdd = dd
    return round(mdd * 100, 2)


def window_ret(series, n, end):
    start = max(0, end - n)
    if start >= end:
        return 0.0
    return round(pct(series[end], series[start]), 2)


def ytd_ret(series, dates, end):
    yr = dates[end][:4]
    si = 0
    for i, d in enumerate(dates):
        if d[:4] == yr:
            si = i
            break
    return round(pct(series[end], series[si]), 2)


def verdict(hold_ret, typ):
    if typ == "bull":
        return "已盈利、技术面看多：可继续持有 / 分批加仓" if hold_ret >= 0 else "浮亏中、技术面看多：可逢低补仓摊薄成本"
    if typ == "bear":
        return "已盈利但技术面转弱：建议逢高减仓、锁定收益" if hold_ret >= 0 else "已浮亏且技术面走弱：建议减仓或止损观望"
    return "盈利中、中性持有" if hold_ret >= 0 else "浮亏中、中性观望"


def decide(ret, hold_ret, mdd):
    m1 = ret["m1"]
    m3 = ret["m3"]
    if m1 >= 2 and hold_ret >= 0:
        return "bull", "持有 / 小幅加仓", "近1月 %+.1f%%、自买入盈利 %+.1f%%，趋势偏强可继续持有" % (m1, hold_ret)
    if m1 <= -3 or (hold_ret <= -5 and m3 < 0):
        return "bear", "逢高减仓 / 观望", "近1月 %+.1f%%、自买入 %+.1f%%，技术面走弱建议减仓" % (m1, hold_ret)
    if hold_ret >= 0:
        return "neutral", "盈利中、中性持有", "自买入盈利 %+.1f%%，近1月 %+.1f%%，可持有观察" % (hold_ret, m1)
    return "neutral", "浮亏中、中性观望", "自买入 %+.1f%%，近1月 %+.1f%%，暂观望等待企稳" % (hold_ret, m1)


def build_fund(cfg, idx):
    code = cfg["code"]
    name = cfg.get("name") or code
    buy = cfg.get("buyDate", "")
    dates, dwjz, ljjz = fetch_nav(code)
    if not dates:
        return None
    end = len(dates) - 1
    asof = dates[end]
    nav = dwjz[end]
    bi = find_index(dates, buy) if buy else 0
    buy_date = dates[bi]
    buy_nav = dwjz[bi]
    hold_ret = round(pct(ljjz[end], ljjz[bi]), 2)  # 含分红
    try:
        hold_days = (datetime.datetime.strptime(asof, "%Y-%m-%d") -
                     datetime.datetime.strptime(buy_date, "%Y-%m-%d")).days
    except Exception:
        hold_days = 0
    ret = {
        "d5": window_ret(ljjz, 5, end),
        "m1": window_ret(ljjz, 21, end),
        "m3": window_ret(ljjz, 63, end),
        "m6": window_ret(ljjz, 126, end),
        "ytd": ytd_ret(ljjz, dates, end),
        "y1": window_ret(ljjz, 252, end),
    }
    mdd = max_drawdown(ljjz[max(0, end - 252):end + 1])
    typ, action, adv = decide(ret, hold_ret, mdd)
    hv = verdict(hold_ret, typ)
    return {
        "code": code, "name": name, "short": name,
        "color": PALETTE[idx % len(PALETTE)],
        "nav": nav, "ret": ret, "mdd": mdd,
        "action": action, "type": typ, "adv": adv,
        "buyDate": buy_date, "buyNav": round(buy_nav, 4),
        "holdRet": hold_ret, "holdDays": hold_days,
        "holdVerdict": hv,
        "navSeries": {"dates": dates, "vals": dwjz},
        "accVals": ljjz,
    }


def build_recs():
    res = []
    for code, name, meta in RECS:
        try:
            dates, dwjz, ljjz = fetch_nav(code)
            if len(ljjz) < 70:
                continue
            end = len(ljjz) - 1
            m1 = window_ret(ljjz, 21, end)
            m3 = window_ret(ljjz, 63, end)
            mdd = max_drawdown(ljjz[-126:])
            res.append({
                "code": code, "name": name,
                "meta": "%s ｜ 近1月 %+.1f%% / 近3月 %+.1f%% / 回撤 %+.1f%%" % (meta, m1, m3, mdd),
                "p": "与现有组合（偏权益+黄金）互补：债券压舱石+宽基，可降低整体波动，适合分批配置。",
            })
        except Exception:
            continue
    return res[:3]


def render(template, asof, funds, recs, start_map):
    sub = "数据日期：%s ｜ 覆盖 %d 只自选基金 ｜ 红涨绿跌（中国市场惯例）" % (asof, len(funds))
    start_info = " ｜ ".join("%s %s" % (c, d) for c, d in start_map.items())
    out = template
    out = out.replace("__SUBTITLE__", sub)
    out = out.replace("__STARTINFO__", start_info)
    out = out.replace("__START_MAP__", json.dumps(start_map, ensure_ascii=False))
    out = out.replace("__FUNDS__", json.dumps(funds, ensure_ascii=False))
    out = out.replace("__RECS__", json.dumps(recs, ensure_ascii=False))
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(out)
    print("[ok] 生成 %s (%d bytes)" % (OUTPUT, os.path.getsize(OUTPUT)))


def main():
    cfg_list = load_config()
    start_map = {c["code"]: c.get("buyDate", "") for c in cfg_list}
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    funds = []
    for i, c in enumerate(cfg_list):
        try:
            fnd = build_fund(c, i)
            if fnd:
                funds.append(fnd)
                print("[ok] %s %s" % (c["code"], c.get("name", "")))
        except Exception as e:
            print("[!] 基金 %s 取数失败: %s" % (c.get("code"), e))
    if not funds:
        raise SystemExit("无基金数据，生成中止")
    asof = max(f["navSeries"]["dates"][-1] for f in funds)
    recs = build_recs()
    render(template, asof, funds, recs, start_map)
    print("[done] asOf=%s" % asof)


if __name__ == "__main__":
    main()
