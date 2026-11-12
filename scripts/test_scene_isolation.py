"""场景隔离与收尾话术的回归自测（11-09）。

守三件 11-09 线上实测暴露出来的事：

1. **对抗跑批必须按场景分批**。340 条 C4（POP 关单场景）用例混进默认售后场景跑，
   全部卡在 IDENTITY，跑出 31/374 = 8.3%；同一批数据切到 `--scene pop_reject`
   是 340/340 = 100%。这个文件把"哪个文件属于哪个场景"钉住，防止下次加批次时
   又悄悄混进来。

2. **收尾那一轮不许加性格后缀**。A01 第 9 轮客户已经说"清楚了，我按你说的弄"，
   客服却回"…感谢您的配合，再见。我放慢一点说。"——告别语后面挂一句"放慢一点说"
   逻辑上说不通，而且这是整通电话最后一句话，客户一定会听见。
   判据必须在 `_reply` 里（next_stage=DONE 自己冻结），不能靠调用方传 freeze：
   评审时把判据删掉，原来的测试 11/11 照过，是假绿。

3. **"不是说了吗"不是修复请求**（初版曾收进来，评审按实测撤回）。
   它是抱怨"已经问过了"。判成修复请求后，16 通对抗画像从 16/16 掉到 14/16：
   复述那一轮不推进确认，A09/A14 最后以 unknown 回写收线（误闭环）。
   同时钉住"明确拒绝优先于修复请求"：客户说"我没听清，不过不要了"必须回写拒绝，
   不能被复述分支拦走。

用法：python3 scripts/test_scene_isolation.py
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import adversarial_run as A            # noqa: E402
from modules import dialogue as D      # noqa: E402
from modules import persona            # noqa: E402
from modules import policy             # noqa: E402
from modules import tools              # noqa: E402


class SceneOfFileTest(unittest.TestCase):
    """C4 批次必须归到 pop_reject，其他批次留在默认场景。"""

    def test_c4_batches_are_pop_reject(self):
        for i in (1, 5, 9):
            name = "adversarial_c4_batch%02d.json" % i
            self.assertEqual(A.scene_of_file(name), "pop_reject", name)

    def test_other_batches_are_default(self):
        for name in ("adversarial_cases.json", "adversarial_1106.json",
                     "adversarial_q1_regression.json", "adversarial_t17_realcall.json"):
            self.assertEqual(A.scene_of_file(name), "", name)

    def test_unknown_file_falls_back_to_default(self):
        self.assertEqual(A.scene_of_file("adversarial_newbatch.json"), "")

    def test_prescan_reads_both_spellings(self):
        self.assertEqual(A._prescan_scene(["x", "--scene", "pop_reject"]), "pop_reject")
        self.assertEqual(A._prescan_scene(["x", "--scene=pop_reject"]), "pop_reject")
        self.assertEqual(A._prescan_scene(["x"]), "")


class CaseSplitTest(unittest.TestCase):
    """默认合并时不能把 C4 带进来，反之亦然。"""

    def _files(self):
        import glob
        return sorted(glob.glob(os.path.join(A.CASES_DIR, "adversarial*.json")))

    def test_default_excludes_c4(self):
        picked = [os.path.basename(p) for p in self._files()
                  if A.scene_of_file(p) == ""]
        self.assertTrue(picked, "默认场景应当有对抗集")
        self.assertFalse([n for n in picked if n.startswith("adversarial_c4_")],
                         "默认场景不该包含 C4 批次")

    def test_pop_reject_only_c4(self):
        picked = [os.path.basename(p) for p in self._files()
                  if A.scene_of_file(p) == "pop_reject"]
        self.assertTrue(picked, "pop_reject 场景应当有对抗集")
        self.assertTrue(all(n.startswith("adversarial_c4_") for n in picked),
                        "pop_reject 场景只该有 C4 批次")


class ClosingReplyNoSuffixTest(unittest.TestCase):
    """收尾话术（next_stage=DONE）不加任何补救型后缀。"""

    def test_persona_adapt_freezes_closing(self):
        s = D.new_session("t-close")
        s["persona_code"] = "hard_of_hearing"
        bye = "感谢您的配合，再见。"
        out, applied = persona.adapt(s, bye, "WRITEBACK", freeze=True)
        self.assertFalse(applied, "冻结时不该做任何适配")
        self.assertEqual(out, bye)
        self.assertNotIn("放慢", out)

    def test_done_reply_is_frozen_at_reply_level(self):
        """真正守 `_reply` 的判据：next_stage=DONE 时必须自己冻结，而不是靠调用方传 freeze。

        这条替代了原来的 `D.respond(...)+if 以再见结尾` 写法——那样写在
        `_reply` 的 DONE 判据被删掉后依然全绿（实测：去掉 `next_stage == "DONE"`
        后 11/11 照过），是假绿。这里同时断言对照组：同一句在 WRITEBACK
        （非 DONE）时性格确实会加后缀，否则性格规则失效也会让断言假过。
        """
        bye = "感谢您的配合，再见。"
        s = D.new_session("t-done")
        s["persona_code"] = "hard_of_hearing"
        done = D._reply(s, bye, next_stage="DONE", intent="consent")
        text = done.get("reply_text") or ""
        # 11-12 用户要求：所有收线句在"再见"之前必须给出热线。
        # 所以这里不再逐字等于原句，改守三件事：不挂补救语、热线在再见之前、仍以再见收尾。
        self.assertNotIn("放慢", text, "DONE 是整通电话最后一句，不许挂补救语")
        self.assertIn(D._HOTLINE, text, "收线前必须给热线")
        self.assertLess(text.index(D._HOTLINE), text.index("再见"), "热线必须在再见之前")
        self.assertTrue(text.endswith("再见。"), "整通最后一句仍应是再见")

        s2 = D.new_session("t-wb")
        s2["persona_code"] = "hard_of_hearing"
        wb = D._reply(s2, bye, next_stage="WRITEBACK", intent="consent")
        self.assertIn("放慢一点说", wb.get("reply_text") or "",
                      "对照组必须真的加后缀，否则这条断言测不到东西")


class RepairMarkTest(unittest.TestCase):
    """修复请求的边界：只收带重说动作的说法；抱怨重复与拒绝都不算。"""

    def test_plead_repeat_is_not_repair(self):
        """「不是说了吗」是抱怨已问过，不是请再说一遍。

        评审实测：收进 REPAIR_MARKS 后，16 通对抗画像从 16/16 掉到 14/16
        （A09/A14 被复述打断确认计数，最后以 unknown 回写收线）。
        它该走「接住抱怨再继续确认」，不该复用修复请求的复述话术。
        """
        for t in ("我刚不是说了吗，就那几个数",
                  "我刚才不是说了嘛",
                  "我不是说了么，你记一下"):
            self.assertFalse(policy.is_repair_request(t), t)

    def test_classic_repair_still_works(self):
        for t in ("信号不好，你再说一遍", "没听清", "再说一次"):
            self.assertTrue(policy.is_repair_request(t), t)

    def test_refusal_is_not_repair(self):
        # 这两句是拒绝，判成修复请求会让客服把方案再念一遍，而客户要的是挂断。
        for t in ("我说过了不要", "我不需要了", "别再问了"):
            self.assertFalse(policy.is_repair_request(t), t)

    def test_refusal_with_repair_word_still_writes_back(self):
        """「没听清，不要了」同时像修复请求与拒绝：拒绝优先，必须回写。

        评审实测的原始缺陷：OFFER 的修复分支在 `flags["no"]` 之前，
        会把"我不是说了吗，不要了"答成一段复述，整通电话不回写。
        """
        tools._reset_for_test()
        s = D.new_session("t-refuse")
        s["stage"] = "OFFER"
        s["slots"]["application"] = {"brand": "闻樾", "category": "家用电器",
                                     "reject_reason": "授权链路不完整",
                                     "materials": "品牌授权书"}
        s["slots"]["identity_confirmed"] = True
        r = D.respond(s, "我没听清，不过不要了")
        self.assertEqual(s["slots"].get("consent"), False,
                         "客户明确拒绝时必须回写拒绝，不能被复述分支拦走")
        self.assertEqual(r.get("next_stage"), "DONE")


def main():
    result = unittest.main(exit=False, verbosity=2).result
    print("=" * 60)
    if result.wasSuccessful():
        print("结果：%d/%d 通过" % (result.testsRun, result.testsRun))
        return 0
    print("结果：%d 条失败 / 共 %d 条" % (len(result.failures) + len(result.errors),
                                          result.testsRun))
    return 1


if __name__ == "__main__":
    sys.exit(main())
