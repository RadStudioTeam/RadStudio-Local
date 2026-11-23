"""生成无服务端的静态 HTML 运行报告（单文件，图片按相对路径引用）。"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

METRIC_SUFFIXES = {".json"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}
MAX_JSON_BYTES = 4 << 20


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _flatten(value: Any, prefix: str = "", depth: int = 0, out: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    if depth > 3 or len(out) >= 200:
        return out
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(child, f"{prefix}.{key}" if prefix else str(key), depth + 1, out)
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        or isinstance(value, str)
        and len(value) <= 80
        and bool(prefix)
        and any(
            word in prefix.lower()
            for word in ("auc", "accuracy", "sensitivity", "specificity", "f1", "brier", "score")
        )
    ):
        out[prefix] = value
    return out


def _metrics(out_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    results: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(out_dir.rglob("*.json")):
        if "_inputs" in path.parts or path.name in {"manifest.json", "operator-catalog.json", "events.jsonl"}:
            continue
        payload = _read_json(path)
        if payload is None:
            continue
        flat = _flatten(payload)
        if flat:
            results.append((str(path.relative_to(out_dir)), flat))
        if len(results) >= 8:
            break
    return results


def _images(out_dir: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if "_inputs" in path.parts:
            continue
        found.append(path)
        if len(found) >= 40:
            break
    return found


def _artifacts(out_dir: Path) -> list[dict[str, Any]]:
    manifest = _read_json(out_dir / "manifest.json")
    if not isinstance(manifest, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in manifest:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "type": item.get("artifactType") or item.get("type") or "",
                "node": item.get("nodeId") or "",
                "path": item.get("path") or "",
            }
        )
    return rows


def _run_meta(out_dir: Path) -> dict[str, Any]:
    payload = _read_json(out_dir / "_inputs" / "run-meta.json")
    return payload if isinstance(payload, dict) else {}


def _table(headers: list[str], rows: list[list[str]], empty: str = "（无）") -> str:
    if not rows:
        return f'<p class="empty">{html.escape(empty)}</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def collect_report_data(out_dir: Path) -> dict[str, Any]:
    """收集报告/UI 需要的元数据、指标、图片与产物清单。"""
    out_dir = out_dir.resolve()
    if not out_dir.is_dir():
        raise FileNotFoundError(f"输出目录不存在: {out_dir}")
    return {
        "dir": out_dir,
        "meta": _run_meta(out_dir),
        "metrics": _metrics(out_dir),
        "images": _images(out_dir),
        "artifacts": _artifacts(out_dir),
    }


def build_report(out_dir: Path) -> Path:
    data = collect_report_data(out_dir)
    out_dir = data["dir"]
    meta = data["meta"]
    metrics = data["metrics"]
    images = data["images"]
    artifacts = data["artifacts"]

    metric_sections = []
    for name, values in metrics:
        rows = [[key, f"{value:.6g}" if isinstance(value, float) else str(value)] for key, value in values.items()]
        metric_sections.append(f"<h3>{html.escape(name)}</h3>" + _table(["指标", "值"], rows))

    image_cards = []
    for path in images:
        rel = path.relative_to(out_dir).as_posix()
        image_cards.append(
            f'<figure><img src="{html.escape(rel)}" alt="{html.escape(path.name)}" loading="lazy">'
            f"<figcaption>{html.escape(rel)}</figcaption></figure>"
        )

    artifact_rows = [[item["type"], item["node"], item["path"]] for item in artifacts]
    run_rows = [[key, str(value)] for key, value in meta.items()]
    generated = html.escape(__import__("time").strftime("%Y-%m-%d %H:%M:%S"))
    run_table = _table(["字段", "值"], run_rows, "缺少 _inputs/run-meta.json")
    metrics_html = "".join(metric_sections) or '<p class="empty">（没有找到指标 JSON）</p>'
    images_html = "".join(image_cards) or '<p class="empty">（没有找到图片）</p>'
    artifacts_table = _table(["类型", "节点", "路径"], artifact_rows, "缺少 manifest.json")

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RadStudio Local 运行报告</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0; background: #f6f8fb; color: #16243d; }}
  main {{ max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 1px solid #dbe4f0; }}
  h3 {{ font-size: 15px; margin: 18px 0 8px; }}
  .sub {{ color: #5b6b82; margin: 0 0 24px; }}
  .warn {{ background: #fff8e6; border: 1px solid #f0d38a; padding: 10px 14px; border-radius: 8px; color: #6b4e00; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 2px rgba(16,36,64,.06); }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef2f7; font-size: 13px; }}
  th {{ background: #f0f4fa; font-weight: 600; }}
  code {{ background: #eef2f7; padding: 1px 5px; border-radius: 4px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }}
  figure {{ margin: 0; background: #fff; border-radius: 8px; padding: 10px; box-shadow: 0 1px 2px rgba(16,36,64,.06); }}
  figure img {{ width: 100%; height: auto; display: block; }}
  figcaption {{ font-size: 12px; color: #5b6b82; margin-top: 6px; word-break: break-all; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0f1622; color: #e6edf7; }}
    table, figure {{ background: #172131; box-shadow: none; }}
    th {{ background: #1e2a3d; }}
    th, td {{ border-bottom-color: #26344a; }}
    code {{ background: #1e2a3d; }}
    h2 {{ border-bottom-color: #26344a; }}
    .sub, figcaption {{ color: #9fb0c7; }}
    .warn {{ background: #2a2415; border-color: #6b5a20; color: #e8d9a8; }}
  }}
</style>
</head>
<body>
<main>
  <h1>RadStudio Local 运行报告</h1>
  <p class="sub">生成于 {generated} · <code>{html.escape(str(out_dir))}</code></p>
  <p class="warn">本报告仅供科研与教学使用，不构成临床诊断依据。</p>

  <h2>运行信息</h2>
  {run_table}

  <h2>指标</h2>
  {metrics_html}

  <h2>图表</h2>
  <div class="grid">{images_html}</div>

  <h2>产物清单</h2>
  {artifacts_table}

  <p class="sub">报告由 <code>radstudio report</code> 生成；产物路径相对于本文件。需要团队协作/GPU/AI 时可在云端打开对应工作流。</p>
</main>
</body>
</html>
"""
    target = out_dir / "report.html"
    target.write_text(document, encoding="utf-8")
    return target
