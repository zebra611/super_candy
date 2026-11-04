---
name: 缺陷报告
about: 报告一个可复现的问题（请务必附事件链）
title: "[缺陷] "
labels: bug
---

## 现象

<!-- 一句话：什么不对。不要写"系统有问题"。 -->

## 复现步骤

1.
2.
3.

## 期望 vs 实际

| | 内容 |
|---|---|
| 期望 | |
| 实际 | |

## 事件链

<!--
最关键的一段。打开 http://127.0.0.1:8000 右侧"事件链"面板，
或跑 curl "http://127.0.0.1:8000/api/events?session_id=XXX&token=YYY"。
把 type 序列粘过来，例如：
call_started → turn_start → asr_result → agent_reply → tool_call → error → turn_end
-->

```

```

## 相关会话

- session_id：
- 是否可复现：必现 / 偶发（约几次一次）

## 归属区域

- [ ] R1 语音与终端（@张博涵）
- [ ] R2A 对话编排（@佟欣）
- [ ] R2B 工具与业务（@潘柏亦）
- [ ] R3 质检评测（@刘源）
- [ ] R4 平台与交付（@黄睿哲）
- [ ] 不确定

## 环境

- 跑法：`启动.bat` / `启动全部.bat` / `docker compose` / 线上链接
- 是否有 `DASHSCOPE_API_KEY`：有 / 无（无则语音走占位实现，属正常降级）
- Python 版本：
