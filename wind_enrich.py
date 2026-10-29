# -*- coding: utf-8 -*-
"""本地运行（需万得Alice积分）：一次合并调用生成 expert_notes.json + recommendations.json。

流程：读取现有7只持仓 + screen_candidates.json（东方财富扩池决赛圈,含真实风险指标）
      → 构造合并 prompt → 调用万得Alice → 解析 JSON → 拆成两个文件。
万得返回形如 {"expert":{...}, "reco":{...}}；云端每日推送只读这两个文件，不调用万得，关机照常。

前置：先运行 `python3 gen_dashboard.py` 生成 screen_candidates.json（本脚本依赖它）。
用法：python3 wind_enrich.py
"""
import os, json, re, subprocess, sys, datetime, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "funds_config.json")
CAND = os.path.join(HERE, "screen_candidates.json")
EXPERT_OUT = os.path.join(HERE, "expert_notes.json")
RECO_OUT = os.path.join(HERE, "recommendations.json")
PROMPT_FILE = os.path.join(HERE, "alice_unified_prompt.txt")
RAW_OUT = os.path.join(HERE, "alice_raw_output.txt")


def find_cli():
    """定位万得Alice CLI：优先 PATH 命令，否则探测 skill cache 里的 cli.mjs。"""
    if shutil.which("alice-financial-copilot"):
        return ["alice-financial-copilot"]
    base = os.path.expanduser("~/.workbuddy/plugins/cache/experts/alice-financial-copilot")
    if os.path.isdir(base):
        for root, _, files in os.walk(base):
            if os.path.basename(root) == "scripts" and "cli.mjs" in files:
                return ["node", os.path.join(root, "cli.mjs")]
    return None


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def load_candidates():
    if not os.path.exists(CAND):
        return []
    with open(CAND, encoding="utf-8") as f:
        return json.load(f)


def build_prompt(held, cands):
    held_lines = []
    for c in held:
        held_lines.append("- %s %s（买入日 %s）" % (c["code"], c.get("name", ""), c.get("buyDate", "")))
    cand_lines = []
    for c in cands:
        m = c.get("m", {})
        cand_lines.append("%s %s | %s | 年化 %+.1f%% / 回撤 %+.1f%% / 夏普 %.2f / 索提诺 %.2f / 费率 %.2f%%"
                          % (c["code"], c["name"], c.get("cat", ""),
                             m.get("annRet", 0), m.get("mdd", 0), m.get("sharpe", 0),
                             m.get("sortino", 0), c.get("fee", 0)))
    prompt = """你是严谨的基金配置顾问。请基于下方【用户现有持仓】与【候选基金池】（东方财富实时榜单扩池后经真实净值风险指标筛选的决赛圈）给出权威附注与精选推荐。

【用户现有持仓（7只）】
%s

【候选基金池（决赛圈 %d 只，含真实风险指标；格式：代码 名称 | 类别 | 年化收益%% / 最大回撤%% / 夏普 / 索提诺 / 综合费率%%）】
%s

【请严格按以下 JSON 输出（不要在外层加任何解释文字，只输出这个 JSON）】
{
  "expert": {
    "overall": "一段整体配置建议（结合现有7只持仓的大类配置与风控，2-4句）",
    "funds": {
      "021562": {"note":"报告期/前十大持仓要点/基金经理及任职/规模摘要（1-2句）", "valuation":"跟踪指数/对标指数当前估值分位判断（低估/中性/高估+大致分位，如PE近10年约X%%；若缺失请说明）", "suggestion":"一句配置建议"},
      "020105": {"note":"...", "valuation":"...", "suggestion":"..."},
      "017641": {"note":"...", "valuation":"...", "suggestion":"..."},
      "018147": {"note":"...", "valuation":"...", "suggestion":"..."},
      "009505": {"note":"...", "valuation":"...", "suggestion":"..."},
      "001766": {"note":"...", "valuation":"...", "suggestion":"..."},
      "018099": {"note":"...", "valuation":"...", "suggestion":"..."}
    }
  },
  "reco": {
    "overall": "综合推荐建议（结合现有7只的再平衡思路，2-4句）",
    "picks": [
      {"code":"...", "name":"...", "reason":"一句推荐理由（说明为何与现有组合互补、控制单一资产集中度）"},
      {"code":"...", "name":"...", "reason":"..."}
    ]
  }
}
要求：reco.picks 精选 5-6 只，必须来自候选池（代码须为候选池中存在的代码），混合债券打底 + 权益增强 + 海外/黄金卫星，避免与现有7只过度重叠（如已持有较多A股窄行业，则不重复推荐同类）。""" % (
        "\n".join(held_lines), len(cands), "\n".join(cand_lines))
    return prompt


