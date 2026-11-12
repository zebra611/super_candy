"""核身校验的负向测试（11-12）。

起因是用户看完线上说的一句：「申请单号后六位对不上与客服的验证，手机号报错
也有可能被客户识别成正常的」。实测确认了三件事，这个文件把它们钉住：

1. 报错尾号能被判「核对上了」，而且念的是**另一家**的驳回原因（串单）；
2. 真核不上时，客服只是换一个槽位再问（"那我换个方式核对…"），客户听不出
   自己刚报错了，以为核对已通过；
3. 按客服自己的提示报「申请单编号后六位」，系统直接丢掉那个数字，回一句
   "需要您预留手机号的后四位"——客户以为自己说错了。

三条都不是"话术不好听"，是核身这个安全关口在漏。

运行：
    python -m scripts.test_identity_verify
    python -m unittest scripts.test_identity_verify -v

约束与其他测试一致：纯标准库、不联网、不花钱。
"""

import os
import sys
import unittest

# 必须是赛题场景：这些行为只在 application_mode 打开时存在。
os.environ.setdefault("APP_SCENE", "pop_reject")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from modules import dialogue, tools                            # noqa: E402

#: 本单：北京市川上商贸（申请单 QZ202611060001，品牌闻樾，预留尾号 0086）。
TARGET_ID = "QZ202611060001"
#: 尾号 1234 属于另一家：天津静姝百货（QZ202611060003，品牌辛夷，美妆个护）。
OTHER_TAIL = "1234"
OTHER_BRAND = "辛夷"


def _rec():
    hit = tools.query_application(TARGET_ID) or {}
    return hit.get("application")


def _session(sid, locked=True, rec=None):
    rec = rec or _rec()
    s = dialogue.new_session(sid)
    s["outbound_target"] = rec
    s["company_confirmed"] = True
    s["target_locked"] = locked
    s["slots"]["application"] = rec
    return s


def _say(s, text):
    return dialogue.respond(s, text)


class TestWrongValueSpeaksUp(unittest.TestCase):
    """对不上必须说出来，而且说出来之后不能把单换掉。"""

    def test_wrong_tail_admits_mismatch(self):
        s = _session("iv-wrong-tail")
        r = _say(s, OTHER_TAIL)
        text = r.get("reply_text") or ""
        self.assertNotIn("核对上了", text, "报错尾号被判核上：%s" % text)
        self.assertIn("对不上", text, "没告诉客户哪个值对不上：%s" % text)
        self.assertIn("尾号", text)
        self.assertEqual(r.get("intent"), "identity_retry")
        # 串单检查：别家的品牌名一个字都不许出现
        self.assertNotIn(OTHER_BRAND, text, "把别家的驳回信息念出来了：%s" % text)
        self.assertEqual(
            (s["slots"].get("application") or {}).get("application_id"), TARGET_ID,
            "核身不通过却把本单换成了别家")

    def test_wrong_application_id_admits_mismatch(self):
        s = _session("iv-wrong-id")
        r = _say(s, "999999")
        text = r.get("reply_text") or ""
        self.assertIn("申请单编号", text, text)
        self.assertIn("对不上", text, text)
        self.assertNotIn("核对上了", text, text)
        self.assertEqual(
            (s["slots"].get("application") or {}).get("application_id"), TARGET_ID)

    def test_mismatch_is_said_each_time_but_not_repeated(self):
        """每次报错都要让他知道不对上；但同一句话不许逐字复读。

        只测"说了一次就不许再说"是错的：客户第二次又报了个错号，
        他还得知道自己又错了。要防的是复读，不是反馈。
        """
        s = _session("iv-once")
        first = _say(s, OTHER_TAIL).get("reply_text") or ""
        self.assertIn("对不上", first)
        second = _say(s, OTHER_TAIL).get("reply_text") or ""
        self.assertIn("对不上", second, "第二次报错反而不说了：%s" % second)
        self.assertNotEqual(first, second, "同一句提示逐字复读：%s" % second)


class TestAcceptedWays(unittest.TestCase):
    """客服自己提示过的报法，必须都能核上。"""

    def test_full_application_id(self):
        s = _session("iv-full-id")
        r = _say(s, TARGET_ID)
        self.assertEqual(r.get("intent"), "identity_ok", r.get("reply_text"))

    def test_application_id_tail_six(self):
        """报「申请单编号后六位」——reply_ask_tail_2 的原话就是这么说的。

        旧代码把它整个丢掉（classify 只认孤立 4 位数字），客户听到的还是
        "需要您预留手机号的后四位"，等于让他把刚说的话再说一遍。
        """
        s = _session("iv-six")
        r = _say(s, TARGET_ID[-6:])
        self.assertEqual(r.get("intent"), "identity_ok", r.get("reply_text"))

    def test_six_digits_inside_sentence(self):
        s = _session("iv-six-sentence")
        r = _say(s, "我的单号后六位是%s" % TARGET_ID[-6:])
        self.assertEqual(r.get("intent"), "identity_ok", r.get("reply_text"))

    def test_whole_phone_number(self):
        """客户把整个手机号念出来也算——核身只比后四位，别让他再说一遍。"""
        s = _session("iv-phone")
        r = _say(s, "19900000086")
        self.assertEqual(r.get("intent"), "identity_ok", r.get("reply_text"))


class TestLockIsTheOnlySwitch(unittest.TestCase):
    """锁定与否只由 start_call 的入参决定，不靠猜。

    真实外呼（HTTP 入口）一定知道打给谁 → 锁；脚本跑批（C4 那 340 条）
    走 start_call() 直调、不传 lock_target → 不锁，行为与改动前逐字一致。
    """

    def test_unlocked_keeps_find_by_number(self):
        s = _session("iv-unlocked", locked=False)
        r = _say(s, OTHER_TAIL)
        self.assertEqual(r.get("intent"), "identity_ok",
                         "不锁定这条路的跑批行为变了，C4 会跟着红")
        # 这条断言是"跑批兼容"的证据：报谁就是谁，档案跟着换
        self.assertEqual(
            (s["slots"].get("application") or {}).get("application_id"),
            "QZ202611060003")

    def test_start_call_lock_flag(self):
        import app
        need = app.start_call()                      # 脚本直调：不锁
        locked = app.start_call(lock_target=True)    # 真实外呼：锁
        try:
            n_sess = app._SESSIONS.get(need["session_id"]) or {}
            l_sess = app._SESSIONS.get(locked["session_id"]) or {}
            self.assertFalse(n_sess.get("target_locked"),
                             "脚本直调不锁，否则 C4/L2 跑批全红")
            self.assertTrue(l_sess.get("target_locked"))
            # 真实外呼一定有目标：列表里下一条待外呼的那张单
            self.assertTrue((l_sess.get("outbound_target") or {}).get("application_id"))
        finally:
            for sess in (need, locked):
                if sess.get("session_id"):
                    app.end_call(sess["session_id"])

    def test_http_handler_passes_lock_target(self):
        """HTTP 入口必须传 lock_target=True —— 这是"真实外呼知道打给谁"的落点。

        上面那条测的是 start_call 的入参行为，等于自证；HTTP 入口那一行漏了
        参数照样全绿，而线上正是走那个入口。所以这里扫源码：删掉参数就红。
        """
        with open(os.path.join(_ROOT, "app.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index('if path == "/api/call/start"')
        seg = src[i:i + 700]
        self.assertIn("lock_target=True", seg,
                      "HTTP 入口没锁目标：线上会退回「报谁的号就换成谁」，也就是串单")


if __name__ == "__main__":
    unittest.main(verbosity=2)
