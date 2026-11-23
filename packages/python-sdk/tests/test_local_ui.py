import csv
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

from radstudio.local_ui import Workspace, dag_svg, make_server


def make_data(root: Path) -> Path:
    data = root / "data"
    (data / "images").mkdir(parents=True)
    (data / "masks").mkdir()
    rng = np.random.default_rng(11)
    for index, name in enumerate(("case1", "case2")):
        Image.fromarray(rng.integers(0, 255, (8, 8), dtype=np.uint8)).save(data / "images" / f"{name}.png")
        mask = np.zeros((8, 8), dtype=np.uint8)
        mask[2:6, 2:6] = 255
        Image.fromarray(mask).save(data / "masks" / f"{name}.png")
    with (data / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_id", "label", "patient_id"])
        writer.writerows([["case1", "0", "p1"], ["case2", "1", "p2"]])
    return data


def minimal_workflow() -> dict:
    return {
        "name": "smoke",
        "schemaVersion": "2",
        "nodes": [
            {
                "id": "data_input",
                "type": "ReadImages",
                "name": "数据输入",
                "operatorVersion": "1.0.0",
                "parameters": {
                    "imageRole": "image",
                    "roiRole": "roi",
                    "requireRoi": True,
                    "labelField": "labelValue",
                    "positiveValue": "1",
                    "negativeValue": "0",
                },
            }
        ],
        "edges": [],
    }


def start_server(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace")
    server = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return workspace, server, base


def get(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return response.read().decode("utf-8")


def post(base: str, path: str, fields: dict[str, str]) -> str:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8")


def stop(workspace: Workspace, server) -> None:
    server.shutdown()
    server.server_close()
    workspace.shutdown()


def test_pages_and_scan(tmp_path):
    workspace, server, base = start_server(tmp_path)
    try:
        assert "数据" in get(base, "/")
        assert "只读 DAG" in get(base, "/workflows")
        assert "运行" in get(base, "/runs")
        assert "工作区" in get(base, "/settings")
        assert json.loads(get(base, "/healthz"))["status"] == "ok"
        data = make_data(tmp_path)
        html = post(base, "/scan", {"dataDir": str(data)})
        assert "扫描结果" in html
        assert "样本数" in html
    finally:
        stop(workspace, server)


def test_run_end_to_end_and_report(tmp_path):
    workspace, server, base = start_server(tmp_path)
    try:
        workspace.save_workflow("smoke.json", json.dumps(minimal_workflow()))
        data = make_data(tmp_path)
        request = urllib.request.Request(
            base + "/runs/start",
            data=urllib.parse.urlencode(
                {"workflow": "smoke.json", "dataDir": str(data), "runId": "ui-smoke"}
            ).encode("utf-8"),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            final = response.geturl()
        assert "/runs/ui-smoke" in final
        deadline = time.time() + 90
        status = {}
        while time.time() < deadline:
            status = json.loads(get(base, "/api/runs/ui-smoke"))
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.5)
        assert status.get("status") == "completed", status
        results = get(base, "/results/ui-smoke")
        assert "结果" in results
        report = get(base, "/report/ui-smoke")
        assert "RadStudio Local 运行报告" in report
        with pytest_raises_http(404):
            get(base, "/artifact/ui-smoke/../../etc/passwd")
    finally:
        stop(workspace, server)


class pytest_raises_http:
    def __init__(self, code: int):
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError("expected HTTPError")
        assert issubclass(exc_type, urllib.error.HTTPError)
        assert exc.code == self.code
        return True


def test_dag_svg_contains_nodes_and_edges():
    svg = dag_svg(minimal_workflow())
    assert "<svg" in svg
    assert "数据输入" in svg or "ReadImages" in svg
    assert "marker" in svg


def test_workflow_save_validates_json(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    try:
        try:
            workspace.save_workflow("bad.json", "{not json")
        except ValueError as exc:
            assert "JSON" in str(exc)
        else:
            raise AssertionError("expected ValueError")
        workspace.save_workflow("ok.json", json.dumps(minimal_workflow()))
        assert (workspace.workflows_dir / "ok.json").is_file()
    finally:
        workspace.shutdown()
