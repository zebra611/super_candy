#!/usr/bin/env python3
"""把 eval/ui/template.html + avatars.json 打成单文件预览。

为什么要打单文件：
    展示给别人时，发两个目录（html + assets）很容易被压平或漏图。
    内联成 base64 后只有一个 .html，双击就能看，也不依赖预览服务器的静态路由。

为什么用脚本而不是手改：
    换头像、改名字、加人，都只改 avatars.json；手改 HTML 会和模板分叉，
    下次换人又要重来一遍。

用法：
    python3 scripts/build_ui_preview.py
    python3 scripts/build_ui_preview.py --out /tmp/preview.html
"""

import argparse
import base64
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(ROOT, "eval", "ui")
TEMPLATE = os.path.join(UI_DIR, "template.html")
MANIFEST = os.path.join(UI_DIR, "avatars.json")
OUT = os.path.join(UI_DIR, "preview.html")

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def data_uri(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in MIME:
        raise SystemExit("不支持的图片格式：%s" % path)
    with open(path, "rb") as f:
        return "data:%s;base64,%s" % (MIME[ext], base64.b64encode(f.read()).decode())


def load_manifest():
    with open(MANIFEST, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data["avatars"]
    if not items:
        raise SystemExit("avatars.json 里没有头像")
    for a in items:
        path = os.path.join(UI_DIR, "assets", a["file"])
        if not os.path.isfile(path):
            raise SystemExit("缺图片：%s" % path)
        if a.get("gender") not in ("f", "m"):
            raise SystemExit("%s 的 gender 只能是 f/m" % a["id"])
    return items


def build(html, items):
    groups = {"f": [], "m": []}
    for a in items:
        groups[a["gender"]].append({
            "img": data_uri(os.path.join(UI_DIR, "assets", a["file"])),
            "name": a["name"],
        })

    js = ("const STAFF=" + json.dumps(groups, ensure_ascii=False)
          + ";\nlet cur={g:\"f\",i:0};\n")

    # 用 JSON 生成 STAFF，替换模板里的同名常量（保留后面的 renderStaff/setGender 逻辑）
    start = html.index("const STAFF=")
    end = html.index("let cur=", start)
    html = html[:start] + js + html[end + html[end:].index(";\n") + 2:]

    # 首屏 <img src> 也换成内联，避免 JS 未执行时空白
    first = groups["f"][0]["img"] if groups["f"] else groups["m"][0]["img"]
    # 模板里的首屏 src 可能带序号也可能不带（avatar-f.jpg / avatar-f1.jpg），
    # 两种都要替换，否则 JS 未执行时首屏会露一个 404。
    # 首屏 src 统一换成 1px 占位：真图由 renderStaff() 立刻填入。
    # 直接把 base64 塞进两个 <img> 会让同一张图在文件里存三份（多 180KB）。
    placeholder = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
    html = re.sub(r'src="assets/avatar-[fm]\d*\.jpg"', 'src="%s"' % placeholder, html)
    return html


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成双端界面的单文件预览")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()
    items = load_manifest()
    out = build(html, items)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out)
    print("生成 %s（%d 位客服，%.0f KB）"
          % (os.path.relpath(args.out, ROOT), len(items), len(out.encode()) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