def extract_json(text):
    """容错提取 JSON：优先 ```json 代码块，其次裸 { }。"""
    m = re.search(r'```json\s*(.*?)```', text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r'\{.*\}', text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def extract_from_session_log(stdout):
    """兜底：CLI 有时只在 session 日志里落 agentResult.value 原文。
    从 stdout 中的 ALICE_SESSION_LOG= 路径读取日志，定位 agentResult.value: 行解析。"""
    m = re.search(r'ALICE_SESSION_LOG=(\S+)', stdout or "")
    if not m:
        return None
    log = m.group(1)
    if not os.path.isfile(log):
        return None
    for line in open(log, encoding="utf-8", errors="ignore"):
        if "agentResult.value:" in line:
            seg = line.split("agentResult.value:", 1)[1].strip()
            try:
                return json.loads(seg)
            except Exception:
                return None
    return None


def run():
    cli = find_cli()
    if not cli:
        print("[!] 未找到万得Alice CLI（alice-financial-copilot 或 skill cache/cli.mjs）")
        sys.exit(2)
    # 每次运行前通过连接器刷新凭据（零交互），避免 token 过期导致 401
    try:
        lr = subprocess.run(cli + ["login", "--from-connector"],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
        print("[*] 已刷新万得登录凭据" if (lr.returncode == 0) else "[!] 刷新凭据返回码=%d（继续尝试）" % lr.returncode)
    except Exception as e:
        print("[!] 刷新凭据失败（继续尝试）：", e)
    held = load_config()
    cands = load_candidates()
    if not cands:
        print("[!] 缺少 screen_candidates.json，请先运行 `python3 gen_dashboard.py` 生成决赛圈候选")
        sys.exit(3)
    prompt = build_prompt(held, cands)
    with open(PROMPT_FILE, "w", encoding="utf-8") as f:
        f.write(prompt)
    print("[*] 调用万得Alice（合并：权威附注 + 精选推荐），约需数分钟…")
    cmd = cli + ["--prompt", prompt, "--no-wait", "--new-session", "-d", HERE]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=2400)
    except Exception as e:
        print("[!] 调用失败:", e)
        sys.exit(4)
    stdout = p.stdout or ""
    data = extract_json(stdout) or extract_from_session_log(stdout)
    if not data:
        with open(RAW_OUT, "w", encoding="utf-8") as f:
            f.write(stdout)
        print("[!] 未能从万得返回解析出 JSON，原始输出已存 %s（可人工查看/重跑）" % RAW_OUT)
        sys.exit(5)
    expert = data.get("expert") or {}
    reco = data.get("reco") or data.get("recommendations") or {}
    today = datetime.date.today().isoformat()
    expert_out = {
        "generatedAt": today,
        "source": "万得金融大模型 Wind Alice（合并调用：权威附注 + 精选推荐）",
        "overall": expert.get("overall", ""),
        "funds": expert.get("funds", {}),
    }
    reco_out = {
        "generatedAt": today,
        "overall": reco.get("overall", ""),
        "picks": reco.get("picks", []),
    }
    with open(EXPERT_OUT, "w", encoding="utf-8") as f:
        json.dump(expert_out, f, ensure_ascii=False, indent=1)
    with open(RECO_OUT, "w", encoding="utf-8") as f:
        json.dump(reco_out, f, ensure_ascii=False, indent=1)
    print("[ok] 已生成 expert_notes.json（%d 只附注）与 recommendations.json（%d 只推荐）"
          % (len(expert_out["funds"]), len(reco_out["picks"])))


if __name__ == "__main__":
    run()
