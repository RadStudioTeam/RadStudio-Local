"""RadStudio Local 单用户 Web UI（标准库实现，无额外依赖）。

启动：``radstudio ui --workspace ~/RadStudio``
安全模型：只监听 127.0.0.1、无登录、单用户；不提供内网共享。
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import local_data
from .report import build_report, collect_report_data

APP_NAME = "RadStudio Local"
TEMPLATE_NAME = "default-radiomics.json"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json$")
RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("radstudio-sdk")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _tail(path: Path, lines: int = 80) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return ""


def _events(out_dir: Path, limit: int = 80) -> list[dict]:
    path = out_dir / "events.jsonl"
    if not path.is_file():
        return []
    found: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    found.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return found[-limit:]


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.workflows_dir = self.root / "workflows"
        self.runs_dir = self.root / "runs"
        for path in (self.root, self.workflows_dir, self.runs_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / "settings.json"
        self.settings = _read_json(self.settings_path, {}) or {}
        self.settings.setdefault("dataDir", str(self.root / "data"))
        self.settings.setdefault("outDir", str(self.runs_dir))
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._ensure_template()

    # ---------- workflows ----------
    def _ensure_template(self) -> None:
        target = self.workflows_dir / TEMPLATE_NAME
        if target.is_file():
            return
        source = Path(__file__).with_name("templates") / "default-workflow.json"
        if source.is_file():
            shutil.copyfile(source, target)

    def list_workflows(self) -> list[dict]:
        items = []
        for path in sorted(self.workflows_dir.glob("*.json")):
            items.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
                }
            )
        return items

    def workflow_path(self, name: str) -> Path:
        if not NAME_RE.match(name or ""):
            raise ValueError("工作流文件名不合法")
        path = (self.workflows_dir / name).resolve()
        if path.parent != self.workflows_dir.resolve() or not path.is_file():
            raise FileNotFoundError(name)
        return path

    def read_workflow(self, name: str) -> str:
        return self.workflow_path(name).read_text(encoding="utf-8")

    def save_workflow(self, name: str, content: str) -> None:
        if not NAME_RE.match(name or ""):
            raise ValueError("工作流文件名只能包含字母/数字/._-，并以 .json 结尾")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"工作流不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise TypeError("工作流需要包含 nodes 数组")
        path = (self.workflows_dir / name).resolve()
        if path.parent != self.workflows_dir.resolve():
            raise ValueError("工作流路径不合法")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def duplicate_template(self, name: str) -> str:
        if not NAME_RE.match(name or ""):
            raise ValueError("工作流文件名不合法")
        if name == TEMPLATE_NAME and not (self.workflows_dir / name).is_file():
            pass
        source = self.workflows_dir / TEMPLATE_NAME
        target = self.workflows_dir / name
        if not source.is_file():
            raise FileNotFoundError(TEMPLATE_NAME)
        if target.exists():
            raise ValueError("同名工作流已存在")
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return name

    # ---------- data ----------
    def scan(self, data_dir: str) -> dict:
        if not data_dir.strip():
            raise ValueError("请填写数据目录")
        workflow = self.workflow_path(TEMPLATE_NAME)
        ir = local_data.load_workflow(workflow)
        reader = local_data.find_reader_node(ir)
        manifest = local_data.discover_dataset(
            Path(data_dir).expanduser(), ir=ir, reader_node=reader
        )
        labels = sorted({row["fields"].get("labelValue", "") for row in manifest["rows"]})
        return {
            "dataDir": str(Path(data_dir).expanduser().resolve()),
            "samples": len(manifest["rows"]),
            "files": len(manifest["files"]),
            "classes": labels,
            "config": manifest["config"],
        }

    # ---------- runs ----------
    def _new_run_id(self) -> str:
        return f"local-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"

    def start_run(self, workflow: str, data_dir: str, run_id: str = "") -> str:
        workflow_path = self.workflow_path(workflow)
        data_path = Path(data_dir).expanduser().resolve()
        if not data_path.is_dir():
            raise ValueError(f"数据目录不存在: {data_path}")
        run_id = (run_id or self._new_run_id()).strip()
        if not RUN_RE.match(run_id):
            raise ValueError("运行 ID 只能包含字母/数字/._-")
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir.resolve():
            raise ValueError("运行路径不合法")
        if run_dir.exists():
            raise ValueError("运行 ID 已存在")
        out_dir = run_dir / "out"
        run_dir.mkdir(parents=True)
        ir = local_data.load_workflow(workflow_path)
        meta = {
            "id": run_id,
            "workflow": workflow,
            "workflowPath": str(workflow_path),
            "dataDir": str(data_path),
            "outDir": str(out_dir),
            "nodesTotal": len(ir.get("nodes") or []),
            "status": "running",
            "startedAt": _now(),
        }
        _write_json(run_dir / "run.json", meta)
        command = [
            sys.executable,
            "-m",
            "radstudio.cli",
            "run",
            str(workflow_path),
            "--data",
            str(data_path),
            "--out",
            str(out_dir),
            "--run-id",
            run_id,
            "--quiet",
        ]
        package_root = Path(__file__).resolve().parent.parent
        log = (run_dir / "cli.log").open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                command, cwd=str(package_root), stdout=log, stderr=subprocess.STDOUT
            )
        finally:
            log.close()
        with self._lock:
            self._procs[run_id] = proc
        return run_id

    def run_status(self, run_id: str) -> dict:
        if not RUN_RE.match(run_id or ""):
            raise FileNotFoundError(run_id)
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir.resolve() or not run_dir.is_dir():
            raise FileNotFoundError(run_id)
        meta = _read_json(run_dir / "run.json", {}) or {}
        proc = self._procs.get(run_id)
        if proc is not None:
            code = proc.poll()
            if code is not None:
                meta["status"] = "completed" if code == 0 else "failed"
                meta["returncode"] = code
                meta["finishedAt"] = _now()
                _write_json(run_dir / "run.json", meta)
                with self._lock:
                    self._procs.pop(run_id, None)
                proc = None
        elif meta.get("status") == "running":
            meta["status"] = "interrupted"
            meta["finishedAt"] = meta.get("finishedAt") or _now()
            _write_json(run_dir / "run.json", meta)
        out_dir = Path(meta.get("outDir") or run_dir / "out")
        events = _events(out_dir)
        completed = sum(1 for event in events if event.get("eventType") == "node.completed")
        meta["events"] = events[-30:]
        meta["nodesCompleted"] = completed
        meta["logTail"] = _tail(run_dir / "cli.log")
        return meta

    def cancel_run(self, run_id: str) -> None:
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        run_dir = self.runs_dir / run_id
        meta = _read_json(run_dir / "run.json", {}) or {}
        meta["status"] = "cancelled"
        meta["finishedAt"] = _now()
        _write_json(run_dir / "run.json", meta)

    def list_runs(self) -> list[dict]:
        items = []
        for path in sorted(self.runs_dir.iterdir(), reverse=True) if self.runs_dir.is_dir() else []:
            if not path.is_dir():
                continue
            meta = _read_json(path / "run.json", {}) or {}
            items.append(
                {
                    "id": path.name,
                    "status": meta.get("status", "unknown"),
                    "workflow": meta.get("workflow", ""),
                    "startedAt": meta.get("startedAt", ""),
                }
            )
            if len(items) >= 50:
                break
        return items

    def results(self, run_id: str) -> tuple[dict, dict | None]:
        meta = self.run_status(run_id)
        out_dir = Path(meta.get("outDir") or "")
        if not out_dir.is_dir():
            return meta, None
        data = collect_report_data(out_dir)
        report = out_dir / "report.html"
        if not report.is_file():
            build_report(out_dir)
        return meta, data

    def artifact(self, run_id: str, relative: str) -> Path:
        meta = self.run_status(run_id)
        out_dir = Path(meta.get("outDir") or "").resolve()
        target = (out_dir / relative).resolve()
        if not target.is_relative_to(out_dir) or not target.is_file():
            raise FileNotFoundError(relative)
        return target

    def save_settings(self, data_dir: str) -> None:
        self.settings["dataDir"] = str(Path(data_dir).expanduser().resolve())
        _write_json(self.settings_path, self.settings)

    # ---------- stop ----------
    def shutdown(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()


# ---------------- DAG SVG ----------------
def dag_svg(ir: dict) -> str:
    nodes = [node for node in (ir.get("nodes") or []) if isinstance(node, dict)]
    if not nodes:
        return "<p class='empty'>工作流没有节点</p>"
    ids = [str(node.get("id")) for node in nodes]
    incoming: dict[str, list[str]] = {node_id: [] for node_id in ids}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in ir.get("edges") or []:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source in incoming and target in incoming:
            incoming[target].append(source)
            outgoing[source].append(target)
    indegree = {node_id: len(incoming[node_id]) for node_id in ids}
    level = {node_id: 0 for node_id in ids}
    queue = [node_id for node_id in ids if indegree[node_id] == 0]
    seen = 0
    while queue:
        current = queue.pop(0)
        seen += 1
        for nxt in outgoing[current]:
            level[nxt] = max(level[nxt], level[current] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen != len(ids):
        for i, node_id in enumerate(ids):
            level[node_id] = i
    by_level: dict[int, list[str]] = {}
    for node_id in ids:
        by_level.setdefault(level[node_id], []).append(node_id)
    box_w, box_h, gap_x, gap_y = 168, 52, 74, 30
    max_rows = max(len(rows) for rows in by_level.values())
    width = max(760, (max(by_level) + 1) * (box_w + gap_x) + 40)
    height = max(260, max_rows * (box_h + gap_y) + 50)
    pos: dict[str, tuple[int, int]] = {}
    for lvl, rows in by_level.items():
        for row, node_id in enumerate(rows):
            pos[node_id] = (20 + lvl * (box_w + gap_x), 25 + row * (box_h + gap_y))
    parts = [
        f'<svg class="dag" viewBox="0 0 {width} {height}" role="img" aria-label="工作流只读图">',
        (
            '<defs><marker id="arrow" markerWidth="8" markerHeight="8"'
            ' refX="7" refY="4" orient="auto">'
            '<path d="M0,0 L8,4 L0,8 z" fill="#7b8ca6"/></marker></defs>'
        ),
    ]
    for edge in ir.get("edges") or []:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source not in pos or target not in pos:
            continue
        sx, sy = pos[source]
        tx, ty = pos[target]
        x1, y1 = sx + box_w, sy + box_h // 2
        x2, y2 = tx, ty + box_h // 2
        mid = max(24, (x2 - x1) // 2)
        parts.append(
            f'<path d="M{x1},{y1} C{x1 + mid},{y1} {x2 - mid},{y2} {x2 - 6},{y2}" '
            'fill="none" stroke="#7b8ca6" stroke-width="1.5" marker-end="url(#arrow)"/>'
        )
    for node in nodes:
        node_id = str(node.get("id"))
        x, y = pos[node_id]
        label = str(node.get("name") or node.get("type") or node_id)
        subtitle = f'{node.get("type", "")} · v{node.get("operatorVersion", "")}'
        parts.append(
            f'<g class="dag-node"><rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="10"/>'
            f'<text x="{x + 12}" y="{y + 21}" class="dag-title">{_e(label[:18])}</text>'
            f'<text x="{x + 12}" y="{y + 39}" class="dag-sub">{_e(subtitle[:26])}</text></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------- HTML ----------------
CSS = """
:root{color-scheme:light dark;--bg:#f5f8fd;--card:#fff;--line:#dde6f2;--text:#16243d;--muted:#5b6b82;--brand:#2a83fa;--brand-dark:#156ffd;--ok:#0f8a5f;--warn:#a86400;--bad:#c0392b;--radius:12px}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
a{color:var(--brand-dark);text-decoration:none}a:hover{text-decoration:underline}
header.top{background:linear-gradient(120deg,#0d1a30,#123a8f);color:#fff;padding:0 20px;display:flex;align-items:center;gap:20px;min-height:60px;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.2px;font-size:17px}.brand span{color:#64d8e6}
nav{display:flex;gap:4px;flex-wrap:wrap}nav a{color:#dce8ff;padding:8px 12px;border-radius:8px;font-size:14px}nav a.active,nav a:hover{background:rgba(255,255,255,.14);text-decoration:none}
main{max-width:1120px;margin:0 auto;padding:26px 20px 70px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:18px;margin:28px 0 12px}h3{font-size:15px;margin:18px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.grid{display:grid;gap:16px}.grid.two{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:0 1px 2px rgba(16,36,64,.05)}
.card h3{margin-top:0}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input[type=text],input[type=number],select,textarea{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text);font:inherit}
textarea{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px;line-height:1.55}
button,.btn{display:inline-block;border:0;border-radius:8px;padding:9px 15px;background:var(--brand-dark);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.secondary,.btn.secondary{background:transparent;color:var(--brand-dark);border:1px solid var(--line)}
button.danger{background:var(--bad)}button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px}th{background:rgba(0,0,0,.03);font-weight:600}
tr:last-child td{border-bottom:0}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;background:#e8eef8;color:#33445f}
.badge.running{background:#e6f0ff;color:#1d5bd6}.badge.completed{background:#e3f6ed;color:var(--ok)}.badge.failed,.badge.cancelled{background:#fdeaea;color:var(--bad)}.badge.interrupted{background:#fff4dd;color:var(--warn)}
pre{background:#0f1622;color:#dbe7f5;padding:14px;border-radius:10px;overflow:auto;font-size:12.5px;line-height:1.55}
code{background:rgba(42,131,250,.1);padding:1px 5px;border-radius:4px;font-size:.92em}
.empty{color:var(--muted)}
.notice{border-left:4px solid var(--brand);background:rgba(42,131,250,.08);padding:10px 14px;border-radius:8px;margin:14px 0}
.error{border-left:4px solid var(--bad);background:rgba(192,57,43,.08);padding:10px 14px;border-radius:8px;margin:14px 0}
.dag{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
.dag-node rect{fill:rgba(42,131,250,.08);stroke:#2a83fa;stroke-width:1.2}
.dag-title{fill:var(--text);font-size:12px;font-weight:600}
.dag-sub{fill:var(--muted);font-size:10.5px}
.thumb{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fff}
.progress{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin:8px 0 4px}.progress>span{display:block;height:100%;background:linear-gradient(90deg,#2a83fa,#2ec7d4)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1622;--card:#172131;--line:#26344a;--text:#e6edf7;--muted:#9fb0c7}header.top{background:linear-gradient(120deg,#0a1220,#12306b)}th{background:rgba(255,255,255,.04)}.badge{background:#1e2a3d;color:#c9d6e8}.dag-node rect{fill:rgba(42,131,250,.14)}}
"""


def layout(active: str, title: str, body: str, workspace: Workspace) -> str:
    nav_items = [
        ("data", "/", "数据"),
        ("workflow", "/workflows", "工作流"),
        ("runs", "/runs", "运行"),
        ("settings", "/settings", "设置"),
    ]
    def _nav_link(item):
        key, href, label = item
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{_e(label)}</a>'

    nav = "".join(_nav_link(item) for item in nav_items)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · {APP_NAME}</title><style>{CSS}</style></head>
<body><header class="top"><div class="brand">Rad<span>Studio</span> Local</div><nav>{nav}</nav>
<span style="margin-left:auto;font-size:12px;color:#c9d8f2">科研用途 · 不用于临床诊断</span></header>
<main>{body}</main></body></html>"""


def _badge(status: str) -> str:
    return f'<span class="badge {_e(status)}">{_e(status)}</span>'


def page_data(workspace: Workspace, scan: dict | None = None, error: str = "") -> str:
    notice = f'<div class="error">{_e(error)}</div>' if error else ""
    if scan:
        rows = "".join(
            f"<tr><td>{_e(key)}</td><td>{_e(value)}</td></tr>"
            for key, value in (
                ("数据目录", scan["dataDir"]),
                ("样本数", scan["samples"]),
                ("文件数", scan["files"]),
                ("标签类别", " / ".join(scan["classes"]) or "（无）"),
                ("影像角色", scan["config"].get("imageRole")),
                ("掩膜角色", scan["config"].get("roiRole")),
                ("正/负类", f"{scan['config'].get('positiveValue')} / {scan['config'].get('negativeValue')}"),
            )
        )
        result = f'<div class="card"><h3>扫描结果</h3><table>{rows}</table></div>'
    else:
        result = ""
    body = f"""<h1>数据</h1><p class="sub">选择本机数据目录；数据不会上传。推荐结构：<code>data/images/</code>、<code>data/masks/</code>、<code>data/labels.csv</code>。</p>
{notice}
<div class="card"><form method="post" action="/scan">
<label for="dataDir">数据目录</label>
<input id="dataDir" type="text" name="dataDir" value="{_e(workspace.settings.get('dataDir', ''))}" placeholder="/path/to/my-study/data">
<div class="row" style="margin-top:12px"><button type="submit">扫描并校验</button><span class="sub" style="margin:0">只读扫描；不修改文件</span></div>
</form></div>
<div style="margin-top:16px">{result}</div>
<div class="notice">还没有工作流？先去 <a href="/workflows">工作流</a> 选择模板；跑通后再到 <a href="/runs">运行</a> 启动。</div>"""
    return layout("data", "数据", body, workspace)


def page_workflows(workspace: Workspace, name: str | None = None, message: str = "", error: str = "") -> str:
    workflows = workspace.list_workflows()
    if not name and workflows:
        name = workflows[0]["name"]
    selected = None
    content = ""
    reader_params: dict = {}
    try:
        if name:
            content = workspace.read_workflow(name)
            selected = json.loads(content)
            reader = local_data.find_reader_node(selected)
            reader_params = dict(reader.get("parameters") or {})
    except Exception as exc:  # noqa: BLE001
        error = error or str(exc)
    def _workflow_item(item):
        bold = ' style="font-weight:700"' if item["name"] == name else ""
        href = f'/workflows?name={quote(item["name"])}'
        return (
            f'<li><a href="{href}"{bold}>{_e(item["name"])}</a>'
            f' <span class="sub">{_e(item["mtime"])}</span></li>'
        )

    listing = "".join(_workflow_item(item) for item in workflows)
    dag = dag_svg(selected) if isinstance(selected, dict) else "<p class='empty'>选择工作流后显示 DAG</p>"
    notice = f'<div class="notice">{_e(message)}</div>' if message else ""
    err = f'<div class="error">{_e(error)}</div>' if error else ""
    body = f"""<h1>工作流</h1><p class="sub">模板 + JSON 编辑；保存后到「运行」启动。工作流是纯 JSON（Workflow IR），可导入云端导出的文件。</p>
{notice}{err}
<div class="grid two">
<div class="card"><h3>工作流列表</h3><ul>{listing or "<li class='empty'>（空）</li>"}</ul>
<form method="post" action="/workflows/new" class="row" style="margin-top:12px">
<input type="text" name="name" placeholder="new-workflow.json" aria-label="新工作流文件名">
<button type="submit" class="secondary">复制默认模板</button></form></div>
<div class="card"><h3>只读 DAG</h3>{dag}</div>
</div>
<div class="card" style="margin-top:16px"><h3>编辑：{_e(name or '（未选择）')}</h3>
<form method="post" action="/workflows/save">
<input type="hidden" name="name" value="{_e(name or '')}">
<textarea name="content" rows="22" spellcheck="false">{_e(content)}</textarea>
<div class="row" style="margin-top:10px"><button type="submit">保存工作流</button><span class="sub" style="margin:0">保存前会校验 JSON 与 nodes</span></div>
</form></div>
<div class="card" style="margin-top:16px"><h3>读取节点参数</h3>
<form method="post" action="/workflows/reader" class="grid two">
<input type="hidden" name="name" value="{_e(name or '')}">
<div><label>imageRole</label><input type="text" name="imageRole" value="{_e(reader_params.get('imageRole', 'image'))}"></div>
<div><label>roiRole</label><input type="text" name="roiRole" value="{_e(reader_params.get('roiRole', 'roi'))}"></div>
<div><label>positiveValue</label><input type="text" name="positiveValue" value="{_e(reader_params.get('positiveValue', '1'))}"></div>
<div><label>negativeValue</label><input type="text" name="negativeValue" value="{_e(reader_params.get('negativeValue', '0'))}"></div>
<div><label><input type="checkbox" name="requireRoi" value="1" {'checked' if reader_params.get('requireRoi', True) else ''}> requireRoi</label></div>
<div class="row" style="align-items:flex-end"><button type="submit" class="secondary">应用读取参数</button></div>
</form></div>"""
    return layout("workflow", "工作流", body, workspace)


def page_runs(workspace: Workspace, message: str = "", error: str = "") -> str:
    workflows = workspace.list_workflows()
    runs = workspace.list_runs()
    options = "".join(
        f'<option value="{_e(item["name"])}">{_e(item["name"])}</option>' for item in workflows
    )
    rows = "".join(
        f'<tr><td><a href="/runs/{quote(item["id"])}">{_e(item["id"])}</a></td>'
        f'<td>{_badge(item["status"])}</td><td>{_e(item["workflow"])}</td><td>{_e(item["startedAt"])}</td></tr>'
        for item in runs
    )
    notice = f'<div class="notice">{_e(message)}</div>' if message else ""
    err = f'<div class="error">{_e(error)}</div>' if error else ""
    body = f"""<h1>运行</h1><p class="sub">在本地执行工作流；运行在后台子进程，页面可刷新查看进度。</p>
{notice}{err}
<div class="card"><h3>新建运行</h3><form method="post" action="/runs/start">
<div class="grid two">
<div><label>工作流</label><select name="workflow">{options}</select></div>
<div><label>数据目录</label><input type="text" name="dataDir" value="{_e(workspace.settings.get('dataDir', ''))}"></div>
<div><label>运行 ID（可选）</label><input type="text" name="runId" placeholder="留空自动生成"></div>
</div><div class="row" style="margin-top:12px"><button type="submit">启动运行</button></div></form></div>
<h2>最近运行</h2><table><thead><tr><th>运行</th><th>状态</th><th>工作流</th><th>开始</th></tr></thead><tbody>{rows or '<tr><td colspan="4" class="empty">还没有运行</td></tr>'}</tbody></table>"""
    return layout("runs", "运行", body, workspace)


def page_run(workspace: Workspace, meta: dict) -> str:
    total = int(meta.get("nodesTotal") or 0)
    done = int(meta.get("nodesCompleted") or 0)
    percent = min(100, int(done * 100 / total)) if total else 0
    running = meta.get("status") == "running"
    refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    events = meta.get("events") or []
    rows = "".join(
        f'<tr><td>{_e(event.get("eventType", ""))}</td><td><code>{_e(json.dumps(event.get("payload", {}), ensure_ascii=False))[:160]}</code></td></tr>'
        for event in events[-20:]
    )
    cancel = (
        f'<form method="post" action="/runs/{quote(meta["id"])}/cancel"><button class="danger" type="submit">取消运行</button></form>'
        if running
        else ""
    )
    body = f"""{refresh}<h1>运行 {_e(meta.get("id"))}</h1><p class="sub">{_badge(meta.get("status", ""))} · {_e(meta.get("workflow", ""))} · {_e(meta.get("startedAt", ""))}</p>
<div class="card"><div class="progress"><span style="width:{percent}%"></span></div><p class="sub">节点 {done}/{total or '?'}</p>
<table><tr><th>数据目录</th><td><code>{_e(meta.get("dataDir", ""))}</code></td></tr><tr><th>输出目录</th><td><code>{_e(meta.get("outDir", ""))}</code></td></tr>
<tr><th>返回码</th><td>{_e(meta.get("returncode", "—"))}</td></tr></table>
<div class="row" style="margin-top:12px">{cancel}<a class="btn secondary" href="/results/{quote(meta.get("id", ""))}">查看结果</a><a class="btn secondary" href="/runs">返回运行列表</a></div></div>
<div class="grid two" style="margin-top:16px"><div class="card"><h3>事件</h3><table><tbody>{rows or '<tr><td class="empty">等待事件…</td></tr>'}</tbody></table></div>
<div class="card"><h3>CLI 日志（尾部）</h3><pre>{_e(meta.get("logTail", ""))}</pre></div></div>"""
    return layout("runs", f"运行 {meta.get('id')}", body, workspace)


def page_results(workspace: Workspace, meta: dict, data: dict | None) -> str:
    if data is None:
        return layout("runs", "结果", f"<h1>结果</h1><div class='error'>输出目录不存在：{_e(meta.get('outDir', ''))}</div>", workspace)
    metrics = []
    for name, values in data["metrics"]:
        rows = "".join(
            f"<tr><td>{_e(key)}</td><td>{_e(f'{value:.6g}' if isinstance(value, float) else value)}</td></tr>"
            for key, value in list(values.items())[:40]
        )
        metrics.append(f"<div class='card'><h3>{_e(name)}</h3><table>{rows}</table></div>")
    images = []
    out_dir = Path(data["dir"])
    for path in data["images"]:
        rel = path.relative_to(out_dir).as_posix()
        images.append(
            f"<figure><img class='thumb' src=\"/artifact/{quote(meta['id'])}/{quote(rel)}\" alt=\"{_e(path.name)}\">"
            f"<figcaption class='sub'>{_e(rel)}</figcaption></figure>"
        )
    artifacts = "".join(
        f"<tr><td>{_e(item['type'])}</td><td>{_e(item['node'])}</td><td><code>{_e(item['path'])}</code></td></tr>"
        for item in data["artifacts"]
    )
    body = f"""<h1>结果</h1><p class="sub">{_badge(meta.get("status", ""))} · {_e(meta.get("id", ""))} · <a href="/report/{quote(meta.get("id", ""))}">打开完整静态报告</a></p>
<div class="row"><a class="btn secondary" href="/runs/{quote(meta.get("id", ""))}">返回运行</a><a class="btn secondary" href="/runs">运行列表</a></div>
<h2>指标</h2><div class="grid two">{''.join(metrics) or '<p class="empty">没有指标 JSON</p>'}</div>
<h2>图表</h2><div class="grid two">{''.join(images) or '<p class="empty">没有图片产物</p>'}</div>
<h2>产物</h2><table><thead><tr><th>类型</th><th>节点</th><th>路径</th></tr></thead><tbody>{artifacts or '<tr><td colspan="3" class="empty">缺少 manifest.json</td></tr>'}</tbody></table>
<p class="sub" style="margin-top:18px">报告与产物均在本地；需要团队协作/GPU/AI 时可在云端打开对应工作流。</p>"""
    return layout("runs", "结果", body, workspace)


def page_settings(workspace: Workspace, message: str = "") -> str:
    notice = f'<div class="notice">{_e(message)}</div>' if message else ""
    body = f"""<h1>设置</h1><p class="sub">单用户本地模式；服务只监听 127.0.0.1，无登录、无遥测。</p>{notice}
<div class="card"><h3>工作区</h3><table>
<tr><th>工作区</th><td><code>{_e(workspace.root)}</code></td></tr>
<tr><th>工作流目录</th><td><code>{_e(workspace.workflows_dir)}</code></td></tr>
<tr><th>运行目录</th><td><code>{_e(workspace.runs_dir)}</code></td></tr>
<tr><th>版本</th><td>{_e(_version())}</td></tr>
<tr><th>Python</th><td>{_e(sys.version.split()[0])}</td></tr></table></div>
<div class="card" style="margin-top:16px"><h3>默认数据目录</h3>
<form method="post" action="/settings"><label for="dataDir">数据目录</label>
<input id="dataDir" type="text" name="dataDir" value="{_e(workspace.settings.get('dataDir', ''))}">
<div class="row" style="margin-top:10px"><button type="submit">保存</button></div></form></div>
<div class="notice">共享给同事/内网访问不在开源版范围；请使用云端团队空间。</div>"""
    return layout("settings", "设置", body, workspace)


# ---------------- HTTP ----------------
class LocalUIHandler(BaseHTTPRequestHandler):
    server_version = "RadStudioLocal/0.2"
    protocol_version = "HTTP/1.1"
    workspace: Workspace

    def log_message(self, format, *args) -> None:
        return

    # ---- helpers ----
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    def _error_page(self, status: int, message: str) -> None:
        self._html(status, layout("runs", "出错了", f"<h1>出错了</h1><div class='error'>{_e(message)}</div>", self.workspace))

    # ---- GET ----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/healthz":
                self._json(200, {"status": "ok", "version": _version()})
                return
            if path == "/":
                self._html(200, page_data(self.workspace))
                return
            if path == "/workflows":
                name = (query.get("name") or [None])[0]
                self._html(200, page_workflows(self.workspace, name=name))
                return
            if path == "/runs":
                self._html(200, page_runs(self.workspace))
                return
            if path.startswith("/api/runs/"):
                run_id = path.split("/")[-1]
                self._json(200, self.workspace.run_status(run_id))
                return
            if path.startswith("/runs/"):
                run_id = path.split("/")[2] if len(path.split("/")) > 2 else ""
                meta = self.workspace.run_status(run_id)
                self._html(200, page_run(self.workspace, meta))
                return
            if path.startswith("/results/"):
                run_id = path.split("/")[2] if len(path.split("/")) > 2 else ""
                meta, data = self.workspace.results(run_id)
                self._html(200, page_results(self.workspace, meta, data))
                return
            if path.startswith("/report/"):
                run_id = path.split("/")[2] if len(path.split("/")) > 2 else ""
                meta, data = self.workspace.results(run_id)
                if data is None:
                    raise FileNotFoundError(run_id)
                report = Path(meta["outDir"]) / "report.html"
                self._send(200, report.read_bytes(), "text/html; charset=utf-8")
                return
            if path.startswith("/artifact/"):
                parts = path.split("/", 3)
                if len(parts) < 4:
                    raise FileNotFoundError(path)
                target = self.workspace.artifact(parts[2], unquote(parts[3]))
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(200, target.read_bytes(), mime)
                return
            if path == "/settings":
                self._html(200, page_settings(self.workspace))
                return
            self._error_page(404, f"页面不存在: {path}")
        except FileNotFoundError as exc:
            self._error_page(404, f"找不到: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._error_page(500, str(exc))

    # ---- POST ----
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        form = self._form()
        try:
            if path == "/scan":
                try:
                    scan = self.workspace.scan(form.get("dataDir", ""))
                    self._html(200, page_data(self.workspace, scan=scan))
                except Exception as exc:  # noqa: BLE001
                    self._html(200, page_data(self.workspace, error=str(exc)))
                return
            if path == "/settings":
                self.workspace.save_settings(form.get("dataDir", ""))
                self._html(200, page_settings(self.workspace, "已保存默认数据目录"))
                return
            if path == "/workflows/save":
                self.workspace.save_workflow(form.get("name", ""), form.get("content", ""))
                self._redirect(f"/workflows?name={quote(form.get('name', ''))}&msg={quote('已保存')}")
                return
            if path == "/workflows/new":
                name = self.workspace.duplicate_template(form.get("name", ""))
                self._redirect(f"/workflows?name={quote(name)}&msg={quote('已复制默认模板')}")
                return
            if path == "/workflows/reader":
                name = form.get("name", "")
                content = json.loads(self.workspace.read_workflow(name))
                reader = local_data.find_reader_node(content)
                params = dict(reader.get("parameters") or {})
                params["imageRole"] = form.get("imageRole", "image")
                params["roiRole"] = form.get("roiRole", "roi")
                params["requireRoi"] = form.get("requireRoi") == "1"
                params["positiveValue"] = form.get("positiveValue", "1")
                params["negativeValue"] = form.get("negativeValue", "0")
                reader["parameters"] = params
                self.workspace.save_workflow(name, json.dumps(content, ensure_ascii=False))
                self._redirect(f"/workflows?name={quote(name)}&msg={quote('读取参数已更新')}")
                return
            if path == "/runs/start":
                try:
                    run_id = self.workspace.start_run(
                        form.get("workflow", ""), form.get("dataDir", ""), form.get("runId", "")
                    )
                    self._redirect(f"/runs/{quote(run_id)}")
                except Exception as exc:  # noqa: BLE001
                    self._html(200, page_runs(self.workspace, error=str(exc)))
                return
            if path.startswith("/runs/") and path.endswith("/cancel"):
                run_id = path.split("/")[2]
                self.workspace.cancel_run(run_id)
                self._redirect(f"/runs/{quote(run_id)}")
                return
            self._error_page(404, f"接口不存在: {path}")
        except Exception as exc:  # noqa: BLE001
            self._error_page(500, str(exc))


def make_server(workspace: Workspace, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (LocalUIHandler,), {"workspace": workspace})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Server((host, port), handler)


def serve(workspace_root: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    workspace = Workspace(workspace_root)
    server = make_server(workspace, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"RadStudio Local UI: {url}")
    print(f"工作区: {workspace.root}")
    print("仅监听本机；Ctrl+C 退出")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        workspace.shutdown()
