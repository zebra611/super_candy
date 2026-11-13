# -*- coding: utf-8 -*-
"""会话语言切换的边界（11-13 用户要求）。

原话：「回答 ok、yes 的时候不要立马切英文，直到用户使用完整的英语句子或者
明确说我中文不好，才转换成英语」。

旧判据是"句子里有两个拉丁字母就切 en"，于是客户应一声 "ok"，客服下一句就
整段改口说英语——客户只是应了一声，并没有要换语言。这个文件把新的四条边界钉住：

    ① ok / yes / thanks / what materials 这类**短回答**：不切语言；
    ② 完整英文句子（≥4 词，或 3 词但带主语/助动词）：切 en；
    ③ 明确说"中文不好 / 说英语"（**可以是中文说的**）：切 en；
    ④ 明确说"听不懂英文 / 说中文"：切回 zh。

外加两条端到端：切过去之后 "ok" 不能把它切回来；英文会话里连说四句英文
不能被"信号不好"挂断。

跑法：python -m scripts.test_lang_switch
"""

import os
import sys
import unittest

os.environ.setdefault("APP_SCENE", "pop_reject")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app                                                   # noqa: E402
from modules import dialogue, i18n                           # noqa: E402

_EN_LETTER = "abcdefghijklmnopqrstuvwxyz"


def _has_english(text, min_run=12):
    """回复里有没有一整段英文（而不是零星的字母/品牌名）。"""
    run = best = 0
    for ch in (text or "").lower():
        run = run + 1 if ch in _EN_LETTER or ch == " " else 0
        best = max(best, run)
    return best >= min_run


class DetectMatrix(unittest.TestCase):
    """判据本身：哪些算"在说英语"，哪些只是"应了一声"。"""

    def test_short_answers_keep_chinese(self):
        for t in ("ok", "OK", "okay", "yes", "yeah", "sure", "thanks",
                  "ok thanks", "yes i am", "no", "what materials"):
            self.assertEqual(i18n.detect(t, "zh"), "zh",
                             "%r 不该把语言切走" % t)

    def test_short_answers_keep_english_too(self):
        """切到英文之后，一句 ok 也不能把它切回中文。"""
        for t in ("ok", "yes", "thanks"):
            self.assertEqual(i18n.detect(t, "en"), "en", t)

    def test_full_sentence_switches(self):
        for t in ("I want to know why my application was rejected",
                  "please tell me what documents I need to submit",
                  "what is this", "i need help"):
            self.assertEqual(i18n.detect(t, "zh"), "en", t)

    def test_chinese_bad_switches(self):
        """用中文说"我中文不好"也得切——这条必须在"句中有中文→zh"之前判。"""
        for t in ("我中文不好", "我中文不太好，你说英语吧", "我不会说中文",
                  "听不懂中文", "能说英文吗"):
            self.assertEqual(i18n.detect(t, "zh"), "en", t)

    def test_back_to_chinese(self):
        for t in ("i can't understand english", "can you speak chinese",
                  "please speak chinese", "no english"):
            self.assertEqual(i18n.detect(t, "en"), "zh", t)

    def test_codes_and_digits_do_not_switch(self):
        for t in ("QZ202611060001", "0086", "19900000086", "1234", "..."):
            self.assertEqual(i18n.detect(t, "zh"), "zh", t)

    def test_plain_chinese_sentence(self):
        self.assertEqual(i18n.detect("请问是安食官方旗舰店吗", "zh"), "zh")


class EndToEnd(unittest.TestCase):
    """真跑一通电话：语言变了，业务流程不能跟着乱。"""

    def setUp(self):
        from modules import tools
        tools._reset_for_test()
        started = app.start_call(lock_target=True)
        self.sid = started["session_id"]
        self.sess = app._SESSIONS[self.sid]

    def tearDown(self):
        app.end_call(self.sid)

    def _say(self, text):
        return dialogue.respond(self.sess, text)

    def test_ok_keeps_chinese_and_advances(self):
        r = self._say("ok")
        text = r.get("reply_text") or ""
        self.assertEqual(self.sess.get("lang"), "zh", "一句 ok 就切英文了")
        self.assertFalse(_has_english(text), "回复变成英文了：%s" % text)
        self.assertTrue(text.strip(), "ok 被当成没听懂，一句都没回")
        # 应了一声之后流程要继续（问尾号/姓名），不能原地反问"您说什么"
        self.assertNotIn("您刚才说什么", text)

    def test_full_sentence_switches_to_english(self):
        r = self._say("I want to know why my application was rejected")
        self.assertEqual(self.sess.get("lang"), "en")
        self.assertTrue(_has_english(r.get("reply_text") or ""),
                        "切了英文却还在说中文：%s" % r.get("reply_text"))

    def test_ok_does_not_switch_back(self):
        self._say("I want to know why my application was rejected")
        r = self._say("ok")
        self.assertEqual(self.sess.get("lang"), "en", "一句 ok 又切回中文了")
        self.assertTrue(_has_english(r.get("reply_text") or ""))

    def test_chinese_bad_switches_to_english(self):
        r = self._say("我中文不好，你能说英语吗")
        self.assertEqual(self.sess.get("lang"), "en")
        self.assertTrue(_has_english(r.get("reply_text") or ""))

    def test_english_turns_do_not_get_hung_up_as_noise(self):
        """英文会话里连说四句，不能被"信号不太好不好意思"挂断。

        classify 的噪声判据是"无中文且无数字"，只认中文语料；切到英文之后
        每一句都会命中它。不豁免的话，三句就把一个正常客户挂掉。
        """
        lines = ["I want to know why my application was rejected",
                 "what should I prepare",
                 "can you explain the materials again",
                 "I still do not understand the reason"]
        for line in lines:
            r = self._say(line)
            self.assertNotEqual(r.get("stage"), "DONE",
                                "第 %r 句被收线了：%s" % (line, r.get("reply_text")))
        self.assertEqual(self.sess.get("lang"), "en")


if __name__ == "__main__":
    unittest.main(verbosity=1)
