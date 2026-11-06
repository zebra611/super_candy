# -*- coding: utf-8 -*-
"""意图识别基准：规则版 vs 模型版（T5）。

跑法：
    python3 scripts/intent_bench.py              # 有 Key 时两版都跑
    python3 scripts/intent_bench.py --no-model   # 只跑规则版（离线/CI）

**这个脚本想证明什么、不想证明什么**

想证明：换模型之后，24 条**口语化改写**样本的意图命中率**不低于**规则版。
不想证明：模型比规则聪明。24 条自拟样本 + 我们自己写的预期，本质是自证覆盖，
          不是泛化准确率。答辩口径必须写成
          "24 条人工标注口语样本上 A/B，非泛化准确率"。

**为什么样本要"口语化"**
    规则版是靠词表命中的。拿词表里的词造句，规则版 100% 命中，测了等于没测。
    所以这里刻意挑的是**真实会听到、但词表里没有原词**的说法：
    "成，帮我弄上"（没有"同意"）、"算了，不折腾了"（没有"不要"）、
    "我找你们管事的"（没有"领导"）。这些才是外呼里真正漏判的那一类。

**退出码**
    0  通过：模型版不低于规则版（或 --no-model 时只校验样本集自身完整）
    1  模型版低于规则版
    2  样本集本身有问题（条数不足、字段缺失、标签不在白名单）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import dialogue, intent  # noqa: E402

# 24 条口语样本：(原话, 期望主意图, 为什么这么标)
# 主意图取值与 dialogue._intent_name 一致。
SAMPLES = [
    ("行，那就这么办", "yes", "没有'同意'两个字，但这是最干脆的答应"),
    ("成，帮我弄上", "yes", "北方口语的答应，词表里一个都对不上"),
    ("可以，没问题", "yes", "词表命中，作为对照组"),
    ("这个我暂时不用，谢谢", "no", "'不用'带主语与语气词，旧词表能命中但易被'谢谢'干扰"),
    ("算了，不折腾了", "no", "泄气式拒绝，和'算了'同义但不是原词"),
    ("我这边不太想弄", "no", "没有否定词，语义是拒绝"),
    ("已经弄好了，不用了", "no", "拒绝 + 事实说明混合"),
    ("我不想再听到你们电话", "no", "拒绝 + 别再打，语义两层"),
    ("在哪里改啊", "ask", "操作路径类追问，赛题五大追问类型之一"),
    ("这个要花钱不", "ask", "句尾'不'是问句标志，不是否定"),
    ("补上来就能过吗", "ask", "结果预期类追问"),
    ("改完多久能审完", "ask", "门槛/时效类追问"),
    ("你们这是搞什么名堂", "angry", "质问式不满，没有骂人词"),
    ("你们这算什么服务态度", "angry", "情绪明确，但没有'投诉/骗子'这类词"),
    ("说了半天我也没听懂", "complain", "泄气，不是骂人"),
    ("问这有啥用", "complain", "质疑沟通价值，典型的放弃前兆"),
    ("那还要我说啥", "complain", "对抗模拟 D6 的原句"),
    ("嗯……让我想想", "unclear", "犹豫，不是同意"),
    ("都行吧你看着来", "unclear", "把决定推走"),
    ("我考虑考虑再说", "unclear", "软拒绝，词表里只有'考虑一下'"),
    ("啊，这样啊", "unclear", "纯语气词组合，没有信息"),
    ("尾号是八八六六", "info", "中文数字尾号，旧规则只认阿拉伯数字"),
    ("我号码后头四个是 8866", "info", "口语化报尾号"),
    ("能不能给我转个人", "handoff", "转人工的口语说法，词表里是'转人工'"),
    ("我找你们管事的", "handoff", "'管事的'不是'领导'，规则漏判"),
]

ALLOWED = {"yes", "no", "ask", "angry", "complain", "unclear", "info", "handoff", "unknown"}


def _rule_intent(text):
    return dialogue._intent_name(dialogue.classify(text))


def _model_intent(text, cfg):
    """把 provider 临时切到 dashscope 跑一次，跑完还原，不影响全局配置。"""
    original = intent._config

    def _fake():
        return cfg

    intent._config = _fake
    try:
        return dialogue._intent_name(intent.classify(text))
    finally:
        intent._config = original


def validate():
    """样本集自身的校验。这个脚本的输出要贴进文档，样本集坏了比跑错更糟。"""
    problems = []
    if len(SAMPLES) < 20:
        problems.append("样本只有 %d 条，T5 要求至少 20 条" % len(SAMPLES))
    seen = set()
    for text, want, _ in SAMPLES:
        if text in seen:
            problems.append("重复样本：%r" % text)
        seen.add(text)
        if want not in ALLOWED:
            problems.append("「%s」的期望标签 %r 不在白名单" % (text, want))
    return problems


def main(argv):
    cmd = {"no_model": "--no-model" in argv, "quiet": "--quiet" in argv}
    problems = validate()
    if problems:
        print("样本集有问题：")
        for p in problems:
            print("  - " + p)
        return 2

    rows = []
    for text, want, why in SAMPLES:
        rows.append({"text": text, "want": want, "why": why,
                     "rule": _rule_intent(text), "model": ""})

    rule_ok = sum(1 for r in rows if r["rule"] == r["want"])
    rule_rate = rule_ok / float(len(rows))

    cfg = {"provider": "dashscope",
           "model": (intent._config().get("model") or intent._DEFAULT_MODEL),
           "timeout_ms": 4000}          # benchmark 放宽超时，测的是判断力不是速度

    model_available = False
    if not cmd["no_model"]:
        try:
            import config_loader
            model_available = bool(config_loader.secret("dashscope_api_key"))
        except Exception:
            model_available = False

    if model_available:
        for r in rows:
            r["model"] = _model_intent(r["text"], cfg)
        model_ok = sum(1 for r in rows if r["model"] == r["want"])
    else:
        model_ok = 0

    if not cmd["quiet"]:
        print("=" * 78)
        print("意图识别基准：%d 条口语化样本" % len(rows))
        print("=" * 78)
        for r in rows:
            mark = "✓" if r["rule"] == r["want"] else "✗"
            mtxt = ("  模型 %-9s %s" % (r["model"], "✓" if r["model"] == r["want"] else "✗")
                    if model_available else "")
            print("  %s 期望 %-9s 规则 %-9s%-22s %r"
                  % (mark, r["want"], r["rule"], mtxt, r["text"]))
        print()
        print("规则版：%d/%d = %.0f%%" % (rule_ok, len(rows), rule_rate * 100))
        if model_available:
            print("模型版：%d/%d = %.0f%%"
                  % (model_ok, len(rows), model_ok / float(len(rows)) * 100))
        else:
            print("模型版：未启用（无 dashscope_api_key 或指定了 --no-model）")
        print()
        print("口径提醒：样本是自拟的，规则版的高低测的是词表覆盖度，")
        print("          不是真实用户分布上的准确率。答辩必须这么写。")

    if not model_available:
        return 0
    return 0 if model_ok >= rule_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
