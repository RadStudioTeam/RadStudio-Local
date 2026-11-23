"""RadStudio Local 命令行入口。

子命令：
  run     从 workflow.json + 本地数据目录构建快照并运行
  replay  用复现包 + 本地数据重跑
  report  生成静态 HTML 报告（无服务端）
  doctor  检查本地环境
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .local_data import (
    LocalDataError,
    build_runner_request,
    discover_dataset,
    find_reader_node,
    load_workflow,
    write_manifest,
)

CLOUD_UTM = "https://app.radstudioi.com/?utm_source=cli&utm_medium=cli&utm_campaign=oss"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("radstudio-sdk")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _build_run_parser(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("workflow", type=Path, help="Workflow IR 或云端导出的 workflow.json")
    sub.add_argument("--data", type=Path, default=Path("data"), help="数据目录（默认 ./data）")
    sub.add_argument("--out", type=Path, default=Path("out"), help="输出目录（默认 ./out）")
    sub.add_argument("--images", type=Path, help="图片目录（默认 data/images，否则 data/）")
    sub.add_argument("--masks", type=Path, help="掩膜目录（默认 data/masks、rois 或 roi）")
    sub.add_argument("--labels", type=Path, help="标签 CSV/TSV（默认 data/labels.csv）")
    sub.add_argument("--id-column", help="标签表中的 sample id 列名")
    sub.add_argument("--label-column", help="标签表中的标签列名")
    sub.add_argument("--patient-column", help="标签表中的患者列名")
    sub.add_argument("--positive", default="", help="正类标签值（默认 1 或按两类推断）")
    sub.add_argument("--negative", default="", help="负类标签值（默认 0 或按两类推断）")
    sub.add_argument("--allow-missing-roi", action="store_true", help="允许没有掩膜（无 ROI 工作流）")
    sub.add_argument("--reader-node", help="ReadImages 节点 id（多读取节点时必填）")
    sub.add_argument("--target-node", help="只运行到该节点（调试用）")
    sub.add_argument("--run-id", help="运行 ID（默认 local-<时间戳>）")
    sub.add_argument("--events", type=Path, help="事件 JSONL 路径（默认 <out>/events.jsonl）")
    sub.add_argument("--quiet", action="store_true", help="不打印逐节点进度")


def _build_replay_parser(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("bundle", type=Path, help="复现包目录（含 request.json / input-hashes.json）")
    sub.add_argument("--dataset-dir", type=Path, required=True, help="本地数据目录")
    sub.add_argument("--out", type=Path, default=Path("out"), help="输出目录")


def _build_report_parser(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("out", type=Path, help="运行输出目录")
    sub.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")


def _build_ui_parser(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--workspace", type=Path, default=Path.home() / "RadStudioLocal", help="本地工作区目录")
    sub.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    sub.add_argument("--port", type=int, default=8765, help="端口")
    sub.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")


def _build_doctor_parser(sub: argparse.ArgumentParser) -> None:
    pass


def _emit_factory(events_path: Path | None, quiet: bool):
    handle = None
    if events_path is not None:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        handle = events_path.open("w", encoding="utf-8")

    def emit(event_type: str, payload: dict) -> None:
        if handle is not None:
            handle.write(json.dumps({"eventType": event_type, "payload": payload}, ensure_ascii=False) + "\n")
            handle.flush()
        if quiet:
            return
        if event_type == "node.started":
            print(f"▶ {payload.get('nodeId')}", file=sys.stderr)
        elif event_type == "node.completed":
            print(f"✔ {payload.get('nodeId')}", file=sys.stderr)
        elif event_type == "node.failed":
            print(
                f"✖ {payload.get('nodeId')}: {payload.get('errorCode')} {payload.get('errorMessage')}",
                file=sys.stderr,
            )
        elif event_type == "run.catalog.accepted":
            print(f"算子快照已接受（digest {str(payload.get('digest'))[:12]}…）", file=sys.stderr)

    return emit, handle


def cmd_run(args: argparse.Namespace) -> int:
    from . import runner

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    ir = load_workflow(args.workflow)
    reader = find_reader_node(ir, node_id=args.reader_node)
    manifest = discover_dataset(
        args.data,
        ir=ir,
        reader_node=reader,
        images_dir=args.images,
        masks_dir=args.masks,
        labels_path=args.labels,
        id_column=args.id_column,
        label_column=args.label_column,
        patient_column=args.patient_column,
        positive=args.positive,
        negative=args.negative,
        allow_missing_roi=args.allow_missing_roi,
    )
    manifest_path, manifest_hash = write_manifest(manifest, out)
    run_id = args.run_id or f"local-{time.strftime('%Y%m%d-%H%M%S')}"
    request = build_runner_request(
        ir,
        reader_node_id=str(reader["id"]),
        manifest_path=manifest_path,
        manifest_sha256=manifest_hash,
        run_id=run_id,
        target_node=args.target_node,
    )
    inputs = out / "_inputs"
    (inputs / "request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    (inputs / "run-meta.json").write_text(
        json.dumps(
            {
                "runId": run_id,
                "cliVersion": _version(),
                "dataDir": str(args.data.resolve()),
                "workflow": str(args.workflow.resolve()),
                "readerNode": reader["id"],
                "samples": len(manifest["rows"]),
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    events_path = args.events or (out / "events.jsonl")
    emit, handle = _emit_factory(events_path, args.quiet)
    print(f"→ {len(manifest['rows'])} 个样本跑 {len(ir.get('nodes', []))} 个节点…", file=sys.stderr)
    try:
        runner.run(request, out, emit)
    finally:
        if handle is not None:
            handle.close()
    print(f"\n✔ 运行完成: {out}")
    print(f"  事件: {events_path}")
    print(f"  产物: {out / 'manifest.json'}")
    print(f"  本地报告: radstudio report {out} --open")
    print(f"  想在云端可视化/GPU/协作? {CLOUD_UTM}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from . import replay

    bundle = args.bundle.resolve()
    if not (bundle / "request.json").is_file():
        raise LocalDataError(
            f"{bundle} 里没有 request.json；复现包应包含 request.json 与 input-hashes.json",
            field="bundle",
        )
    args.out.mkdir(parents=True, exist_ok=True)
    replay.main(bundle, ["--dataset-dir", str(args.dataset_dir), "--output", str(args.out)])
    print(f"\n✔ 复跑完成: {args.out.resolve()}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report import build_report

    path = build_report(args.out)
    print(f"✔ 报告已生成: {path}")
    if args.open:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .local_ui import serve

    serve(args.workspace, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib

    checks: list[tuple[str, str, bool]] = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(("Python", sys.version.split()[0], py_ok))
    required = [
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("scikit-learn", "sklearn"),
        ("Pillow", "PIL"),
        ("pyradiomics", "radiomics"),
    ]
    missing: list[str] = []
    for label, module_name in required:
        try:
            module = importlib.import_module(module_name)
            checks.append((label, str(getattr(module, "__version__", "installed")), True))
        except Exception as exc:  # noqa: BLE001
            checks.append((label, f"缺失: {exc}", False))
            missing.append(label)
    print(f"RadStudio Local {_version()}")
    width = max(len(name) for name, _, _ in checks)
    for name, value, ok in checks:
        print(f"  {'✔' if ok else '✖'} {name:<{width}}  {value}")
    if missing:
        print(
            "\n环境不完整。在仓库内运行 `cd packages/python-sdk && uv sync`；"
            "或使用后续提供的预构建 Docker 镜像。",
            file=sys.stderr,
        )
        return 1
    print("\n✔ 本地环境可用。下一步：radstudio run <workflow.json> --data ./data")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="radstudio",
        description="RadStudio Local —— 本地可复现影像组学（科研用途，不用于临床诊断）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行工作流")
    _build_run_parser(run_parser)
    run_parser.set_defaults(func=cmd_run)

    replay_parser = subparsers.add_parser("replay", help="复现包重跑")
    _build_replay_parser(replay_parser)
    replay_parser.set_defaults(func=cmd_replay)

    report_parser = subparsers.add_parser("report", help="生成静态 HTML 报告")
    _build_report_parser(report_parser)
    report_parser.set_defaults(func=cmd_report)

    ui_parser = subparsers.add_parser("ui", help="启动本地 Web UI")
    _build_ui_parser(ui_parser)
    ui_parser.set_defaults(func=cmd_ui)

    doctor_parser = subparsers.add_parser("doctor", help="检查本地环境")
    _build_doctor_parser(doctor_parser)
    doctor_parser.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LocalDataError as exc:
        print(f"错误 [{exc.code}]: {exc}", file=sys.stderr)
        if exc.suggested_action:
            print(f"建议: {exc.suggested_action}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
