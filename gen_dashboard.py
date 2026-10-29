# -*- coding: utf-8 -*-
"""云端版每日基金看板生成器（纯 Python + 东方财富免费接口，不依赖万得/WorkBuddy）。

取数：东方财富基金历史净值接口（单位净值 DWJZ / 累计净值 LJJZ），免费、无需鉴权。
分析：基于风险指标（夏普/索提诺/卡玛/最大回撤）的加减仓建议 + 东方财富实时榜单扩池后的数据驱动筛选 + 万得Alice（本地生成）优选推荐。
产出：自包含离线 HTML（内联 Chart.js），输出 index.html，供 GitHub Pages 发布。

用法：python3 gen_dashboard.py
"""
import os, json, re, datetime, time, threading, urllib.request, urllib.error
import concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "fund_dashboard_template.html")
OUTPUT = os.path.join(HERE, "index.html")
CONFIG = os.path.join(HERE, "funds_config.json")

PALETTE = ['#e23b3b', '#f46036', '#7b2cbf', '#2e86ab', '#1faa59', '#e8a33d', '#16a085']

# 候选池构建（B 方案）：东方财富实时榜单按类别取规模/业绩 Top（约数百只），
# 经质量门槛（规模≥5亿、近1年/近2年正收益）过滤后，对决赛圈基金拉取真实净值
# 计算风险指标综合打分。候选池不再写死，由公开数据动态生成，比原 20 只手选更全更可靠。
# 费率取自榜单返回的「管理费/托管费/销售服务费」字段（公开披露、动态获取，无需静态录入）。
# 另含少量海外/黄金「卫星」基金（小名单），仍经同一风险引擎打分，保证资产类别覆盖。
CATEGORIES = [
    ("gp", "股票型·主动", "主动权益"),
    ("hh", "混合型",     "混合·均衡"),
    ("zq", "债券型",     "债券·稳健"),
    ("zs", "指数型",     "指数·宽基/行业"),
]
SATELLITES = [
    ("270042", "广发纳斯达克100指数",   "海外·QDII", 1.00),
    ("000071", "华夏恒生ETF联接A",       "海外·港股", 0.60),
    ("000216", "华安黄金易ETF联接A",     "黄金",      0.60),
    ("002610", "博时黄金ETF联接C",       "黄金",      0.70),
]
UNI_PER_CAT   = 60      # 每类榜单取前 N 只（按规模）
UNI_MIN_SCALE = 5.0     # 规模下限（亿元），过滤迷你/清盘风险
FINAL_PER_CAT = 6       # 每类进入决赛圈（拉真实净值算风险指标）的数量
MAX_PER_CAT   = 2       # 最终推荐每类最多几只，保证分散

