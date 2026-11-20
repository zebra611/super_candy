# -*- coding: utf-8 -*-
"""P3-4 断言：看板 API 返回值自数据来源标签，前端有持久徽标。

守两件事：
  1. dashboard.summary(db="demo") 返回 is_demo=True + source_label 明示"演示数据"；
     summary(db="live") 返回 is_demo=False——截图/JSON 脱离上下文也能自证；
  2. dashboard.html 的 sticky header 里有 #source-badge，且 JS 按 data_source/is_demo
     设置它、demo 时加 .demo 高亮——不是只靠正文里可能被截掉的横幅。

全程只读，不写生产库。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dashboard  # noqa: E402


class DemoSourceLabelTest(unittest.TestCase):
    def test_live_api_self_describes(self):
        out = dashboard.summary(db="live", use_cache=False)
        self.assertEqual(out.get("data_source"), "live")
        self.assertIs(out.get("is_demo"), False)
        self.assertIn("真实", out.get("source_label", ""))

    def test_demo_api_self_describes(self):
        if not os.path.exists(dashboard.DEMO_DB):
            self.skipTest("演示库不存在，跳过（先跑 scripts/make_demo_db.py）")
        out = dashboard.summary(db="demo", use_cache=False)
        self.assertEqual(out.get("data_source"), "demo")
        self.assertIs(out.get("is_demo"), True)
        label = out.get("source_label", "")
        self.assertIn("演示", label)
        self.assertIn("非真实", label)

    def test_html_has_persistent_badge(self):
        with open(os.path.join(ROOT, "web", "dashboard.html"),
                  encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="source-badge"', html, "header 应有持久数据源徽标")
        self.assertIn(".source-badge", html, "应有徽标样式")
        self.assertIn(".source-badge.demo", html, "演示态应有高亮样式")
        # JS 必须真正绑定：读 data_source/is_demo 并设置徽标文案 + demo 类
        self.assertIn('$("source-badge")', html, "JS 应引用徽标元素")
        self.assertIn('badge.textContent', html, "JS 应设置徽标文案")
        self.assertIn('badge.classList.toggle("demo"', html, "JS 应切换 demo 高亮")


if __name__ == "__main__":
    unittest.main(verbosity=2)
