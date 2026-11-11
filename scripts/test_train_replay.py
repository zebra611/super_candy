#!/usr/bin/env python3
"""把 11-11 线上那段「全是问题」的对话固化成回归用例。

来源：用户截图的那通 A15（留出集·半信半疑的财务），13 轮里同时出现了
五类毛病，全都不是"话说得不好听"，而是：

    1. 客户只说"单号我真记不住、号码 4656 没存"，客服回"您报的**单号**我这边对得上"
       ——把客户刚否认的事当成确认，后面每一轮都建在错前提上；
    2. 客服连着两轮逐字相同地念"您可以简单回我一句清楚了或者还有疑问就行"；
    3. 客户说"材料页面根本打不开"，客服把材料清单再念一遍，连着两轮；
    4. 客户最后说"打错了，没这个人！"，客服**照样写了一张工单**并收线；
    5. 客户从头到尾只说"嗯"，客服也照样回写一张 consent=unknown 的工单。

这些都不是聚合指标能看出来的：闭环率、覆盖率、veto 次数当时全是绿的。
所以这个文件的每一条断言，都直接**读对话本身**。

跑法：python scripts/test_train_replay.py
"""

# 跑批必须可复现：模型客服每次说的话不一样，验收口径会漂，这里强制关掉。
import os as _os_agent
_os_agent.environ.setdefault("AGENT_FREE", "0")

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import events  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="sc_replay_")
events.DATA_DIR = _tmp
events.DB_PATH = os.path.join(_tmp, "replay.db")
events.init_db()

import config_loader  # noqa: E402
from modules import nlg  # noqa: E402

config_loader.tts_config = lambda: {"provider": "browser"}

from app import end_call, handle_turn, start_call  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok), detail))


def _replay(application_id, lines):
    """逐句走一遍，返回 (每轮 (客户, 客服), end_reason, 事件列表)。"""
    with nlg.template_only():
        started = start_call(application_id=application_id)
        sid = started["session_id"]
        pairs, end_reason = [], ""
        for u in lines:
            res = handle_turn(sid, user_text=u)
            pairs.append((u, res.get("reply_text") or "", res.get("end_reason") or ""))
            if res.get("end_reason"):
                end_reason = res["end_reason"]
                break
        evs = events.list_events(sid)
        end_call(sid)
    return started, pairs, end_reason, evs


#: 截图里用户的原话，一字未改（尾号是那单的真实尾号）。
def _lines(tail):
    return [
        "单号？我只记得尾号，别的记不清了。",
        "啥？备案凭证和单号不一致？我这材料明明都对得上啊！",
        "补啥材料？上哪儿补去？",
        "后台审核我看材料页面都打不开，店里还一堆事等着呢",
        "单号？我真记不住，号码%s的手机上也没存！" % tail,
        "单号？真记不住了，就记得尾号%s" % tail,
        "都当我一时真想不起来",
        "补这些材料，平台收不收费啊？",
        "记住，就记得尾号%s" % tail,
        "都当我一时真想不起来",
        "后台审核我看材料页面都打不开",
        "打错了，没这个人！",
    ]