# 性能相关（云端 GitHub Actions 单次运行时长直接决定微信推送延迟）
NAV_LOOKBACK_DAYS = 420  # 净值回溯天数（≈285 个交易日，足够算 252 日风险指标）
NAV_MAX_WORKERS   = 6    # 净值并发数（对东方财富保持温和，避免被限流）
HTTP_TIMEOUT      = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 数据驱动筛选权重（用于综合打分）
W_SHARPE = 0.40
W_MDD = 0.35
W_FEE = 0.25


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _http_get(url, referer, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _http_get_retry(url, referer, tries=2, timeout=HTTP_TIMEOUT):
    """轻量重试：进程内复用连接，避免为每次请求 fork 一个 curl 进程（Actions 上开销明显）。"""
    for i in range(tries):
        try:
            t = _http_get(url, referer, timeout)
            if t and t.strip():
                return t
        except Exception:
            pass
        if i + 1 < tries:
            time.sleep(0.6 * (i + 1))
    return None


def _ts_to_date(ms):
    """东方财富时间戳为北京时间当日 0 点，按 UTC+8 还原成 YYYY-MM-DD。"""
    return datetime.datetime.fromtimestamp(
        ms / 1000.0, datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")


def fetch_nav_pz(code, start_date):
    """pingzhongdata 单请求返回该基金全历史净值（单位净值 + 累计净值），比 lsjz 分页快约 10 倍。"""
    url = "https://fund.eastmoney.com/pingzhongdata/%s.js?v=%d" % (code, int(time.time() * 1000))
    text = _http_get_retry(url, "https://fund.eastmoney.com/")
    if not text or "Data_netWorthTrend" not in text:
        return [], [], []
    try:
        m1 = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S)
        if not m1:
            return [], [], []
        trend = json.loads(m1.group(1))
        ac_map = {}
        m2 = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S)
        if m2:
            try:
                for it in json.loads(m2.group(1)):
                    try:
                        ac_map[int(it[0])] = float(it[1])
                    except Exception:
                        continue
            except Exception:
                ac_map = {}
        rows = []
        for it in trend:
            try:
                x = int(it["x"])
                y = float(it["y"])
            except Exception:
                continue
            if y <= 0:
                continue
            rows.append((_ts_to_date(x), y, ac_map.get(x) or y))
        if not rows:
            return [], [], []
        rows.sort(key=lambda r: r[0])
        kept = [r for r in rows if r[0] >= start_date]
        if len(kept) < len(rows):          # 多留 1 条窗口前值，保证首日收益可算
            i = len(rows) - len(kept) - 1
            if i >= 0:
                kept = [rows[i]] + kept
        rows = kept or rows[-300:]
    except Exception:
        return [], [], []
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def fetch_nav_lsjz(code, start_date):
    """备用通道：f10/lsjz 历史净值分页（单页上限 20 条）。"""
    rows = []
    page = 1
    while page <= 60:
        ts = int(time.time() * 1000)
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=%d&pageSize=20&_=%d"
               % (code, page, ts))
        text = _http_get_retry(url, "http://fundf10.eastmoney.com/")
        if not text:
            break
        try:
            data = json.loads(text)
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


_NAV_CACHE = {}
_NAV_LOCK = threading.Lock()


def fetch_nav(code, start_date=None):
    """净值序列（升序 dates / dwjz / ljjz）。结果进程内缓存，持仓与决赛圈共用同一份数据。"""
    with _NAV_LOCK:
        if code in _NAV_CACHE:
            return _NAV_CACHE[code]
    if start_date is None:
        start_date = (datetime.date.today() - datetime.timedelta(days=NAV_LOOKBACK_DAYS)).isoformat()
    res = fetch_nav_pz(code, start_date)
    if not res[0]:
        res = fetch_nav_lsjz(code, start_date)
    with _NAV_LOCK:
        _NAV_CACHE[code] = res
    return res


def _safe_fetch(args):
    code, start_date = args
    try:
        return code, fetch_nav(code, start_date)
    except Exception:
        return code, ([], [], [])


def prefetch_navs(codes, start_map=None):
    """并发预热净值缓存。start_map: code -> 起始日（可选）。返回耗时秒数。"""
    start_map = start_map or {}
    todo = [(c, start_map.get(c)) for c in dict.fromkeys(codes)
            if c and c not in _NAV_CACHE]
    if not todo:
        return 0.0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=NAV_MAX_WORKERS) as ex:
        list(ex.map(_safe_fetch, todo))
    return round(time.time() - t0, 1)


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


def daily_returns(series):
    return [series[i] / series[i - 1] - 1 for i in range(1, len(series)) if series[i - 1]]


def _mean(x):
    return sum(x) / len(x) if x else 0.0


def _std(x):
    n = len(x)
    if n < 2:
        return 0.0
    m = _mean(x)
    return (sum((v - m) ** 2 for v in x) / (n - 1)) ** 0.5


def ann_vol_pct(returns):
    return round(_std(returns) * (252 ** 0.5) * 100, 1)


def ann_ret_ari(returns):
    return _mean(returns) * 252


def sharpe(returns, rf=0.02):
    v = _std(returns)
    if v <= 0:
        return 0.0
    return (ann_ret_ari(returns) - rf) / (v * (252 ** 0.5))


