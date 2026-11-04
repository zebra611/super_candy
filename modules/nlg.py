"""M3 的措辞层 —— 把骨架话术改写成自然口语。

负责人：2A 佟欣。完整契约见 docs/17_任务2接口契约.md 第 3 节。

**边界（别越界）**
    这个模块只决定「同一件事怎么说」，不决定「说哪件事」。
    走哪个环节、能不能推进、该不该收尾，全是 dialogue.py 里状态机的事。
    一旦这里开始判断"要不要核验身份"，就会出现模型自由发挥、
    跳过环节、每次说法还不一样——而且不可复现、查不出来。

**为什么是「改写」而不是「自由生成」**
    状态机先用一句现成骨架话术承载意图（"为了核对身份，请说一下手机号后四位"），
    这里只把它说得自然些。好处：
        - 业务正确性不依赖模型：模型抽风最多话说得别扭，不会漏环节；
        - 合规护栏仍然有效：禁语检查在 app.py 对最终文本做，换措辞也跑得掉；
        - 降级零成本：出错直接返回骨架，行为退回今天的样子；
        - 可回归：关掉改写，verify 的 53 条断言必须全过——这是唯一标准。

**当前状态**
    只有接缝，没有实现。provider=template 时原样返回骨架话术，
    行为与接入前完全一致（verify 53/53、eval 30/30 是你开工前的基线）。

**佟欣要做的**
    1. 实现 _rewrite()：按 docs/17 第 3.3 节的 prompt 结构组织上下文；
    2. 把 provider 改成 dashscope 后，自己跑一遍 eval + bench_voice，
       确认业务没退步（eval 不低于 28/30）、时延没超预算（服务端 P50 <=1300ms）；
    3. 把 docs/17 第 3.5 节的验收表逐项过一遍。

**一个必须遵守的数**
    LLM 这一步的时间预算是 **400ms 硬上限**，不是参考值。
    现在服务端 P50 是 974ms，加 400ms 还有余量；加 800ms 就贴门槛了。
    超时就返回骨架话术，不要等——宁可话说得朴素，也别让用户对着话筒干等。
"""

import config_loader


def say(session, base_text, stage=""):
    """把骨架话术改写成自然口语。

    参数：
        session    对话状态（**只读**，禁止修改；slots 里的业务事实唯一来源）
        base_text  骨架话术，来自 config/scene.json 对应环节
        stage      当前环节名，用于 prompt

    返回：str
        改写后的话术。**任何异常都必须返回 base_text 本身**，
        绝不抛异常、绝不返回空串——外呼里突然没声比话说得难听严重得多。
    """
    if not base_text:
        return base_text or ""

    cfg = config_loader.nlg_config()
    if (cfg.get("provider") or "template").strip() != "dashscope":
        return base_text                       # 默认路径：原样返回，行为不变

    rewritten = _rewrite(session, base_text, stage, cfg)
    return rewritten if rewritten else base_text


def _rewrite(session, base_text, stage, cfg):
    """真正调模型的地方。返回 None 表示"这次不改写"，上层会退回 base_text。

    返回 None 的六种情况（按 docs/17 第 3.4 节，别漏）：
        1. 没有 API Key
        2. 请求超时（> cfg["timeout_ms"]）
        3. 返回空串或只有标点
        4. 返回字数与骨架差异超过 2 倍（模型跑题长篇大论）
        5. 返回里出现了骨架没有的具体数字/金额/日期（编造业务事实）
        6. 其他任何异常

    实现提示：
        - endpoint 与 ASR/TTS 同一套鉴权，key 从 config_loader.secret() 取；
        - 用 POST + timeout=cfg["timeout_ms"]/1000，别用 urllib 的默认超时；
        - 不要在异常里 raise，直接 return None。

    TODO(佟欣)：按上面六条实现，然后跑 docs/17 第 3.5 节的验收表。
    注意：这个函数还没有任何实现，返回 None 是当前唯一正确的行为。
    """
    return None
