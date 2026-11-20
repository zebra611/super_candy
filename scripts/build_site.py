#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 GitHub Pages 项目展示页到 site/ 目录（可复现）。

为什么需要脚本而不是手写 site/index.html：
    截图来自答辩产物（ppt_assets/），数字口径来自逐字稿。手写会漂移——
    截图更新或数字修订后，展示页悄悄过期。脚本每次从同一批源文件重建，
    路径、清单、数字都在一处，可复算。

用法：
    python scripts/build_site.py
    # 输出 site/index.html + site/assets/*.png
"""
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 答辩资产在 WorkBuddy 会话工作区，不在仓库里（两者是平级的不同根目录）。
# 用几个已知候选定位；找不到时脚本仍出页，只是图片位留空——不阻塞构建。
_CANDIDATE_WORKSPACES = [
    os.path.join(os.path.expanduser("~"), "WorkBuddy", "2026-11-01-10-59-03"),
    os.path.dirname(ROOT),
]
ASSET_SRC = ""
for _w in _CANDIDATE_WORKSPACES:
    if os.path.isdir(os.path.join(_w, "ppt_assets")):
        ASSET_SRC = os.path.join(_w, "ppt_assets")
        break
SITE = os.path.join(ROOT, "site")
ASSET_DST = os.path.join(SITE, "assets")

# (源文件名, 输出文件名, 图注)
SHOTS = [
    ("10_ui_with_profile.png", "ui.png",
     "通话终端：左侧实时对话与阶段标签，右侧客户档案与目标。"),
    ("11_full_call_closed.png", "call_closed.png",
     "一通完整闭环通话：身份核验 → 需求 → 方案 → 确认 → 回写，writeback 事件落库。"),
    ("12_multiturn_conversation.png", "multiturn.png",
     "多轮追问：客户打断、换问法、反复确认，状态机稳住环节不跳步。"),
    ("13_dashboard_seed.png", "dashboard.png",
     "指标看板（演示种子库）：会话、闭环、首字 P50/P95、成本、事件分布。横幅明示演示数据。"),
]


def copy_assets():
    os.makedirs(ASSET_DST, exist_ok=True)
    copied = []
    for src_name, dst_name, _ in SHOTS:
        src = os.path.join(ASSET_SRC, src_name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(ASSET_DST, dst_name))
            copied.append(dst_name)
        else:
            print("  [提示] 缺截图：%s（展示页会留空）" % src)
    return copied


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>super_candy · 智语外呼数字员工</title>
<style>
  :root{--blue:#2563eb;--dark:#1e293b;--sub:#64748b;--line:#e2e8f0;
        --bg:#f8fafc;--card:#fff;--green:#059669;--red:#dc2626;}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       color:var(--dark);background:var(--bg);line-height:1.7}
  header{background:var(--card);border-bottom:1px solid var(--line);padding:18px 6%;
         display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  header h1{font-size:19px;margin:0}
  header .tag{font-size:12px;color:var(--sub);border:1px solid var(--line);
              border-radius:999px;padding:2px 10px}
  .spacer{flex:1}
  header a{font-size:13px;color:var(--blue);text-decoration:none;
           border:1px solid var(--blue);border-radius:8px;padding:5px 14px}
  .wrap{max-width:1080px;margin:0 auto;padding:34px 20px 60px}
  .hero{text-align:center;padding:26px 0 14px}
  .hero h2{font-size:30px;margin:0 0 8px}
  .hero p{color:var(--sub);font-size:15px;margin:0 auto;max-width:720px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
        gap:12px;margin:30px 0}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;
       padding:16px;text-align:center}
  .kpi .v{font-size:24px;font-weight:700;color:var(--blue)}
  .kpi .l{font-size:12px;color:var(--sub);margin-top:4px}
  section{background:var(--card);border:1px solid var(--line);border-radius:12px;
          padding:22px;margin:18px 0}
  h3{font-size:18px;margin:0 0 12px;border-left:4px solid var(--blue);padding-left:10px}
  .flow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:center;
        margin:8px 0}
  .flow .n{background:#eff6ff;border:1px solid #bfdbfe;color:#1d4ed8;
           border-radius:8px;padding:8px 12px;font-size:13px}
  .flow .n.done{background:#fef2f2;border-color:#fecaca;color:var(--red);font-weight:600}
  .flow .a{color:var(--sub)}
  .note{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
        padding:10px 14px;font-size:13px;color:#92400e;margin-top:12px}
  figure{margin:20px 0}
  figure img{width:100%;border:1px solid var(--line);border-radius:10px;display:block}
  figcaption{font-size:12px;color:var(--sub);margin-top:8px;text-align:center}
  ul{margin:8px 0;padding-left:22px}
  li{margin:6px 0}
  code{background:#f1f5f9;padding:1px 6px;border-radius:4px;font-size:12px}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:720px){.two{grid-template-columns:1fr}}
  footer{text-align:center;color:var(--sub);font-size:12px;padding:20px}
  .ok{color:var(--green);font-weight:600}
</style>
</head>
<body>
<header>
  <h1>super_candy</h1>
  <span class="tag">智语外呼 · 浏览器里的数字员工</span>
  <span class="spacer"></span>
  <a href="https://github.com/zebra611/super_candy">源码仓库</a>
</header>
<div class="wrap">
  <div class="hero">
    <h2>把一通外呼电话真正打完</h2>
    <p>状态机管流程，大模型管说话。自报身份 → 核身 → 需求 → 方案 → 确认 → 结构化回写，
       闭环只以 writeback 事件为准；首字响应压在 2 秒门槛内；拒绝与沉默也留痕。</p>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="v">6+1</div><div class="l">流程阶段（DONE 为收尾态）</div></div>
    <div class="kpi"><div class="v">275</div><div class="l">行为断言全过</div></div>
    <div class="kpi"><div class="v">1.1–1.4s</div><div class="l">首字响应历史基线</div></div>
    <div class="kpi"><div class="v">0</div><div class="l">误闭环（不该闭的闭了）</div></div>
  </div>

  <section>
    <h3>架构：状态机 + 事件流单一事实源</h3>
    <div class="flow">
      <span class="n">浏览器采集/播报/打断</span><span class="a">→</span>
      <span class="n">VAD + 云端 ASR</span><span class="a">→</span>
      <span class="n">规则状态机 + 槽位</span><span class="a">→</span>
      <span class="n">云端 TTS 分段即推</span><span class="a">→</span>
      <span class="n done">writeback 回写 = 闭环</span>
    </div>
    <div class="note">主链路零第三方 Python 依赖，<code>python app.py</code> 即可跑；
      事件流（SQLite）是单一事实源，转写、质检、时延、成本都是它的派生视角。</div>
  </section>

  <section>
    <h3>真实运行画面</h3>
    __FIGURES__
  </section>

  <section>
    <h3>已落地能力与边界（不吹）</h3>
    <div class="two">
      <div>
        <p class="ok">已落地</p>
        <ul>
          <li>端侧 3A（回声消除 / 降噪 / 自动增益），与微信语音同一套技术</li>
          <li>Silero + 谱特征双引擎 VAD，挡键盘声、关门声等瞬态噪声</li>
          <li>流式 ASR（paraformer）+ 批式兜底（qwen3），边说边出字</li>
          <li>C6 噪声容错：连续三轮噪声不挂断，乱码不进核身失败计数</li>
          <li>幂等按通话（session:turn 派生键），一通电话一张单</li>
          <li>规则质检 + 可选 LLM-as-Judge 离线评分（默认关）</li>
        </ul>
      </div>
      <div>
        <p style="color:var(--sub);font-weight:600">已知边界</p>
        <ul>
          <li>合成是分段合成，不是首包流式（首字受首段合成限制）</li>
          <li>语义级合规做了但默认关：多一次模型往返，超 2 秒预算</li>
          <li>说话人一致性门已在影子模式（只记录不拦截），缺真人双人语料标定</li>
          <li>音频不落盘（视赛题隐私要求）</li>
          <li>演示看板数字为只读种子库，非真实通话统计</li>
        </ul>
      </div>
    </div>
  </section>

  <section>
    <h3>复现</h3>
    <ul>
      <li><code>python app.py</code> 启动；<code>scripts/verify.py</code> 行为自检（275 项）</li>
      <li><code>scripts/run_eval.py</code> 文字驱动评测；加 <code>--judge=both</code> 开 LLM 评分</li>
      <li><code>scripts/auto_evolve.py</code> 一键自进化：体检 → 提案 → 闸门 → 反馈闭环</li>
      <li>看板 <code>/dashboard</code>；加 <code>?db=demo</code> 查看演示种子库</li>
    </ul>
    <div class="note">本页为项目展示页（静态）；完整可交互系统需启动 Python 后端，见仓库 README。</div>
  </section>
</div>
<footer>super_candy · 北京市「人工智能+」校外活动 · 源码见 GitHub</footer>
</body>
</html>
"""


def main():
    copied = copy_assets()
    figs = []
    for _, dst, caption in SHOTS:
        if dst in copied:
            figs.append('<figure><img src="assets/%s" alt="%s">'
                        '<figcaption>%s</figcaption></figure>' % (dst, caption, caption))
    html = HTML.replace("__FIGURES__", "\n".join(figs) or "<p>（截图资产缺失）</p>")
    os.makedirs(SITE, exist_ok=True)
    out = os.path.join(SITE, "index.html")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print("展示页已生成：%s（含 %d 张截图）" % (out, len(copied)))
    return out


if __name__ == "__main__":
    main()
