"""异步任务状态对象测试：进度/心跳写入、终态收敛、协作取消标记检测。"""

from __future__ import annotations

import json
import time

from radstudio import operator_serve


class _Response:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, puts: list, gets: list, cancel: bool = False):
        self.puts = puts
        self.gets = gets
        self.cancel = cancel

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put(self, url, content=None, headers=None):
        self.puts.append((url, json.loads(content)))
        return _Response()

    def get(self, url):
        self.gets.append(url)
        return _Response(200 if self.cancel else 404)


def _writer(monkeypatch, *, cancel: bool = False, heartbeat: int = 2):
    puts: list = []
    gets: list = []
    fake = _FakeClient(puts, gets, cancel=cancel)
    monkeypatch.setattr(operator_serve, "_transfer_client", lambda: fake)
    writer = operator_serve._StatusWriter(
        {
            "statusUrl": "mem://status",
            "cancelUrl": "mem://cancel",
            "heartbeatSeconds": heartbeat,
        }
    )
    return writer, puts, gets


def test_execute_payload_fetches_request_ref(monkeypatch):
    """超限请求体转存对象存储后，serve 按 requestRef 取回完整 payload。"""
    request = {
        "requestId": "r1",
        "runId": "run-1",
        "nodeId": "node-1",
        "operatorType": "__missing__",
        "operatorVersion": "0.0.0",
        "parameters": {},
        "inputs": {},
    }
    urls: list = []

    def fake_get(url):
        urls.append(url)
        return json.dumps({"request": request}).encode()

    monkeypatch.setattr(operator_serve, "_http_get", fake_get)
    result = operator_serve.execute_payload({"requestRef": "mem://request.json"})
    assert urls == ["mem://request.json"]
    assert result.status == "failed"
    assert result.error.code == "operator.version_unavailable"
    assert "__missing__@0.0.0" in result.error.message


def test_status_writer_progress_and_finish(monkeypatch):
    writer, puts, _ = _writer(monkeypatch)
    writer.start()
    writer.progress(42.5, "shard 1")
    writer.finish({"status": "completed", "outputs": {}})
    writer.stop()

    statuses = [payload["status"] for _, payload in puts]
    assert statuses[0] == "running"
    assert statuses[-1] == "completed"
    assert any(payload["progress"] == 42.5 for _, payload in puts)
    final = puts[-1][1]
    assert final["progress"] == 100.0
    assert final["result"]["status"] == "completed"


def test_status_writer_heartbeat_detects_cancel(monkeypatch):
    writer, _, gets = _writer(monkeypatch, cancel=True)
    writer.start()
    try:
        assert not writer.is_cancelled()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not writer.is_cancelled():
            time.sleep(0.1)
        assert writer.is_cancelled()
        assert gets, "cancel marker must be polled by heartbeat"
    finally:
        writer.stop()


def test_status_writer_disabled_without_url():
    writer = operator_serve._StatusWriter(None)
    assert not writer.enabled
    # 未配置预签名 URL 时所有写入都是无副作用的空操作。
    writer.start()
    writer.progress(10)
    writer.finish({"status": "completed"})
    writer.stop()
    assert not writer.is_cancelled()
