"""浏览器内文档站：把仓库 design/ 与 docs/ 的 Markdown 渲染成带侧边目录的网页。

挂在服务的 `/guide` 路由，配合 FastAPI 自带的 `/docs`(Swagger) 与 `/redoc`，让团队在浏览器
点开看**设计文档**与**使用指南**。极简实现：**无第三方依赖、离线可用**（自带 markdown→HTML）。

仅渲染白名单内文件（design/*.md、docs/*.md、README.md），避免路径穿越。
仓库根默认取包的上级目录，可用 `VORTEX_BACKTEST_DOCS_ROOT` 覆盖（部署时指向代码仓）。
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path


def _repo_root() -> Path:
    env = os.getenv("VORTEX_BACKTEST_DOCS_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[1]


def _title(path: Path, default: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return default


def discover() -> list[tuple[str, str, str]]:
    """返回 [(分组, 相对路径, 标题)]：README + design/*.md + docs/*.md。"""
    root = _repo_root()
    out: list[tuple[str, str, str]] = []
    readme = root / "README.md"
    if readme.exists():
        out.append(("概览", "README.md", _title(readme, "README")))
    for cat, sub in (("设计 (design/)", "design"), ("使用 (docs/)", "docs")):
        d = root / sub
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                out.append((cat, f"{sub}/{p.name}", _title(p, p.stem)))
    return out


# ---------------- 极简 Markdown 渲染（够用即可：标题/段落/列表/表格/代码/引用/分割线/行内）----------------

def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


_BLOCK_START = re.compile(r"^(#{1,6}\s|```|>|---+\s*$|\s*([-*]|\d+\.)\s)")


def render_markdown(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    list_stack: list[str] = []

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    while i < n:
        line = lines[i]

        if re.match(r"^```", line):                                   # 代码块
            close_lists()
            i += 1
            buf: list[str] = []
            while i < n and not re.match(r"^```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue

        if "|" in line and i + 1 < n and "-" in lines[i + 1] and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            close_lists()                                             # 表格
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>")
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)                      # 标题
        if m:
            close_lists()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        if re.match(r"^---+\s*$", line):                             # 分割线
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        if line.startswith(">"):                                     # 引用
            close_lists()
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + _inline(" ".join(buf)) + "</blockquote>")
            continue

        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)             # 列表
        if m:
            kind = "ol" if m.group(2)[0].isdigit() else "ul"
            if not list_stack or list_stack[-1] != kind:
                close_lists()
                out.append(f"<{kind}>")
                list_stack.append(kind)
            out.append(f"<li>{_inline(m.group(3))}</li>")
            i += 1
            continue

        if not line.strip():                                         # 空行
            close_lists()
            i += 1
            continue

        close_lists()                                                # 段落（聚合换行包裹的连续行）
        buf = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _BLOCK_START.match(lines[i]) and "|" not in lines[i]:
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")

    close_lists()
    return "\n".join(out)


_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>vortex_backtest 文档 · %%ACTIVE%%</title>
<style>
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--accent:#0969da;--code:#f6f8fa;}
*{box-sizing:border-box}body{margin:0;font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg);background:var(--bg)}
.top{position:sticky;top:0;display:flex;gap:16px;align-items:center;padding:10px 20px;border-bottom:1px solid var(--line);background:#fafbfc;z-index:5}
.top b{font-size:15px}.top a{color:var(--accent);text-decoration:none;font-size:14px}.top a:hover{text-decoration:underline}
.wrap{display:flex;align-items:flex-start}
.side{width:280px;flex:none;height:calc(100vh - 49px);overflow:auto;border-right:1px solid var(--line);padding:14px 10px}
.side .cat{margin:14px 8px 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.side a{display:block;padding:5px 10px;border-radius:6px;color:var(--fg);text-decoration:none;font-size:13.5px}
.side a:hover{background:var(--code)}.side a.active{background:#ddf4ff;color:var(--accent);font-weight:600}
.main{flex:1;min-width:0;max-width:900px;margin:0 auto;padding:24px 40px 80px}
.main h1{font-size:28px;border-bottom:1px solid var(--line);padding-bottom:.3em;margin-top:0}
.main h2{font-size:22px;border-bottom:1px solid var(--line);padding-bottom:.3em;margin-top:1.6em}
.main h3{font-size:18px}.main h4{font-size:16px}
.main code{background:var(--code);padding:.15em .4em;border-radius:5px;font-size:85%;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.main pre{background:var(--code);padding:14px 16px;border-radius:8px;overflow:auto;border:1px solid var(--line)}
.main pre code{background:none;padding:0;font-size:13px}
.main table{border-collapse:collapse;margin:1em 0;display:block;overflow:auto}
.main th,.main td{border:1px solid var(--line);padding:6px 12px;text-align:left;font-size:13.5px;vertical-align:top}
.main th{background:var(--code)}
.main blockquote{margin:1em 0;padding:.4em 1em;color:var(--muted);border-left:4px solid var(--line);background:#fafbfc}
.main hr{border:0;border-top:1px solid var(--line);margin:1.6em 0}
.main a{color:var(--accent)}
</style></head>
<body>
<div class="top"><b>📘 vortex_backtest 文档</b>
  <a href="/guide">📖 文档站</a>
  <a href="/docs">🔌 API (Swagger)</a>
  <a href="/redoc">📑 API (ReDoc)</a>
  <a href="/ui/">📊 看板</a>
</div>
<div class="wrap">
  <nav class="side">%%SIDEBAR%%</nav>
  <article class="main">%%CONTENT%%</article>
</div>
</body></html>"""


def render_site(active: str | None) -> str:
    docs = discover()
    if not docs:
        return ("<h1>文档未找到</h1><p>请从代码仓根目录运行服务，"
                "或设置环境变量 <code>VORTEX_BACKTEST_DOCS_ROOT</code> 指向代码仓。</p>")
    allowed = {rel: title for _, rel, title in docs}
    if active not in allowed:
        active = "README.md" if "README.md" in allowed else docs[0][1]
    content = render_markdown((_repo_root() / active).read_text(encoding="utf-8"))

    side: list[str] = []
    cur = None
    for cat, rel, title in docs:
        if cat != cur:
            side.append(f'<div class="cat">{html.escape(cat)}</div>')
            cur = cat
        active_cls = " active" if rel == active else ""
        side.append(f'<a class="{active_cls.strip()}" href="/guide/{rel}">{html.escape(title)}</a>')

    return (_PAGE
            .replace("%%ACTIVE%%", html.escape(allowed[active]))
            .replace("%%SIDEBAR%%", "\n".join(side))
            .replace("%%CONTENT%%", content))