def sortino(returns, rf=0.02):
    downs = [r for r in returns if r < 0]
    dd = _std(downs)
    if dd <= 0:
        return 0.0
    return (ann_ret_ari(returns) - rf) / (dd * (252 ** 0.5))


def cagr(series, n):
    if n < 2 or series[0] <= 0:
        return 0.0
    return (series[-1] / series[0]) ** (252.0 / n) - 1


def calmar(cagr_val, mdd_pct):
    if mdd_pct >= 0:
        return 0.0
    return round(cagr_val / abs(mdd_pct / 100.0), 2)


def risk_metrics(series):
    """基于近1年累计净值序列，算年化波动/夏普/索提诺/卡玛/最大回撤（无需基准）。"""
    n = min(len(series), 252)
    win = series[-n:]
    rets = daily_returns(win)
    vol = ann_vol_pct(rets)
    sh = round(sharpe(rets), 2)
    so = round(sortino(rets), 2)
    mdd = max_drawdown(win)           # 负值百分比
    cal = calmar(cagr(win, n), mdd)
    return {"vol": vol, "sharpe": sh, "sortino": so, "calmar": cal, "mdd": mdd}


def decision(hold_ret, r):
    """基于风险调整指标给出持有/控仓建议（非择时预测，仅供参考）。"""
    sh, vol, cal, so, mdd = r["sharpe"], r["vol"], r["calmar"], r["sortino"], r["mdd"]
    if hold_ret < 0 and mdd <= -20:
        return ("bear", "控仓 / 观望",
                "自买入 %+.1f%%，近1年最大回撤 %d%%，波动偏大，建议控仓等待企稳" % (hold_ret, mdd))
    if sh < 0:
        return ("bear", "减仓 / 换仓考量",
                "夏普 %.2f（风险调整后收益为负）、近1年回撤 %d%%，可考虑减仓或换基" % (sh, mdd))
    if sh >= 1.0 and hold_ret >= 0:
        return ("bull", "持有（风险收益佳）",
                "夏普 %.2f、卡玛 %.2f、年化波动 %d%%，风险收益比健康，可继续持有" % (sh, cal, vol))
    if hold_ret >= 0:
        return ("neutral", "持有 / 中性",
                "自买入盈利 %+.1f%%，夏普 %.2f，可持有观察" % (hold_ret, sh))
    return ("neutral", "观望 / 分批",
            "自买入 %+.1f%%，夏普 %.2f、回撤 %d%%，暂观望或定投分批" % (hold_ret, sh, mdd))


def risk_summary(r):
    return "夏普 %.2f · 年化波动 %d%% · 卡玛 %.2f · 索提诺 %.2f" % (
        r["sharpe"], r["vol"], r["calmar"], r["sortino"])


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
    rsk = risk_metrics(ljjz)
    typ, action, adv = decision(hold_ret, rsk)
    hv = risk_summary(rsk)
    return {
        "code": code, "name": name, "short": name,
        "color": PALETTE[idx % len(PALETTE)],
        "nav": nav, "ret": ret, "mdd": mdd,
        "action": action, "type": typ, "adv": adv,
        "buyDate": buy_date, "buyNav": round(buy_nav, 4),
        "holdRet": hold_ret, "holdDays": hold_days,
        "holdVerdict": hv, "risk": rsk,
        "navSeries": {"dates": dates, "vals": dwjz},
        "accVals": ljjz,
    }


def screen_metrics(ljjz, fee_pct):
    """基于近1年累计净值序列算风险指标 + 综合费率。"""
    n = min(len(ljjz), 252)
    win = ljjz[-n:]
    rets = daily_returns(win)
    vol = ann_vol_pct(rets)
    sh = round(sharpe(rets), 2)
    so = round(sortino(rets), 2)
    mdd = max_drawdown(win)
    ar = round(cagr(win, n) * 100, 2)  # 年化收益 %
    return {"vol": vol, "sharpe": sh, "sortino": so, "mdd": mdd, "annRet": ar, "fee": fee_pct}


