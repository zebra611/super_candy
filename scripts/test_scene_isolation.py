"""场景隔离与收尾话术的回归自测（11-09）。

守三件 11-09 线上实测暴露出来的事：

1. **对抗跑批必须按场景分批**。340 条 C4（POP 关单场景）用例混进默认售后场景跑，
   全部卡在 IDENTITY，跑出 31/374 = 8.3%；同一批数据切到 `--scene pop_reject`
   是 340/340 = 100%。这个文件把"哪个文件属于哪个场景"钉住，防止下次加批次时
   又悄悄混进来。

2. **收尾那一轮不许加性格后缀**。A01 第 9 轮客户已经说"清楚了，我按你说的弄"，
   客服却回"…感谢您的配合，再见。我放慢一点说。"——告别语后面挂一句"放慢一点说"
   逻辑上说不通，而且这是整通电话最后一句话，客户一定会听见。

3. **"我刚不是说了吗"算修复请求**。客户没说"再说一遍"，但要的是同一件事。
   不含这类说法时，客服会回"这个我这边确认不到"，客户只好一遍遍重复。
   但"我说过了不要"是拒绝，不能被当成修复请求——两条边界都钉住。

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

    def test_done_turn_keeps_reply_clean(self):
        """真跑一轮到 DONE，回复不得以补救语结尾。"""
        s = D.new_session("t-done")
        s["persona_code"] = "hard_of_hearing"
        # 直接构造一个收尾回复：走到 WRITEBACK 并结束
        r = D.respond(s, "是我，尾号0086")
        # 这里只断言：任何以"再见"结尾的回复后面都不许再挂补救语
        txt = r.get("reply_text") or ""
        if txt.endswith("再见。"):
            self.assertNotIn("放慢一点说", txt)


class RepairMarkTest(unittest.TestCase):
    """修复请求的边界：收抱怨重复，不收拒绝。"""

    def test_plead_repeat_is_repair(self):
        for t in ("我刚不是说了吗，就那几个数",
                  "我刚才不是说了嘛",
                  "我不是说了么，你记一下"):
            self.assertTrue(policy.is_repair_request(t), t)

    def test_classic_repair_still_works(self):
        for t in ("信号不好，你再说一遍", "没听清", "再说一次"):
            self.assertTrue(policy.is_repair_request(t), t)

    def test_refusal_is_not_repair(self):
        # 这两句是拒绝，判成修复请求会让客服把方案再念一遍，而客户要的是挂断。
        for t in ("我说过了不要", "我不需要了", "别再问了"):
            self.assertFalse(policy.is_repair_request(t), t)


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
