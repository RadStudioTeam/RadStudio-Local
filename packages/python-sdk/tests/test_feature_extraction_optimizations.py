"""特征提取长节点优化：多进程、断点续算、分片合并与进度上报。"""

import io

import httpx
import numpy as np

import radstudio.operators_builtin  # noqa: F401  # 导入即注册算子
from radstudio.operator import ArtifactStore, OperatorRequest, invoke


def _preprocessed(store, name, seed=0, size=8):
    rng = np.random.default_rng(seed)
    image = rng.uniform(10, 100, (size, size))
    mask = np.ones((size, size), dtype=bool)
    bins = (image // 25).astype(np.int32) + 1
    buffer = io.BytesIO()
    np.savez_compressed(buffer, image=image, mask=mask, discretized=bins)
    return store.write_bytes(name, buffer.getvalue(), "array")


def _samples(store, count):
    samples = []
    for i in range(count):
        samples.append(
            {
                "imageId": f"i{i}",
                "patientId": f"p{i}",
                "label": i % 2,
                "preprocessed": _preprocessed(store, f"sample_{i}.npz", seed=i),
            }
        )
    return store.write_json("samples.json", samples, "sample_collection")


def _request(inputs, parameters, node_id="feature"):
    return OperatorRequest(
        requestId="req-feature",
        runId="run-1",
        nodeId=node_id,
        operatorType="IBSI2DFeatureExtraction",
        parameters={
            "engine": "custom",
            "featureFamilies": ["first_order"],
            "imageTypes": ["original"],
            "binWidth": 25,
            **parameters,
        },
        inputs=inputs,
    )


def test_feature_extraction_multiprocessing_matches_single_process(tmp_path):
    single_store = ArtifactStore(tmp_path / "single")
    single = invoke(
        _request({"samples": _samples(single_store, 4)}, {"workers": 1}),
        single_store,
    )
    assert single.status == "completed", single.error
    expected = single_store.read_json(single.outputs["features"])

    pool_store = ArtifactStore(tmp_path / "pool")
    events = []
    pooled = invoke(
        _request({"samples": _samples(pool_store, 4)}, {"workers": 2, "checkpointEvery": 2}),
        pool_store,
        emit=lambda event, payload: events.append((event, payload)),
    )
    assert pooled.status == "completed", pooled.error
    actual = pool_store.read_json(pooled.outputs["features"])
    assert actual["featureNames"] == expected["featureNames"]
    assert [r["imageId"] for r in actual["records"]] == ["i0", "i1", "i2", "i3"]
    for want, got in zip(expected["records"], actual["records"]):
        assert want["values"] == got["values"]
    assert any(event == "node.progress" for event, _ in events)


def test_feature_extraction_resumes_from_checkpoint(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = _samples(store, 4)
    baseline = invoke(_request({"samples": samples_ref}, {"workers": 1}), store)
    assert baseline.status == "completed", baseline.error
    table = store.read_json(baseline.outputs["features"])
    names = table["featureNames"]
    records = table["records"]

    # 已完成样本 0/2 的断点记录；随后把这两个 NPZ 破坏，验证确实被跳过。
    checkpoint = {
        "featureNames": names,
        "records": [
            {"index": 0, "record": records[0]},
            {"index": 2, "record": records[2]},
        ],
    }
    for i in (0, 2):
        with open(store.resolve_path(f"sample_{i}.npz"), "wb") as fp:
            fp.write(b"not-an-npz")

    class _Response:
        status_code = 200

        def json(self):
            return checkpoint

    puts = []
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: _Response())
    monkeypatch.setattr(httpx, "put", lambda url, json=None, **kwargs: puts.append(json))
    request = _request({"samples": samples_ref}, {"workers": 1, "checkpointEvery": 2})
    request.transfer = {
        "checkpointGetUrl": "https://checkpoint.test/get",
        "checkpointPutUrl": "https://checkpoint.test/put",
    }
    result = invoke(request, store)
    assert result.status == "completed", result.error
    resumed = store.read_json(result.outputs["features"])
    assert [r["imageId"] for r in resumed["records"]] == ["i0", "i1", "i2", "i3"]
    assert resumed["records"][0] == records[0]
    assert resumed["records"][2] == records[2]
    assert puts, "checkpoint not written"
    saved = puts[-1]
    assert saved["featureNames"] == names
    assert [entry["index"] for entry in saved["records"]] == [0, 1, 2, 3]


def test_feature_extraction_merge_mode_rebuilds_outputs(tmp_path):
    store = ArtifactStore(tmp_path / "workspace")
    part0 = store.write_json(
        "part_0.json",
        {
            "featureNames": ["a", "b"],
            "records": [{"imageId": "i0", "patientId": "p0", "label": 0, "values": [1, 2]}],
        },
        "feature_table",
    )
    part1 = store.write_json(
        "part_1.json",
        {
            "featureNames": ["a", "b"],
            "records": [{"imageId": "i1", "patientId": "p1", "label": 1, "values": [3, 4]}],
        },
        "feature_table",
    )
    result = invoke(
        _request({"part0": part0, "part1": part1}, {"mergeParts": 2, "shardCount": 2}),
        store,
    )
    assert result.status == "completed", result.error
    merged = store.read_json(result.outputs["features"])
    assert merged["featureNames"] == ["a", "b"]
    assert [r["imageId"] for r in merged["records"]] == ["i0", "i1"]
    report = store.read_json(result.outputs["report"])
    assert report["shards"] == 2
    assert any(a.type == "feature_table_xlsx" for a in result.artifacts)
