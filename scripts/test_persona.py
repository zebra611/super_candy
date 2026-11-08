"""性格识别 / 针对性回复 / 自进化闸门的负向测试（11-08）。

刻意把「假阳性」当主要敌人：
    - 识别错性格却改了事实 → 比不改更糟，必须被守卫整句退回；
    - 规则表里塞禁语 / 占位 / 改数字 → 静态校验必须报红；
    - 规则表空时接线却悄悄改了回复 → 恒等对照必须逐字一致。
识别一致率用**真跑一遍性格集**验证，不读缓存报告（读缓存会让测试假绿）。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from modules import persona
from scripts import evolve_persona as ep


def _session(**slots):
    return {"slots": slots or {}, "persona_hits": {}}


class TestClassify(unittest.TestCase):
    def test_keywords(self):
        cases = {
            "你快点说，别绕弯子": "impatient",
            "你哪来的我号码？": "skeptical",
            "信号不好，你再说一遍": "hard_of_hearing",
            "我记不住那几个数": "forgetful",
            "我跟你说啊，上次那个审核": "talkative",
            "没问题，我配合": "cooperative",
            "嗯，明白了": "quiet",
        }
        for text, want in cases.items():
            self.assertEqual(persona._classify(text), want, text)

    def test_digits_are_not_quiet(self):
        """报尾号不是「话少」——不能被判成 quiet。"""
        self.assertIsNone(persona._classify("4316"))
        self.assertIsNone(persona._classify("嗯，4316"))

    def test_priority_skeptical_over_impatient(self):
        self.assertEqual(persona._classify("快点说，你哪来的我号码"), "skeptical")

    def test_neutral_long_sentence(self):
        self.assertIsNone(persona._classify("我这边看一下，这笔申请是上个月交的"))

    def test_empty(self):
        self.assertIsNone(persona._classify(""))
        self.assertIsNone(persona._classify("   "))


class TestObserve(unittest.TestCase):
    def test_one_weak_hit_does_not_label(self):
        """弱信号（嗯/好）一次不定性：一句话就贴标签会把整通电话都改掉。"""
        s = _session()
        persona.observe(s, "嗯")
        self.assertEqual(s["persona_code"], "neutral")

    def test_one_strong_hit_labels(self):
        """强信号（凭什么/快点/再说一遍）一次就够：真人一句话就听得出。"""
        for text, want in (("你快点说", "impatient"),
                           ("你哪来的我号码", "skeptical"),
                           ("你再说一遍", "hard_of_hearing")):
            s = _session()
            persona.observe(s, text)
            self.assertEqual(s["persona_code"], want, text)

    def test_two_hits_label(self):
        s = _session()
        for _ in range(2):
            persona.observe(s, "你快点说")
        self.assertEqual(s["persona_code"], "impatient")
        self.assertGreater(s["persona_conf"], 0)

    def test_dominant_evidence_wins(self):
        s = _session()
        for _ in range(3):
            persona.observe(s, "你哪来的我号码")
        persona.observe(s, "嗯")
        self.assertEqual(s["persona_code"], "skeptical")


class TestGuard(unittest.TestCase):
    """守卫：适配不能改事实。任一不满足，整句退回原文。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"enabled": True, "rules": {
            "impatient": {"*": {"suffix": "您慢慢看。"}}}}, self.tmp)
        self.tmp.close()
        self.old = os.environ.get(persona.ENV_RULES)
        os.environ[persona.ENV_RULES] = self.tmp.name
        persona._CACHE.clear()

    def tearDown(self):
        if self.old is None:
            os.environ.pop(persona.ENV_RULES, None)
        else:
            os.environ[persona.ENV_RULES] = self.old
        persona._CACHE.clear()
        os.unlink(self.tmp.name)

    def _adapt(self, text, **slots):
        s = _session(**slots)
        s["persona_code"] = "impatient"
        s["persona_conf"] = 1.0
        return persona.adapt(s, text, "NEED")

    def test_identity_rules_do_change_form(self):
        out, applied = self._adapt("这笔申请没有通过审核。")
        self.assertTrue(applied)
        self.assertIn("您慢慢看", out)

    def test_question_does_not_get_statement_suffix(self):
        """含问号的句子不许接「我就不多说了」这类陈述式引导。

        必须覆盖「问号在中间、句尾是句号」这个真实形状——只测纯问句是假绿，
        线上实测漏掉的就是「那方便说一下您的姓名吗？我核对一下申请单。」
        """
        for text in ("那方便说一下您的姓名吗？",
                     "那方便说一下您的姓名吗？我核对一下申请单。",
                     "您清楚了吗？还有疑问也行。"):
            out, _ = self._adapt(text)
            self.assertEqual(out, text, text)
            self.assertNotIn("您慢慢看", out, text)

    def test_digits_cannot_disappear(self):
        out, _ = self._adapt("申请单编号 20261101001 请核对。")
        self.assertIn("20261101001", out)

    def test_company_name_cannot_disappear(self):
        app = {"company_name": "北京市柏庐商贸有限公司"}
        out, _ = self._adapt("北京市柏庐商贸有限公司的申请被驳回。", application=app)
        self.assertIn("北京市柏庐商贸有限公司", out)

    def test_placeholder_not_introduced(self):
        out, applied = self._adapt("请补充材料。")
        self.assertNotIn("【", out)
        self.assertNotIn("XX", out)

    def test_empty_rules_is_identity(self):
        """规则表为空 → 逐字不变。接线本身不许改变任何一句回复。"""
        with open(self.tmp.name, "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "rules": {}}, f)
        persona._CACHE.clear()
        text = "好的，核对申请单需要您预留手机号的后四位，说四个数字就行。"
        out, applied = self._adapt(text)
        self.assertFalse(applied)
        self.assertEqual(out, text)

    def test_freeze_only_allows_repeat_guide(self):
        """官方原话冻结：不准删客套、不准追加 suffix，只有重复引导能生效。"""
        with open(self.tmp.name, "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "rules": {"impatient": {"*": {
                "strip_lead": ["好的，"], "suffix": "您慢慢看。",
                "avoid_repeat": ["我再说一遍重点。"]}}}}, f)
        persona._CACHE.clear()
        s = _session()
        s.update({"persona_code": "impatient", "last_reply_text": ""})
        out, applied = persona.adapt(s, "好的，官方审核口径如下。", "NEED", freeze=True)
        self.assertFalse(applied)
        self.assertEqual(out, "好的，官方审核口径如下。")
        s["last_reply_text"] = "好的，官方审核口径如下。"
        out2, applied2 = persona.adapt(s, "好的，官方审核口径如下。", "NEED", freeze=True)
        self.assertTrue(applied2)
        self.assertTrue(out2.startswith("好的，官方审核口径如下。"),
                        "官方原句一个字都不能被删改")
        self.assertIn("我再说一遍重点", out2)


