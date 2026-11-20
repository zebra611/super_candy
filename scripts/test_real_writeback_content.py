# -*- coding: utf-8 -*-
"""P0-1 对端回写验证：real 模式建单后，对端 /mock/tickets 里必须能读到完整 content。

为什么单独起一个服务副本、用临时数据目录：
    mock_business/service.py 把工单落盘到它自己目录下的 data/tickets.json。
    如果直接连仓库里那个服务，测试会污染演示存档（下次演示多出一张 T 开头的单）。
    这里把 service.py 复制到 tempfile 临时目录起进程，_DATA 跟着副本走，
    测完连目录一起删，仓库数据一个字节都不动。

它守的是什么：
    RealAdapter.create_ticket 以前 body 只带四个固定键（customer_id/order_id/
    consent/need），reject_category 这类场景字段全丢；而本地副本 _last_ticket
    照样记了完整 content，test_tools 里那条断言照样绿——典型的"自己验自己"。
    这条测试从**对端 HTTP** 读回来，缺了 body["content"] 就会红。

变异验证（证明它不是摆设）：把 real.py body 里 "content": content 那行删掉，
跑本测试必须失败；恢复后全绿。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules.adapters.real import RealAdapter


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class RealWritebackContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="sw_real_writeback_")
        svc_dir = os.path.join(cls._tmp, "mock_business")
        os.makedirs(svc_dir)
        shutil.copy(os.path.join(ROOT, "mock_business", "service.py"),
                    os.path.join(svc_dir, "service.py"))
        cls._port = _free_port()
        cls._proc = subprocess.Popen(
            [sys.executable, os.path.join(svc_dir, "service.py"), str(cls._port)],
            cwd=svc_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % cls._port
        for _ in range(60):
            try:
                _get_json(base + "/mock/health", timeout=1)
                break
            except Exception:
                if cls._proc.poll() is not None:
                    raise RuntimeError("mock 服务副本启动失败")
                time.sleep(0.1)
        else:
            raise RuntimeError("mock 服务副本 6s 内未就绪")
        cls._base = base

    @classmethod
    def tearDownClass(cls):
        try:
            cls._proc.terminate()
            cls._proc.wait(timeout=3)
        except Exception:
            try:
                cls._proc.kill()
            except Exception:
                pass
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_writeback_content_reaches_remote(self):
        """建单后从对端 /mock/tickets 读回，场景字段必须一字不差。"""
        content = {
            "customer_id": "C0031",
            "order_id": "O0031",
            "consent": True,
            "need": "POP 资质驳回原因咨询",
            "reject_category": "材料不全",
            "reject_summary": "缺少经营场所证明",
            "merchant_understood": True,
            "checklist_sent": True,
            "corrections": ["尾号以档案为准"],
        }
        key = "s-real-content:1"
        ad = RealAdapter({"provider": "real", "base_url": self._base,
                          "timeout_ms": 2000})
        result = ad.create_ticket("s-real-content", content, key)
        self.assertTrue(result["ticket_id"], "对端应返回工单号")

        tickets = _get_json(self._base + "/mock/tickets")["tickets"]
        hit = [t for t in tickets if t.get("idempotency_key") == key]
        self.assertEqual(len(hit), 1, "对端应有且仅有一张这张单")
        stored = hit[0]["content"]
        # 这五个字段以前全丢；缺 body["content"] 时 stored 是 {}，这里必红
        self.assertEqual(stored.get("reject_category"), "材料不全")
        self.assertEqual(stored.get("reject_summary"), "缺少经营场所证明")
        self.assertIs(stored.get("merchant_understood"), True)
        self.assertIs(stored.get("checklist_sent"), True)
        self.assertEqual(stored.get("corrections"), ["尾号以档案为准"])
        # 固定键也仍在
        self.assertEqual(stored.get("customer_id"), "C0031")
        self.assertEqual(stored.get("need"), "POP 资质驳回原因咨询")


if __name__ == "__main__":
    unittest.main(verbosity=2)