def main():
    started, pairs, end_reason, evs = _replay("QZ202611060002", _lines("1111"))
    company = (started.get("company") or {})
    check("复现用的档案取到了（客户报的尾号与客服手上是同一张单）",
          bool(company.get("company_name")), str(company.get("company_name")))

    # 1) 客户从没报过单号，就不许说"您报的单号我这边对得上"
    bad_claim = [(u, a) for u, a, _ in pairs if "您报的单号" in a or "单号没错" in a]
    check("客户只说记不住单号时，客服不许说「您报的单号对得上」",
          not bad_claim,
          "踩了吗：%s" % (bad_claim[:1] or "否"))

    # 2) 相邻两轮不许逐字相同
    dup = [(pairs[i - 1][1], pairs[i][1]) for i in range(1, len(pairs))
           if pairs[i][1] and pairs[i][1] == pairs[i - 1][1]]
    check("相邻两轮客服回复不许逐字重复", not dup,
          "重复：%s" % (dup[:1] or "无"))

    # 3) 客户说后台打不开时，回复得接住"打不开"，不能只念材料清单
    blocked = [(u, a) for u, a, _ in pairs if "打不开" in u]
    ok_blocked = blocked and all(
        any(w in a for w in ("打不开", "浏览器", "专员", "页面")) for _, a in blocked)
    check("客户说「页面打不开」时，客服要接住这堵墙（不能只念材料清单）",
          ok_blocked, "共 %d 轮；首句：%s" % (len(blocked), (blocked[0][1][:40] if blocked else "")))

    # 4) 客户说打错了：必须停，且一张工单都不许写
    wb = [e for e in evs if e.get("type") in ("writeback", "ticket")]
    check("客户说「打错了，没这个人」时立即收线（not_me）",
          end_reason == "not_me", "end_reason=%s" % end_reason)
    check("打错人的电话不许回写工单", not wb, "回写事件 %d 条" % len(wb))

    # 5) 只"嗯"两次不算表态：不许替他写结论
    # 先走到确认环节（"嗯"在 NEED 会被当成含糊，走的是另一条澄清路，
    # 那一条本来就不回写；这里要测的是确认环节的含糊出口）。
    with nlg.template_only():
        st = start_call(application_id="QZ202611060002")
        sid2 = st["session_id"]
        for u in ("对，是我", "后四位1111", "补什么材料",
                  # 「那我接下来怎么弄」以前靠"问一句→答一句"推进 CONFIRM，
                  # 现在这句会被求助分支接住（给分步操作、阶段不动）——
                  # 这是本轮改对的行为。进确认阶段改用明确的确认词；
                  # NEED 里第一次"清楚了"只是继续解释并推进 OFFER，
                  # OFFER 里第二次才落进 CONFIRM，所以连说两次。
                  "好的，清楚了", "好的，清楚了"):
            r2 = handle_turn(sid2, user_text=u)
            if (r2.get("next_stage") or "") == "CONFIRM":
                break
        r2 = handle_turn(sid2, user_text="嗯")
        r3 = handle_turn(sid2, user_text="嗯")
        stage2 = r3.get("next_stage") or ""
        end2 = r3.get("end_reason") or ""
        r4 = handle_turn(sid2, user_text="嗯")     # 连续第三次才交给人
        end3 = r4.get("end_reason") or ""
        evs2 = events.list_events(sid2)
        end_call(sid2)
    wb2 = [e for e in evs2 if e.get("type") in ("writeback", "ticket")]
    check("客户两次只说「嗯」：不回写、不收线，留在确认阶段等表态",
          not wb2 and not end2 and stage2 == "CONFIRM",
          "end_reason=%r；next_stage=%r；回写 %d 条" % (end2, stage2, len(wb2)))
    check("连续第三次仍含糊才转专员，且全程一张工单都不写",
          end3 == "unresolved_consent" and not wb2,
          "end_reason=%r；回写 %d 条" % (end3, len(wb2)))

    # 6) 客户同一动作不许连着来三次（读模拟器本身，不看客服）
    from modules.adversary import AdversarySim
    persona = {"id": "A15", "label": "测试", "goal": "offtopic", "max_level": 4,
               "traits": {"patience": 20},
               "knows": {"phone_tail": True, "contact_name": True,
                         "application_id": True, "company_name": True}}
    sim = AdversarySim(persona, {"company_name": "上海市白鹿商贸股份有限公司",
                                 "contact_name": "虞扶苏", "phone_tail": "1111",
                                 "application_id": "QZ202611060002"}, seed="A15")
    acts = []
    for _ in range(8):
        out = sim.say("您可以简单回我一句清楚了或者还有疑问就行。")
        if out is None:
            break
        acts.append(out["action"])
    run = best = 1
    for i in range(1, len(acts)):
        run = run + 1 if acts[i] == acts[i - 1] else 1
        best = max(best, run)
    check("客户同一动作最多连两次（原实录连说 5 轮「记不住」）",
          best <= 2, "最长连续 %d：%s" % (best, acts))

    # 7) 核身阶段：客户连着不报信息、最后质问「你哪个部门的」，
    #    不许甩给「信号不太好」（L2 基线 S05 的实录）。
    #    变异靶子：把 _contentful 判据删掉，本条必须红。
    # 用 L2 基线 S05 的**六句原话**（eval/reports/sim_baseline_v*.json 里
    # 逐轮存着）。前两版都造错了场景：连着两句明确拒绝会被 identity_refusals
    # 提前收尾；「嗯/啊」这类短句走的是「没听懂」通道，根本不累计 noise_turns。
    # 只有这六句会真的顶到 NOISE_MAX 临界点，才能测到那个 no_voice 兜底。
    _, pairs7, end7, _ = _replay("QZ202611060002", [
        "我说，是我，你说",
        "我说，这种电话我接多了，号码我不会报",
        "我说，这种电话我接多了，号码我不会报",
        "我说，那号在我儿子手机上，我记不住",
        "我说，你先别问号，你说说这电话到底什么事",
        "我说，我凭什么告诉你，你先说你是哪个部门的",
    ])
    last7 = pairs7[-1][1] if pairs7 else ""
    check("S05：客户质问来意时不许甩给「信号不太好」并当噪声挂断",
          last7 and "信号不太好" not in last7 and end7 != "no_voice",
          "end_reason=%r；共 %d 轮；末句=%r" % (end7, len(pairs7), last7[:56]))

    # 8) 客户复读 ≠ 含糊表态：短句真含糊仍要能触发转专员
    from modules import dialogue
    _eg = dialogue.new_session("verify-repeat")
    _eg["prev_user_text"] = "少拿这些破权益堵我嘴"
    check("客户把同一句抱怨又说一遍，判成「没接住」而不是「含糊」",
          dialogue._self_repeated(_eg, "少拿这些破权益堵我嘴")
          and not dialogue._self_repeated(_eg, "嗯")
          and not dialogue._self_repeated(_eg, "再说吧"),
          "长句=%s；嗯=%s；再说吧=%s"
          % (dialogue._self_repeated(_eg, "少拿这些破权益堵我嘴"),
             dialogue._self_repeated(_eg, "嗯"),
             dialogue._self_repeated(_eg, "再说吧")))
    _eg["prev_user_text"] = "少拿这些破权益堵我嘴"
    _rr = [dialogue._self_repeat_reply(_eg, {}, {"yes": False, "no": False,
                                                 "ask": False, "angry": False,
                                                 "complain": False, "deny": False,
                                                 "hangup": False, "handoff": False})
           for _ in range(4)]
    _rr_text = [r.get("reply_text") or "" for r in _rr]
    check("客户连复读四轮，客服四轮不能逐字重复（轮换承接）",
          all(a != b for a, b in zip(_rr_text, _rr_text[1:])),
          "首两句：%r / %r" % (_rr_text[0][:36], _rr_text[1][:36]))

    # 9) 「记住了没？」是挑衅，不是"我清楚了"（线上 A15 第 7 轮）。
    #    pop 场景 confirm_yes_words 里有「记住了」，后置否定没查就回写了工单。
    _, pairs9, _, evs9 = _replay("QZ202611060019", [
        "对，就是广东省降真百货经营部，我叫澹台溪亭。",
        "哦，预留手机号后四位是9403。",
        "等等，具体要补哪几份材料？在后台哪个入口提交？",
        "哎？你这号是哪来的？谁让你打的？",
        "申请单号QZ202611060019，记不住就写下来！",
        "QZ202611060019，记不住就抄十遍！",
        "澹台溪亭，记住了没？",
    ])
    wb9 = [e for e in evs9 if e.get("type") in ("writeback", "ticket")]
    check("客户反问「记住了没？」不许当成确认并回写工单",
          not wb9, "回写事件 %d 条；末句=%r"
          % (len(wb9), (pairs9[-1][1] if pairs9 else "")[:50]))

    # 11) 「刷不出来」也是同一堵墙：词表只认「打不开」时，这句一个字都没命中，
    #     客服继续问「清楚了吗」（11-11 线上 A15 第 11 轮实数）。
    _base11 = [
        "对，就是广东省降真百货经营部，我叫澹台溪亭。",
        "哦，预留手机号后四位是9403。",
        "等等，具体要补哪几份材料？在后台哪个入口上传？",
        "哎？你这号哪来的？谁让你打我电话的？",
        "是我。",
    ]
    _, pairs11, _, _ = _replay("QZ202611060019", _base11 + [
        "后台卡得我连补传页面都刷不出来，店里还一堆单等着发货呢！"])
    last11 = pairs11[-1][1] if pairs11 else ""
    check("客户说「刷不出来」时也要接住这堵墙（词表不能只认「打不开」）",
          last11 and any(w in last11 for w in ("浏览器", "专员", "页面", "后台")),
          "末句：%r" % last11[:64])

    # 12) 「你到底是谁」不含「你谁」这个连续串，旧词表整句没命中，落到兜底
    #     回「我这边确认不到」——同一通电话第 4/6 轮明明答过两次。
    _, pairs12, _, _ = _replay("QZ202611060019", _base11 + [
        "你这号码哪来的？谁给你的？",
        "授权书、链路证明、营业执照——这三样具体要补哪几份？后台哪个入口传？",
        "你到底是谁？这号真是平台给的？"])
    last12 = pairs12[-1][1] if pairs12 else ""
    check("客户问「你到底是谁」要正面答来源，不许说「确认不到」",
          last12 and "确认不到" not in last12
          and any(w in last12 for w in ("官方", "预留", "号码", "审核")),
          "末句：%r" % last12[:64])

    # 13) 客户重申尾号时只回「清楚了」= 没接住：必须复述那个值。
    _, pairs13, _, _ = _replay("QZ202611060019", _base11 + ["尾号9403，没错。"])
    last13 = pairs13[-1][1] if pairs13 else ""
    check("客户重申尾号时，客服要复述那个尾号（不是只说「清楚了」）",
          "9403" in last13, "末句：%r" % last13[:64])

    # 14) 客户第三次报信息后仍追问表态，就是逼表态了。
    _, pairs14, _, _ = _replay("QZ202611060019", _base11 + [
        "尾号9403，没错。", "是我。", "QZ202611060019，这单号我早背熟了！"])
    last14 = pairs14[-1][1] if pairs14 else ""
    # 判据必须是「不许再用问句」，不是「不许说这几个词」：
    # 第一版只挡了「清楚了吗/还有疑问/简单回我一句/哪点不清楚」，
    # 把第 3 次改回追问（换成「那这几样材料您清楚怎么准备吗？」）照样全绿——
    # 换个说法继续逼表态，和原句没有区别。问句本身就是判据。
    check("客户第三次报信息后，不许再用问句逼表态",
          last14 and not re.search(r"[？?]", last14) and "吗" not in last14,
          "末句：%r" % last14[:64])

    # 15) 第二次说后台打不开：系统解决不了，就该如实收线。
    #     旧话术说「我这条记下『后台打不开』」，实际一张记录都没写——
    #     嘴上比手上走得快，跟「没得到答案却说知道了」是同一类毛病。
    _, _, end15, evs15 = _replay("QZ202611060019", _base11 + [
        "后台卡得我连补传页面都刷不出来，店里还一堆单等着发货呢！",
        "清楚啥？我连补传入口都打不开，这算哪门子清楚！"])
    wb15 = [e for e in evs15 if e.get("type") in ("writeback", "ticket")]
    check("后台打不开两次：如实收线转专员（end_reason=blocked_ui），一张工单都不写",
          end15 == "blocked_ui" and not wb15,
          "end_reason=%r；回写 %d 条" % (end15, len(wb15)))

    # 16) 改写层不许动骨架里的数字：尾号 9403 改掉就是另一件事。
    from modules.nlg import _entities_ok
    check("改写丢了骨架里的尾号必须退回骨架（数字是业务事实）",
          not _entities_ok("尾号 9403，对上了。那这几样材料您清楚怎么准备吗？",
                           "清楚了，还有其他疑问吗？", {})
          and _entities_ok("尾号 9403，对上了。", "尾号9403，对上了，您看呢？", {}),
          "丢了 9403 却没被拦住")

    # 17) 客户说「我刚才不是说过了吗」是在指客服重复，不是在问新问题。
    #     旧逻辑落到 engaged="talk" 兜底，回「我这边确认不到」（线上 A15 第 9 轮）。
    _, pairs17, _, _ = _replay("QZ202611060019", _base11 + [
        "尾号9403，没错。", "我刚才不是说过了吗？",
        "QZ202611060019，这号我早报过三遍了！"])
    last17 = pairs17[-1][1] if pairs17 else ""
    check("客户说「我刚才不是说过了吗」要认账，不许回「确认不到」",
          last17 and "确认不到" not in last17
          and any(w in last17 for w in ("没接住", "问重", "不重复", "记下")),
          "末句：%r" % last17[:64])
    # 带具体遍数的也算：「这号我早报过三遍了」是最明确的一种"你重复问"。
    # 线上第 14 轮没认出来，又回了一句「材料这块，您还有哪点不清楚」。
    check("「我早报过三遍了」也算元话语，不许再追问、不许再要单号",
          last17 and "还有哪点不清楚" not in last17
          and "对照申请单编号核实" not in last17,
          "末句：%r" % last17[:64])

    # 18) 「是我公司」和「是我本人」不是一回事，别一律答"是您本人"。
    _, pairs18, _, _ = _replay("QZ202611060019", _base11 + ["对，是我公司。"])
    last18 = pairs18[-1][1] if pairs18 else ""
    check("客户说「是我公司」要接成「是您公司」，不许答「是您本人」",
          "公司" in last18 and "本人" not in last18,
          "末句：%r" % last18[:64])

    # 19) 改写层不许把承接句压掉（线上第 6 轮「对，是您本人」被压成纯追问）。
    #     这里只测判据本身；**接线**（say 有没有真的调用它）由
    #     scripts/test_nlg.py::test_open_kept_or_fallback 守——
    #     变异 m14 证明：只测纯函数，把 say 里的调用改成 False 也不会红。
    from modules.nlg import _open_kept
    check("改写把承接句吃掉时必须退回骨架",
          not _open_kept("您这边清楚这几样材料怎么准备吗？", "对，是您本人。")
          and _open_kept("对，是您本人，那这几样材料您清楚吗？", "对，是您本人。")
          and _open_kept("随便什么", ""),
          "丢了承接却没被拦")

    # 20) 半句保护：截断只许断在句末，不许停在「、，；」
    from modules.nlg import _fit
    sample = "您好，关于授权书公章与品牌名不一致的情况，需重新提交品牌授权书、逐级授权链路证明。"
    got = _fit(sample, 30)
    check("话术截断不许留下半句（句尾挂着「、」）",
          got is None or not re.search(r"[、，；]$", got or ""), "截成：%r" % (got,))

    # 21) 串单：客户报的尾号是**别人家**的（截图那通就这么串的）。
    #     旧代码拿 9673 去全库查，命中青海梧丘，把 slots.application 换成那家，
    #     然后念"梧冈品牌、运动户外类目、需补境外商标注册证"——电话打给降真，
    #     说的是梧丘的事。这不是听不懂，是**把 A 商家的事念给 B 听**。
    _, pairs21, _, _ = _replay("QZ202611060019", ["对，是我。", "9673"])
    j21 = " ".join(x[1] for x in pairs21)
    check("客户报别家尾号：不核过，也不许把别人家的驳回原因念出来",
          "梧冈" not in j21 and "运动户外" not in j21 and "境外商标" not in j21
          and "核对上了" not in j21,
          "回复：%r" % j21[:90])

    # 22) 嘴漏：核身没过就把档案里的尾号念出来，等于自己把门打开。
    #     线上模型连着说「您预留的手机号后四位是9403，对上了」。
    from modules import agent_free, dialogue as _dlg, persona as _per
    _s22 = {"slots": {}, "outbound_target":
            {"phone_tail": "9403", "application_id": "QZ202611060019"}}
    _base22 = "麻烦报一下您预留手机号的后四位。"
    # 测的是**接线**：直接调 _gates，看这道闸有没有真的接在出话路径上。
    # 只测 _leaks_secret 这个纯函数是不够的——把 _gates 里那句调用删掉，
    # 纯函数照样返回 True（这一轮已经吃过三次"测了判据没测接线"的教训）。
    _leak22 = agent_free._gates(_s22, "您预留的手机号后四位是9403，对上了。",
                               _base22, "IDENTITY", [], "", "")
    _ok22 = agent_free._gates(_s22, "麻烦报一下您预留手机号的后四位，四个数字就行。",
                             _base22, "IDENTITY", [], "", "")
    check("核身未通过时，不许念出档案里的尾号/单号（嘴漏闸接在出话路径上）",
          _leak22 is None and _ok22, "漏句=%r；正常句=%r" % (_leak22, _ok22))

    # 23) 骨架复读：同一个状态连着两轮会查同一句话，必须换说法。
    _s23 = _dlg.new_session("v23")
    _same = "您可以简单回我一句清楚了或者还有疑问就行。"
    with nlg.template_only():
        _o23a = _dlg._reply(_s23, _same, next_stage="CONFIRM")
        _o23b = _dlg._reply(_s23, _same, next_stage="CONFIRM")
    check("骨架要和上一句一模一样时必须换说法（不许原样播两遍）",
          _o23a["reply_text"] != _o23b["reply_text"]
          and _same in _o23b["reply_text"],
          "两次：%r / %r" % (_o23a["reply_text"][:26], _o23b["reply_text"][:26]))

    # 24) 「怎么补」是本环节的基本问题，答案就在档案里，不许落到「确认不到」。
    # 必须走到 **OFFER** 再问：NEED 那句本身就会念材料，测不到这条分支
    # （第一版用例只说到第二句就问了，那时还在 NEED，删掉 OFFER 分支照样全绿——
    #  变异 m20 就是这么漏过去的）。
    _, pairs24, _, _ = _replay("QZ202611060019",
                               ["对，是我。", "9403", "嗯，然后呢", "怎么补？"])
    last24 = pairs24[-1][1] if pairs24 else ""
    check("OFFER 里问「怎么补」要答出本单材料和提交入口，不许说「确认不到」",
          "确认不到" not in last24
          and any(w in last24 for w in ("授权书", "链路", "营业执照"))
          and ("后台" in last24 or "重新提交" in last24),
          "末句：%r" % last24[:70])

    # 25) 尾巴守卫：核身问句/收线话术后面不许再挂「您回一个字也行」——
    #     刚让对方报四个数字，又说"回一个字也行"，自相矛盾。
    _g25 = _per._apply_actions(
        "好的，核对申请单需要您预留手机号的后四位，说四个数字就行。",
        {"suffix": "您回一个字也行。", "avoid_repeat": ["您回一个字也行。"]},
        repeated=True, repeat_n=0)
    check("核身问句/收线话术后面不许再挂「回一个字也行」",
          "回一个字" not in _g25, "实际：%r" % _g25[:60])

    print()
    bad = [c for c in _checks if not c[1]]
    for name, ok, detail in _checks:
        print("  [%s] %s%s" % ("通过" if ok else "失败", name,
                               ("　　" + str(detail)) if (detail and not ok) else ""))
    print()
    print("%d/%d 通过" % (len(_checks) - len(bad), len(_checks)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