class TestGateStatic(unittest.TestCase):
    """静态校验的变异：坏候选必须全部报红。"""

    def _problems(self, rules):
        return ep.validate_candidate({"rules": rules}, {"rules": {}})

    def test_unknown_temper(self):
        self.assertTrue(self._problems({"easy": {"*": {"suffix": "好"}}}))

    def test_identifier_is_not_editable(self):
        """想通过候选改识别词表 —— 直接拒绝（未知动作）。"""
        self.assertTrue(self._problems({"skeptical": {"*": {"marks": ["随便"]}}}))

    def test_forbidden_word(self):
        self.assertTrue(self._problems({"skeptical": {"*": {"suffix": "保证通过"}}}))

    def test_placeholder(self):
        self.assertTrue(self._problems({"skeptical": {"*": {"suffix": "请补【】"}}}))

    def test_digits_in_replace(self):
        self.assertTrue(self._problems({"skeptical": {"*": {
            "replace": [["编号 1234", "编号 5678"]]}}}))

    def test_overlong(self):
        self.assertTrue(self._problems({"skeptical": {"*": {"suffix": "啊" * 61}}}))

    def test_good_candidate_passes(self):
        self.assertEqual(self._problems({"skeptical": {"*": {
            "avoid_repeat": ["您可以对照申请单编号核实。"]}}}), [])


class TestRecognitionLive(unittest.TestCase):
    """真跑一遍性格集：声明性格 vs 客服听到的性格。不读缓存报告。"""

    def test_majority_match(self):
        out = os.path.join(_ROOT, "eval", "reports", "_test_persona_live.json")
        cmd = [sys.executable, os.path.join(_ROOT, "scripts", "sim_eval.py"),
               "--personas", "eval/personas_temperament.json",
               "--json", out, "--quiet", "--tag", "test_persona"]
        r = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, (r.stdout or "")[-800:] + (r.stderr or "")[-800:])
        with open(out, encoding="utf-8") as f:
            d = json.load(f)
        os.unlink(out)
        declared = {p["id"]: p["temper"] for p in d["personas"]}
        hit = 0
        for s in d["sessions"]:
            codes = [t["agent"].get("persona", {}).get("code")
                     for t in s["turns"] if t["agent"].get("persona", {}).get("code")]
            if codes and codes[-1] == declared.get(s["id"]):
                hit += 1
        self.assertGreaterEqual(hit, 11, "12 个画像里至少 11 个要被听对：%d" % hit)
        self.assertTrue(any(t["agent"].get("persona", {}).get("applied")
                            for s in d["sessions"] for t in s["turns"]),
                        "默认规则必须真的改过回复，否则等于没接线")


if __name__ == "__main__":
    unittest.main(verbosity=2)