def _norm(vals):
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.5 for _ in vals]
    return [(v - lo) / (hi - lo) for v in vals]


def _pct(s):
    if not s:
        return None
    s = str(s).replace('%', '').strip()
    try:
        return float(s)
    except Exception:
        return None


def fetch_ranking(ft, pn=200, pi=1):
    """东方财富基金排行榜（按类别取前 N 只，含规模/费率/多周期收益），免费、无需鉴权。"""
    ed = datetime.date.today().isoformat()
    sd = (datetime.date.today() - datetime.timedelta(days=370)).isoformat()
    ts = int(time.time() * 1000)
    url = ("https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=%s&rs=&gs=0"
           "&sc=1nzf&st=desc&sd=%s&ed=%s&qdii=&tabSubtype=,,,,,"
           "&pi=%d&pn=%d&dx=1&_=%d" % (ft, sd, ed, pi, pn, ts))
    text = _http_get_retry(url, "https://fund.eastmoney.com/", tries=3)
    return parse_ranking(text) if text else []


def parse_ranking(text):
    """解析 rankhandler 返回的逗号分隔数据；字段：0代码 1名称 11近1年 12近2年 13近3年 18规模(亿) 19管理费 20托管费 22销售服务费。"""
    m = re.search(r'datas:\[(.*)\]', text, re.S)
    if not m:
        return []
    items = [it.strip('"') for it in m.group(1).split('","')]
    out = []
    for it in items:
        f = it.split(',')
        if len(f) < 24:
            continue
        try:
            code = f[0].strip()
            name = f[1].strip()
            if not code or not name:
                continue
            scale = _pct(f[18])
            y1, y2, y3 = _pct(f[11]), _pct(f[12]), _pct(f[13])
            fee = (_pct(f[19]) or 0) + (_pct(f[20]) or 0) + (_pct(f[22]) or 0)
            out.append({"code": code, "name": name, "scale": scale,
                        "y1": y1, "y2": y2, "y3": y3, "fee": round(fee, 3)})
        except Exception:
            continue
    return out


def build_universe():
    """东方财富实时榜单扩池：按类别取规模 Top，加质量门槛；附海外/黄金卫星。返回候选列表。
    4 个类别榜单并发拉取，顺序保持稳定以便结果可复现。"""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CATEGORIES)) as ex:
        futs = {ex.submit(fetch_ranking, ft, UNI_PER_CAT, 1): ft for ft, _, _ in CATEGORIES}
        for f in concurrent.futures.as_completed(futs):
            ft = futs[f]
            try:
                results[ft] = f.result()
            except Exception:
                results[ft] = []
    uni = []
    for ft, cat_label, cat_key in CATEGORIES:
        for r in results.get(ft, []):
            r["cat"] = cat_key
            uni.append(r)
    for code, name, cat, fee in SATELLITES:
        uni.append({"code": code, "name": name, "cat": cat, "curated": True,
                    "scale": None, "y1": None, "y2": None, "y3": None, "fee": fee})
    return uni


def screen_universe(held_codes, dump_path=None):
    """数据驱动筛选（B）：榜单扩池 → 质量门槛 → 决赛圈拉真实净值算风险指标 → 综合打分 → 每类≤2只。
    返回 (recs, candidates)；candidates 写入 dump_path 供本地万得Alice生成推荐（云端不调用万得）。"""
    uni = build_universe()
    cands = []
    for r in uni:
        if r["code"] in held_codes:
            continue
        if r.get("curated"):           # 卫星基金（海外/黄金）直入，跳过规模/收益门槛
            cands.append(r)
            continue
        sc = r.get("scale")
        if sc is not None and sc < UNI_MIN_SCALE:
            continue
        y1, y2, y3 = r.get("y1"), r.get("y2"), r.get("y3")
        if y1 is None or y1 <= 0:
            continue
        if (y2 is None or y2 <= 0) and (y3 is None or y3 <= 0):
            continue
        cands.append(r)
    if not cands:
        return [], []
    # 每类取规模前 FINAL_PER_CAT 作为决赛圈（拉真实净值算风险）
    by_cat = {}
    for r in cands:
        by_cat.setdefault(r["cat"], []).append(r)
    finalists = []
    for cat, lst in by_cat.items():
        lst.sort(key=lambda x: (x.get("scale") or 0), reverse=True)
        finalists += lst[:FINAL_PER_CAT]
    # 计算真实风险指标（决赛圈净值并发预热后再逐只计算）
    t_pre = prefetch_navs([r["code"] for r in finalists])
    scored = []
    for r in finalists:
        try:
            dates, dwjz, ljjz = fetch_nav(r["code"])
            if len(ljjz) < 180:   # 至少约9个月数据才有意义
                continue
            m = screen_metrics(ljjz, r.get("fee") if r.get("fee") is not None else 0.0)
            r["m"] = m
            scored.append(r)
        except Exception:
            continue
    print("[info] 决赛圈 %d 只，净值并发拉取 %.1fs" % (len(finalists), t_pre))
    if dump_path:
        try:
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump([{"code": r["code"], "name": r["name"], "cat": r["cat"],
                            "scale": r.get("scale"), "y1": r.get("y1"), "y3": r.get("y3"),
                            "fee": r.get("fee"), "m": r.get("m")} for r in scored],
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    if not scored:
        return [], []
    n_sh = _norm([x["m"]["sharpe"] for x in scored])
    n_md = _norm([-x["m"]["mdd"] for x in scored])
    n_fe = _norm([-(x.get("fee") or 0) for x in scored])
    for i, x in enumerate(scored):
        x["score"] = round(W_SHARPE * n_sh[i] + W_MDD * n_md[i] + W_FEE * n_fe[i], 3)
    scored.sort(key=lambda x: x["score"], reverse=True)

    def take(qualified_only):
        pick, per = [], {}
        for x in scored:
            if qualified_only and not (x["m"]["sharpe"] >= 0 and x["m"]["mdd"] >= -20):
                continue
            if len(pick) >= 6:
                break
            c = x["cat"]
            if per.get(c, 0) >= MAX_PER_CAT:
                continue
            per[c] = per.get(c, 0) + 1
            pick.append(x)
        return pick

    pick = take(True)
    if len(pick) < 6:           # 合格不足6只时放宽补满（忽略类别上限）
        for x in scored:
            if len(pick) >= 6:
                break
            if x not in pick:
                pick.append(x)
    res = []
    for x in pick:
        m = x["m"]
        res.append({
            "code": x["code"], "name": x["name"], "cat": x["cat"],
            "meta": "%s ｜ 规模%s亿 ｜ 年化收益 %+.1f%% / 近1年回撤 %+.1f%% / 夏普 %.2f / 索提诺 %.2f / 综合费率 %.2f%%"
                  % (x["cat"], ("%.1f" % x["scale"]) if x.get("scale") else "?",
                     m["annRet"], m["mdd"], m["sharpe"], m["sortino"], (x.get("fee") or 0)),
            "p": ("风险调整后收益为正、回撤可控、费率偏低，与现有组合（偏权益+黄金）互补，可分散波动、分批配置。"
                  if m["sharpe"] >= 0 else
                  "当前风险调整后收益为负，仅作观察，不建议急于建仓。"),
            "score": x["score"], "alice": None,
        })
    return res, scored


def load_recommendations():
    """读取本地万得Alice生成的推荐（可选；缺失不影响云端自动推送）。"""
    p = os.path.join(HERE, "recommendations.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"picks": [], "overall": "", "generatedAt": ""}


def render(template, asof, funds, recs, start_map, expert, reco):
    sub = "数据日期：%s ｜ 覆盖 %d 只自选基金 ｜ 红涨绿跌（中国市场惯例）｜ 推荐=东方财富实时榜单扩池+风险指标打分+万得Alice优选，仅供参考" % (asof, len(funds))
    start_info = " ｜ ".join("%s %s" % (c, d) for c, d in start_map.items())
    reco_picks = {p.get("code"): p for p in (reco.get("picks") or [])}
    out = template
    out = out.replace("__SUBTITLE__", sub)
    out = out.replace("__STARTINFO__", start_info)
    out = out.replace("__START_MAP__", json.dumps(start_map, ensure_ascii=False))
    out = out.replace("__FUNDS__", json.dumps(funds, ensure_ascii=False))
    out = out.replace("__RECS__", json.dumps(recs, ensure_ascii=False))
    out = out.replace("__EXPERT_JSON__", json.dumps(expert, ensure_ascii=False))
    out = out.replace("__RECO_JSON__", json.dumps(
        {"overall": reco.get("overall", ""), "generatedAt": reco.get("generatedAt", ""),
         "picks": reco_picks}, ensure_ascii=False))
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(out)
    print("[ok] 生成 %s (%d bytes)" % (OUTPUT, os.path.getsize(OUTPUT)))


def load_expert():
    """读取本地万得Alice生成的专家附注（可选；缺失不影响云端自动推送）。"""
    p = os.path.join(HERE, "expert_notes.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"funds": {}, "overall": "", "generatedAt": ""}


def main():
    T0 = time.time()
    cfg_list = load_config()
    start_map = {c["code"]: c.get("buyDate", "") for c in cfg_list}
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    # 持仓净值并发预热：起始日取「回溯窗口」与「买入日前 30 天」中更早者，保证买入点一定落在序列内
    pre_start = {}
    for c in cfg_list:
        sd = (datetime.date.today() - datetime.timedelta(days=NAV_LOOKBACK_DAYS)).isoformat()
        b = c.get("buyDate") or ""
        if b:
            try:
                bd = (datetime.date.fromisoformat(b) - datetime.timedelta(days=30)).isoformat()
                if bd < sd:
                    sd = bd
            except Exception:
                pass
        pre_start[c["code"]] = sd
    t_nav = prefetch_navs(list(pre_start), pre_start)
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
    print("[info] 持仓 %d 只，净值并发拉取 %.1fs" % (len(funds), t_nav))
    asof = max(f["navSeries"]["dates"][-1] for f in funds)
    recs, candidates = screen_universe({f["code"] for f in funds},
                                      os.path.join(HERE, "screen_candidates.json"))
    expert = load_expert()
    reco = load_recommendations()
    # 万得Alice推荐存在时，以其精选列表作为主推荐（候选池仍由榜单扩池+风险筛选产生）；
    # 缺失时退回数据驱动筛选结果，保证关机也能正常推送。
    if reco.get("picks"):
        cmap = {c["code"]: c for c in candidates}
        disp = []
        for p in reco["picks"]:
            c = cmap.get(p.get("code"))
            if c:
                m = c["m"]
                meta = p.get("meta") or ("%s ｜ 规模%s亿 ｜ 年化收益 %+.1f%% / 近1年回撤 %+.1f%% / 夏普 %.2f / 索提诺 %.2f / 综合费率 %.2f%%"
                       % (c["cat"], ("%.1f" % c["scale"]) if c.get("scale") else "?",
                          m["annRet"], m["mdd"], m["sharpe"], m["sortino"], c.get("fee") or 0))
                name = p.get("name") or c["name"]
                cat = c["cat"]
            else:
                meta = p.get("meta") or ""
                name = p.get("name", "")
                cat = p.get("cat", "")
            disp.append({"code": p.get("code"), "name": name, "cat": cat, "meta": meta,
                         "p": p.get("reason", "") or "万得Alice 精选推荐。", "score": None, "alice": True})
        if disp:
            recs = disp
    render(template, asof, funds, recs, start_map, expert, reco)
    src = "万得Alice精选" if reco.get("picks") else "数据驱动筛选"
    print("[done] asOf=%s 推荐%d只(%s) 专家附注=%s 万得推荐=%s" % (
        asof, len(recs), src, expert.get("generatedAt") or "无", reco.get("generatedAt") or "无"))
    print("[time] 总耗时 %.1fs" % (time.time() - T0))


if __name__ == "__main__":
    main()
