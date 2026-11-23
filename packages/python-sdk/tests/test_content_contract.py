import hashlib
import json
from pathlib import Path

import pytest

from radstudio.artifact_boundary import ArtifactBoundaryError, validate_inputs, validate_outputs
from radstudio.content_contract import ContentContractError, validate_content_bytes

CASES = json.loads(
    (
        Path(__file__).resolve().parents[2] / "operator-schema/tests/content-contract-cases.json"
    ).read_text(encoding="utf-8")
)


CASES += json.loads(
    (
        Path(__file__).resolve().parents[2] / "operator-schema/tests/split-content-cases.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["name"])
def test_shared_content_contract_cases(case):
    data = json.dumps(case["value"]).encode()
    if case["valid"]:
        assert (
            validate_content_bytes(data, case["physicalType"], case["contract"])["rules"]
            == "artifact-content/1"
        )
    else:
        with pytest.raises(ContentContractError) as error:
            validate_content_bytes(data, case["physicalType"], case["contract"])
        if "fieldPath" in case:
            assert error.value.details["fieldPath"] == case["fieldPath"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"featureNames":[],"featureNames":["x"],"records":[]}',
        b'{"featureNames":["x"],"records":[{"imageId":"i","patientId":"p","label":1,"values":[NaN]}]}',
    ],
)
def test_noncanonical_json_content_rejected(raw):
    with pytest.raises(ContentContractError):
        validate_content_bytes(
            raw, "feature_table", {"id": "radstudio/feature-table", "version": "1"}
        )


def test_content_failure_at_both_input_and_output_boundaries(tmp_path):
    data = json.dumps(CASES[2]["value"]).encode()
    (tmp_path / "features.json").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    ref = f"file:features.json#sha256={digest};type=feature_table"
    ports = [
        {
            "name": "features",
            "type": "feature_table",
            "required": True,
            "contract": {"id": "radstudio/feature-table", "version": "1"},
        }
    ]
    meta = [
        {
            "storagePath": "features.json",
            "type": "feature_table",
            "fileHash": digest,
            "fileSize": len(data),
        }
    ]
    with pytest.raises(ArtifactBoundaryError) as error:
        validate_inputs({"features": ref}, tmp_path, ports, verify_content=True)
    assert error.value.code == "operator.input_invalid"
    with pytest.raises(ArtifactBoundaryError) as error:
        validate_outputs({"features": ref}, meta, tmp_path, ports=ports, verify_content=True)
    assert error.value.code == "operator.output_invalid"
    validate_inputs(
        {"features": ref}, tmp_path, ports
    )  # Historical rule does not silently acquire a new validator.


@pytest.mark.parametrize("enabled", [True, False])
def test_runner_content_rule_is_frozen_and_prevents_publication(tmp_path, monkeypatch, enabled):
    import copy

    from radstudio import runner
    from radstudio.catalog_snapshot import capture_catalog_snapshot, catalog_digest
    from radstudio.operators_readers import ReadImagesOperator

    definition = copy.deepcopy(
        next(
            d
            for d in json.loads(runner.catalog_path().read_text(encoding="utf-8"))
            if d["type"] == "ReadImages" and d["version"] == "1.0.0"
        )
    )
    definition["outputs"] = [
        {
            "name": "features",
            "type": "feature_table",
            "required": True,
            "contract": {"id": "radstudio/feature-table", "version": "1"},
        }
    ]
    ir = {
        "schemaVersion": "2",
        "nodes": [
            {
                "id": "source",
                "type": "ReadImages",
                "operatorVersion": "1.0.0",
                "parameters": definition["defaults"],
            }
        ],
        "edges": [],
    }
    catalog = capture_catalog_snapshot(ir, [definition])
    if not enabled:
        del catalog["rules"]["contentContracts"]
        del catalog["rules"]["artifactExecution"]
        catalog["digest"] = catalog_digest(catalog)

    def invalid_output(self, ctx):
        return {"features": ctx.output_json("features.json", CASES[2]["value"], "feature_table")}

    monkeypatch.setattr(ReadImagesOperator, "run", invalid_output)
    events = []
    request = {"runId": "content-rule", "ir": ir, "operatorCatalogSnapshot": catalog}
    output = tmp_path / "output"
    if enabled:
        with pytest.raises(ArtifactBoundaryError) as error:
            runner.run(request, output, lambda kind, payload: events.append(kind))
        assert error.value.details["fieldPath"] == "/records/0/values"
        assert "artifact.created" not in events
        assert "node.completed" not in events
        assert not (output / "manifest.json").exists()
    else:
        runner.run(request, output, lambda kind, payload: events.append(kind))
        assert "node.completed" in events


def test_two_nodes_exchange_valid_explicit_content_contracts(tmp_path, monkeypatch):
    import copy

    from radstudio import runner
    from radstudio.catalog_snapshot import capture_catalog_snapshot
    from radstudio.operators_builtin import QualityControlOperator
    from radstudio.operators_readers import ReadImagesOperator

    definitions = json.loads(runner.catalog_path().read_text(encoding="utf-8"))
    chosen = [
        copy.deepcopy(next(d for d in definitions if d["type"] == kind and d["version"] == "1.0.0"))
        for kind in ("ReadImages", "QualityControl")
    ]
    features = {
        "name": "features",
        "type": "feature_table",
        "required": True,
        "contract": {"id": "radstudio/feature-table", "version": "1"},
    }
    predictions = {
        "name": "predictions",
        "type": "prediction_table",
        "required": True,
        "contract": {"id": "radstudio/prediction-table", "version": "1"},
    }
    chosen[0]["outputs"] = [features]
    chosen[1]["inputs"] = [features]
    chosen[1]["outputs"] = [predictions]

    def source(self, ctx):
        return {"features": ctx.output_json("features.json", CASES[0]["value"], "feature_table")}

    def consumer(self, ctx):
        assert ctx.input_json("features")["featureNames"] == ["a", "b"]
        return {
            "predictions": ctx.output_json(
                "predictions.json", CASES[9]["value"], "prediction_table"
            )
        }

    monkeypatch.setattr(ReadImagesOperator, "run", source)
    monkeypatch.setattr(QualityControlOperator, "run", consumer)
    nodes = [
        {
            "id": nid,
            "type": definition["type"],
            "operatorVersion": "1.0.0",
            "parameters": definition["defaults"],
        }
        for nid, definition in zip(("source", "consumer"), chosen)
    ]
    ir = {
        "schemaVersion": "2",
        "nodes": nodes,
        "edges": [
            {
                "source": "source",
                "target": "consumer",
                "sourceOutput": "features",
                "targetInput": "features",
            }
        ],
    }
    request = {
        "runId": "content-e2e",
        "ir": ir,
        "operatorCatalogSnapshot": capture_catalog_snapshot(ir, chosen),
    }
    events = []
    results = runner.run(request, tmp_path / "output", lambda kind, payload: events.append(kind))
    assert set(results) == {"source", "consumer"}
    assert events.count("node.completed") == 2
    assert events.count("artifact.created") == 2
    manifest = json.loads((tmp_path / "output/manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 2


def test_unknown_content_rule_is_not_accepted():
    from radstudio import runner
    from radstudio.catalog_snapshot import (
        CatalogSnapshotError,
        capture_catalog_snapshot,
        catalog_digest,
        validate_catalog_snapshot,
    )

    definition = next(
        d
        for d in json.loads(runner.catalog_path().read_text(encoding="utf-8"))
        if d["type"] == "ReadImages" and d["version"] == "1.0.0"
    )
    ir = {
        "schemaVersion": "2",
        "nodes": [
            {
                "id": "source",
                "type": "ReadImages",
                "operatorVersion": "1.0.0",
                "parameters": definition["defaults"],
            }
        ],
        "edges": [],
    }
    snapshot = capture_catalog_snapshot(ir, [definition])
    snapshot["rules"]["contentContracts"] = 2
    snapshot["digest"] = catalog_digest(snapshot)
    with pytest.raises(CatalogSnapshotError):
        validate_catalog_snapshot(ir, snapshot)


def test_scientific_small_feature_value_is_not_parameter_budget():
    data = b'{"featureNames":["tiny"],"records":[{"imageId":"i","patientId":"p","label":1,"values":[1e-200]}]}'
    assert (
        validate_content_bytes(
            data, "feature_table", {"id": "radstudio/feature-table", "version": "1"}
        )["records"]
        == 1
    )


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).resolve().parents[2] / "operator-schema/tests/content-json-cases.json"
        ).read_text(encoding="utf-8")
    ),
    ids=lambda item: item["name"],
)
def test_shared_raw_content_cases(case):
    if case["valid"]:
        validate_content_bytes(case["raw"].encode(), case["physicalType"], case["contract"])
    else:
        with pytest.raises(ContentContractError):
            validate_content_bytes(case["raw"].encode(), case["physicalType"], case["contract"])


@pytest.mark.parametrize(
    "contract", [None, {}, {"id": "radstudio/feature-table", "version": "1", "dimension": None}]
)
def test_invalid_content_declaration_is_structured_error(contract):
    with pytest.raises(ContentContractError):
        validate_content_bytes(b"{}", "feature_table", contract)


SOURCE_CASES = json.loads(
    (
        Path(__file__).resolve().parents[2] / "operator-schema/tests/split-source-cases.json"
    ).read_text(encoding="utf-8")
)


SAMPLE_CONTRACT = {"id": "radstudio/sample-collection", "version": "1"}


@pytest.mark.parametrize("case", SOURCE_CASES, ids=lambda item: item["name"])
def test_shared_split_source_binding(case, tmp_path):
    from radstudio.content_contract import validate_content_file
    from radstudio.json_contract import canonical_manifest_json

    if not case["missingSource"]:
        (tmp_path / "features.json").write_bytes(canonical_manifest_json(case["table"]))
    plan = tmp_path / "split.json"
    plan.write_bytes(canonical_manifest_json(case["plan"]))
    if case["valid"]:
        validate_content_file(plan, "split_plan", {"id": "radstudio/split-plan", "version": "1"})
    else:
        with pytest.raises(ContentContractError) as error:
            validate_content_file(
                plan, "split_plan", {"id": "radstudio/split-plan", "version": "1"}
            )
        assert error.value.details["fieldPath"] == case["fieldPath"]


# ---------------------------------------------------------------------------
# 样本生产者契约声明版本（B24）：radstudio/sample-collection@1 绑在输出端口上
# ---------------------------------------------------------------------------

SAMPLE_PRODUCER_TYPES = (
    "ImageFormatConversion",
    "ImageFilterBank",
    "QualityControl",
    "ROIValidation",
    "Preprocessing2D",
)


def packaged_catalog():
    """serve 与 validate_graph 实际读取的那份目录副本，不是 packages 下的来源文件。"""
    from radstudio.runner import catalog_path

    return json.loads(Path(str(catalog_path())).read_text(encoding="utf-8"))


def test_packaged_catalog_binds_sample_outputs_only():
    """五个生产者各登记一个 1.1.0 声明版本，只绑输出，样本输入一个都不绑。"""
    rows = packaged_catalog()
    bound_outputs = []
    bound_inputs = []
    free_input_consumers = set()
    for row in rows:
        for port in row["outputs"]:
            if port["type"] == "sample_collection" and port.get("contract"):
                bound_outputs.append((row["type"], row["version"]))
                assert port["contract"] == SAMPLE_CONTRACT, row["type"]
        for port in row["inputs"]:
            if port["type"] == "sample_collection":
                if port.get("contract"):
                    bound_inputs.append((row["type"], row["version"]))
                else:
                    free_input_consumers.add(row["type"])
    assert sorted(bound_outputs) == sorted((op_type, "1.1.0") for op_type in SAMPLE_PRODUCER_TYPES)
    # 1.0.0 行端口进语义摘要、就地回填会被不可变守卫拒绝；而绑定输入要求上游先声明，
    # 平台又没有样本适配器，历史产出端会当场无解——所以回填只能从产出端起步。
    assert bound_inputs == []
    # 消费端清单：样本进入这些算子时仍不核对契约，是下一批要收口的范围。
    # 数据准备与分割推理四个新算子同样保持不绑定：ReadImages 没有任何契约、
    # 生产者只在 1.1.0 声明，绑样本输入会让默认入口当场无解。
    assert free_input_consumers == {
        "ImageFormatConversion",
        "ImageFilterBank",
        "QualityControl",
        "ROIValidation",
        "Preprocessing2D",
        "IBSI2DFeatureExtraction",
        "DLCNNTraining",
        "DLCNNInference",
        "FixedSplit",
        "GradCAM",
        "SegUNetTraining",
        "SegUNetInference",
        "SegMaskApply",
        "SegmentationEvaluation",
        "PerceptualHashDedup",
        "ROICrop",
        "MedSAMInference",
        "CNNFeatureExtraction",
    }
    for op_type in SAMPLE_PRODUCER_TYPES:
        published = next(
            row for row in rows if row["type"] == op_type and row["version"] == "1.0.0"
        )
        assert not any(
            port.get("contract") for port in published["inputs"] + published["outputs"]
        ), op_type


@pytest.mark.parametrize("op_type", SAMPLE_PRODUCER_TYPES)
def test_contract_version_reuses_the_published_implementation(op_type):
    """1.1.0 是声明版本而不是行为版本：run 与 1.0.0 是同一个函数对象。"""
    from radstudio import operators_builtin, operators_radiomics  # noqa: F401
    from radstudio.operator import OPERATOR_REGISTRY, resolve_operator

    declared = resolve_operator(op_type, "1.1.0")
    published = resolve_operator(op_type, "1.0.0")
    assert issubclass(declared, published)
    assert declared.run is published.run
    assert declared.operator_version == "1.1.0"
    # 未指定版本的历史直连会走到最新注册版本，这条边界由断言钉住而不是留给推测。
    assert OPERATOR_REGISTRY[op_type] is declared


# ---------------------------------------------------------------------------
# 表格模型契约声明版本（B25）：四个第一方分类器的 model 输出绑定
# radstudio/model@1、predictions 绑定 radstudio/prediction-table@1。
# 血缘信封（task/dimension/family/preprocess/fit/weights）是新增产出，
# 所以这一族与 B24 的纯声明版本不同——run 不再是 1.0.0 的函数对象。
# ---------------------------------------------------------------------------

TABLE_MODEL_TYPES = (
    "LogisticRegression",
    "RandomForestClassifier",
    "SVMClassifier",
    "GradientBoostingClassifier",
)
MODEL_FAMILIES = {
    "LogisticRegression": "logistic_regression",
    "RandomForestClassifier": "random_forest",
    "SVMClassifier": "svm",
    "GradientBoostingClassifier": "gradient_boosting",
}
FEATURE_CONTRACT = {
    "id": "radstudio/feature-table",
    "version": "1",
    "task": "binary_classification",
    "dimension": "tabular",
}
PREDICTION_CONTRACT = {
    "id": "radstudio/prediction-table",
    "version": "1",
    "task": "binary_classification",
    "dimension": "tabular",
}
MODEL_ENVELOPE_KEYS = {"task", "dimension", "family", "preprocess", "fit", "weights"}
LEGACY_LOGISTIC_MODEL_KEYS = {
    "evaluation",
    "featureNames",
    "foldCount",
    "folds",
    "kind",
    "pipelineArtifact",
    "seed",
}
# 消费端 model 输入的绑定：与 Go 侧 consumerModelBinding() 同一份声明，两侧不许各写一遍。
# 刻意不含 modelFamily——报告与外部应用都要接受任一分类器，写上家族就成了「只认逻辑回归的报告」。
CONSUMER_MODEL_BINDING = {
    "id": "radstudio/model",
    "version": "2",
    "task": "binary_classification",
    "dimension": "tabular",
}
# 影像链另立契约版本：@2 的 dimension 只有 tabular，就地放宽会让表格消费端也接受体数据模型。
DL_MODEL_CONTRACT = {
    "id": "radstudio/model",
    "version": "3",
    "task": "binary_classification",
    "dimension": "2d",
}
BOUND_MODEL_CONSUMERS = [("ModelApply", "1.1.0"), ("Report", "1.2.0")]
BOUND_DL_MODEL_CONSUMERS = [("DLCNNInference", "1.2.0")]
UNBOUND_MODEL_CONSUMERS = [
    ("ModelApply", "1.0.0"),
    ("Report", "1.0.0"),
    ("Report", "1.1.0"),
    ("DLCNNInference", "1.0.0"),
    ("DLCNNInference", "1.1.0"),
    ("DLCNNInference", "1.3.0"),
    ("GradCAM", "1.0.0"),
    # 新算子读 DLCNNTraining 的 checkpoint 出 penultimate 特征，但画板默认仍钉 1.0.0；
    # 1.0.0 的 model 产出没有 @3 信封，绑了 @3 会让默认「训练 → 特征」连边当场无解。
    ("CNNFeatureExtraction", "1.0.0"),
]


def model_contract(operator_type):
    return {
        "id": "radstudio/model",
        "version": "1",
        "task": "binary_classification",
        "dimension": "tabular",
        "modelFamily": MODEL_FAMILIES[operator_type],
    }


def model_contract_for(operator_type, version):
    """表格分类器 1.2.0 起产出折内血缘信封绑 @2；DL 1.1.0 产出影像血缘绑 @3。"""
    if operator_type == "DLCNNTraining":
        assert version == "1.1.0"
        return DL_MODEL_CONTRACT
    return (
        model_contract(operator_type) if version == "1.1.0" else model_fold_contract(operator_type)
    )


def tabular_feature_table(count=60):
    """合成特征表：signal 随标签偏移，其余三列是噪声、常数列与重复列。"""
    import numpy as np

    rng = np.random.default_rng(42)
    return {
        "featureNames": ["signal", "noise", "constant", "duplicate"],
        "records": [
            {
                "imageId": f"i{index}",
                "patientId": f"p{index // 2}",
                "label": (index // 2) % 2,
                "values": [
                    float((index // 2) % 2 + rng.normal()),
                    float(rng.normal()),
                    1,
                    float(index % 3),
                ],
            }
            for index in range(count)
        ],
    }


def classifier_chain(
    operator_type,
    classifier_version,
    *,
    split_version="1.2.0",
    bind=None,
    enabled=True,
    apply_model=False,
):
    """ReadImages→DataSplit→FeatureFilter→分类器 的四节点链。

    ReadImages 的真实产出是 samples，这里用 monkeypatch 让它直出 feature_table，
    只为把上游端口声明成契约；被测对象始终是分类器的 model/predictions 输出。
    apply_model=True 时再接一个 ModelApply，用来测量真实消费端读到的是什么。
    """
    import copy

    from radstudio import runner
    from radstudio.catalog_snapshot import capture_catalog_snapshot, catalog_digest

    definitions = json.loads(runner.catalog_path().read_text(encoding="utf-8"))

    def row(kind, version):
        return copy.deepcopy(
            next(
                item for item in definitions if item["type"] == kind and item["version"] == version
            )
        )

    rows = [
        row("ReadImages", "1.0.0"),
        row("DataSplit", split_version),
        row("FeatureFilter", "1.1.0"),
        row(operator_type, classifier_version),
    ]
    rows[0]["outputs"] = [
        {"name": "features", "type": "feature_table", "contract": FEATURE_CONTRACT}
    ]
    node_ids = ["source", "split", "filter", "clf"]
    if apply_model:
        rows.append(row("ModelApply", "1.0.0"))
        node_ids.append("apply")
    if bind is not None:
        bind(rows, operator_type)
    parameters = []
    for definition in rows:
        value = dict(definition["defaults"])
        if definition["type"] == "ReadImages":
            value["dataCollectionId"] = "col-1"
            value["readerConfigId"] = "cfg-1"
        parameters.append(value)
    edges = [
        {
            "source": "source",
            "target": "split",
            "sourceOutput": "features",
            "targetInput": "features",
        },
        {
            "source": "source",
            "target": "clf",
            "sourceOutput": "features",
            "targetInput": "features",
        },
        {"source": "split", "target": "clf", "sourceOutput": "split", "targetInput": "split"},
        {
            "source": "filter",
            "target": "clf",
            "sourceOutput": "strategy",
            "targetInput": "strategy",
        },
    ]
    if apply_model:
        edges += [
            {"source": "clf", "target": "apply", "sourceOutput": "model", "targetInput": "model"},
            {
                "source": "source",
                "target": "apply",
                "sourceOutput": "features",
                "targetInput": "features",
            },
        ]
    ir = {
        "schemaVersion": "2",
        "nodes": [
            {
                "id": nid,
                "type": definition["type"],
                "operatorVersion": definition["version"],
                "parameters": value,
            }
            for nid, definition, value in zip(node_ids, rows, parameters)
        ],
        "edges": edges,
    }
    catalog = capture_catalog_snapshot(ir, rows)
    if not enabled:
        del catalog["rules"]["contentContracts"]
        del catalog["rules"]["artifactExecution"]
        catalog["digest"] = catalog_digest(catalog)
    return {
        "runId": "model-lineage",
        "ir": ir,
        "operatorCatalogSnapshot": catalog,
    }


def bind_published_model_ports(rows, operator_type):
    """把 1.0.0 分类器的产出端口按 1.1.0 的声明绑上契约（回填实验）。"""
    rows[3]["outputs"] = [
        {"name": "model", "type": "model", "contract": model_contract(operator_type)},
        {"name": "predictions", "type": "prediction_table", "contract": PREDICTION_CONTRACT},
    ]


def attach_reader_manifest(request, root):
    """给 monkeypatch 的读取节点补一份等价 manifest：执行参数来自 manifest.config。"""
    reader = next((n for n in request["ir"]["nodes"] if n["type"] == "ReadImages"), None)
    if reader is None:
        return
    binding_keys = {"dataCollectionId", "readerConfigId", "dataResourceBinding"}
    config = {k: v for k, v in reader["parameters"].items() if k not in binding_keys}
    manifest_path = root.parent / "reader-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schemaVersion": "1",
                "reader": {"key": "read-images", "version": "1.0.0", "implVersion": "1.0.0"},
                "collection": {"id": "col-1", "revision": 1},
                "config": config,
                "files": {},
                "rows": [],
            }
        ),
        encoding="utf-8",
    )
    request.setdefault("snapshotInputs", {})[reader["id"]] = {"manifestPath": str(manifest_path)}


def run_chain(root, request, monkeypatch, events=None):
    from radstudio import runner
    from radstudio.operators_readers import ReadImagesOperator

    table = tabular_feature_table()

    def source(self, ctx):
        return {"features": ctx.output_json("features.json", table, "feature_table")}

    monkeypatch.setattr(ReadImagesOperator, "run", source)
    attach_reader_manifest(request, root)
    collected = [] if events is None else events
    results = runner.run(request, root, lambda kind, payload: collected.append(kind))
    return collected, results, table


def read_json(root, relative):
    return json.loads((root / relative).read_text(encoding="utf-8"))


def test_packaged_catalog_binds_model_lineage_on_declared_rows():
    """四个分类器各有 1.1.0（全集血缘）与 1.2.0（折内血缘）两个声明版本，绑表格 @1/@2 产出；
    DL 训练 1.1.0 绑影像 @3 产出，并给 split 输入绑源绑定划分。
    model 输入只在已登记的消费端新版本上绑（表格 @2 / DL @3），已发布行一律留空。"""
    rows = packaged_catalog()
    bound_model = []
    bound_predictions = []
    unbound_model_outputs = []
    model_inputs = []
    split_contracts = []
    for row in rows:
        for port in row["outputs"]:
            if port["type"] == "model":
                if port.get("contract"):
                    bound_model.append((row["type"], row["version"]))
                    assert port["contract"] == model_contract_for(row["type"], row["version"]), row
                else:
                    unbound_model_outputs.append((row["type"], row["version"]))
            elif port["type"] == "prediction_table" and port.get("contract"):
                bound_predictions.append((row["type"], row["version"]))
                assert port["contract"] == PREDICTION_CONTRACT, row["type"]
        for port in row["inputs"]:
            if port["type"] == "model":
                model_inputs.append((row["type"], row["version"], port.get("contract")))
            if port["type"] == "split_plan" and port.get("contract"):
                split_contracts.append((row["type"], row["version"], port["contract"]))
    declared_rows = [
        (op_type, version) for version in ("1.1.0", "1.2.0") for op_type in TABLE_MODEL_TYPES
    ]
    assert sorted(bound_model) == sorted(declared_rows + [("DLCNNTraining", "1.1.0")])
    assert sorted(bound_predictions) == sorted(declared_rows)
    # 没有契约的 model 产出只剩各类型的 1.0.0 发布行（端口进语义摘要，就地回填
    # 会被不可变守卫拒绝）。DL 1.1.0 虽然也绑契约，但绑的是影像 @3，不出现在表格断言里。
    assert sorted(unbound_model_outputs) == sorted(
        [("DLCNNTraining", "1.0.0"), ("DLCNNTraining", "1.2.0")]
        + [(op_type, "1.0.0") for op_type in TABLE_MODEL_TYPES]
    )
    # 消费端清单：读 model 的是这八行。清单逐条固化，绑与不绑都按行点名——
    # 「该绑谁」凭印象决定过一次，代价是把整条深度学习链判成无解。
    assert sorted({(op, version) for op, version, _ in model_inputs}) == sorted(
        BOUND_MODEL_CONSUMERS + BOUND_DL_MODEL_CONSUMERS + UNBOUND_MODEL_CONSUMERS
    )
    bound_inputs = {(op, version): contract for op, version, contract in model_inputs if contract}
    assert sorted(bound_inputs) == sorted(BOUND_MODEL_CONSUMERS + BOUND_DL_MODEL_CONSUMERS)
    for key, contract in bound_inputs.items():
        expected = DL_MODEL_CONTRACT if key in BOUND_DL_MODEL_CONSUMERS else CONSUMER_MODEL_BINDING
        assert contract == expected, key
    # split 输入只有 DL 训练 1.1.0 绑了源绑定划分：@3 信封的 fit.features 取自划分的 source，
    # 未携带来源特征表的 1.0.0 / 1.1.0 划分无从核对影像样本的来源。
    assert split_contracts == [
        (
            "DLCNNTraining",
            "1.1.0",
            {
                "id": "radstudio/split-plan",
                "version": "1",
                "task": "binary_classification",
                "dimension": "tabular",
            },
        )
    ]
    # 未绑定的那五行是历史工作流的逃生通道：钉回旧版本仍能建图。
    for op, version, contract in model_inputs:
        if (op, version) not in BOUND_MODEL_CONSUMERS + BOUND_DL_MODEL_CONSUMERS:
            assert contract is None, (op, version)
    for op_type in TABLE_MODEL_TYPES:
        published = next(
            row for row in rows if row["type"] == op_type and row["version"] == "1.0.0"
        )
        assert not any(port.get("contract") for port in published["inputs"] + published["outputs"])


@pytest.mark.parametrize("operator_type", TABLE_MODEL_TYPES)
def test_declared_version_publishes_verifiable_lineage(tmp_path, monkeypatch, operator_type):
    from radstudio.content_contract import validate_content_file

    events, results, table = run_chain(
        tmp_path, classifier_chain(operator_type, "1.1.0"), monkeypatch
    )
    assert events.count("node.completed") == 4
    model = read_json(tmp_path, results["clf"]["model"]["path"])
    assert model["task"] == "binary_classification"
    assert model["dimension"] == "tabular"
    assert model["family"] == MODEL_FAMILIES[operator_type]
    assert model["preprocess"] == ["missing", "impute", "filter", "scale"]

    features_bytes = (tmp_path / "source/features.json").read_bytes()
    split_bytes = (tmp_path / "split/split.json").read_bytes()
    features_ref = (
        f"file:source/features.json#sha256={hashlib.sha256(features_bytes).hexdigest()}"
        ";type=feature_table"
    )
    split_ref = (
        f"file:split/split.json#sha256={hashlib.sha256(split_bytes).hexdigest()};type=split_plan"
    )
    assert model["fit"]["features"] == features_ref
    assert model["fit"]["split"] == split_ref
    # 血缘与折计划互相对齐：split 的 source 就是模型声称消费的那张特征表。
    assert read_json(tmp_path, "split/split.json")["source"] == features_ref

    plan = read_json(tmp_path, "split/split.json")
    trained = sorted({sid for fold in plan["folds"] for sid in fold["trainIds"]})
    assert model["fit"]["trainedImageIds"] == trained
    assert model["fit"]["trainedCount"] == len(trained)
    assert set(trained) <= {row["imageId"] for row in table["records"]}

    weights = model["weights"]
    payload = (tmp_path / weights["path"]).read_bytes()
    assert weights["sha256"] == hashlib.sha256(payload).hexdigest()
    assert weights["sizeBytes"] == len(payload)
    assert model["pipelineArtifact"] == (
        f"file:{weights['path']}#sha256={weights['sha256']};type=model"
    )

    # 换一份载荷就通不过：契约不是事后抄录，而是能独立复核模型来源。
    summary = validate_content_file(
        tmp_path / results["clf"]["model"]["path"],
        "model",
        model_contract(operator_type),
        reference_roots=[tmp_path],
    )
    assert summary["rules"] == "artifact-content/1"
    assert summary["records"] == len(trained)
    predictions = read_json(tmp_path, results["clf"]["predictions"]["path"])
    assert validate_content_file(
        tmp_path / results["clf"]["predictions"]["path"],
        "prediction_table",
        PREDICTION_CONTRACT,
        reference_roots=[tmp_path],
    )["records"] == len(predictions["records"])


def test_backfilling_model_contract_on_published_run_refuses(tmp_path, monkeypatch):
    """1.0.0 的产物字节没有信封；就地绑端口只会让产出当场不可发布，不会补出字段。"""
    from radstudio.artifact_boundary import ArtifactBoundaryError

    events = []
    with pytest.raises(ArtifactBoundaryError) as error:
        run_chain(
            tmp_path,
            classifier_chain("LogisticRegression", "1.0.0", bind=bind_published_model_ports),
            monkeypatch,
            events,
        )
    assert error.value.code == "operator.output_invalid"
    assert error.value.details["fieldPath"] == "/task"
    assert error.value.details["contract"] == "radstudio/model@1"
    assert events[-2:] == ["node.started", "node.failed"]
    assert events.count("artifact.created") == 3
    manifest = read_json(tmp_path, "manifest.json")
    assert [entry["nodeId"] for entry in manifest] == ["source", "split", "filter"]
    # 未发布的节点工作区仍留着被拒绝的字节——注册与落盘是两回事，这里如实钉住。
    assert sorted(path.name for path in (tmp_path / "clf").iterdir()) == [
        "model.json",
        "pipelines.joblib",
        "predictions.json",
    ]
    # 关掉规则的历史运行照旧完成，说明改判来自绑定而不是实现回归。
    relaxed, relaxed_results, _ = run_chain(
        tmp_path / "legacy",
        classifier_chain(
            "LogisticRegression", "1.0.0", bind=bind_published_model_ports, enabled=False
        ),
        monkeypatch,
    )
    assert relaxed.count("node.completed") == 4
    assert set(read_json(tmp_path / "legacy", relaxed_results["clf"]["model"]["path"])) == (
        LEGACY_LOGISTIC_MODEL_KEYS
    )


@pytest.mark.parametrize("classifier_version", ["1.1.0", "1.2.0"])
def test_envelope_refuses_split_plan_without_source(tmp_path, monkeypatch, classifier_version):
    """血缘要指向带 source 的 split 计划；1.0.0 的 DataSplit 产出没有这一条，两个声明版本都必须拒。"""
    from radstudio.artifact_boundary import ArtifactBoundaryError

    with pytest.raises(ArtifactBoundaryError) as error:
        run_chain(
            tmp_path,
            classifier_chain("LogisticRegression", classifier_version, split_version="1.0.0"),
            monkeypatch,
        )
    assert error.value.code == "operator.output_invalid"
    assert error.value.details["contract"] == "radstudio/split-plan@1"
    # 拒绝落在 split-plan@1 自身校验上（缺 source），不是血缘比对的字段路径。
    assert error.value.details["fieldPath"] == ""


def test_envelope_is_the_only_difference_from_published_bytes(tmp_path, monkeypatch):
    """声明版本只加字段：预测逐字节不变，模型去掉信封后与 1.0.0 完全相同。"""
    from radstudio import runner
    from radstudio.operators_readers import ReadImagesOperator

    table = tabular_feature_table()

    def source(self, ctx):
        return {"features": ctx.output_json("features.json", table, "feature_table")}

    monkeypatch.setattr(ReadImagesOperator, "run", source)
    payloads = {}
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        root = tmp_path / version
        request = classifier_chain("LogisticRegression", version)
        attach_reader_manifest(request, root)
        results = runner.run(
            request,
            root,
            lambda kind, payload: None,
        )
        payloads[version] = (
            (root / results["clf"]["predictions"]["path"]).read_bytes(),
            read_json(root, results["clf"]["model"]["path"]),
        )
    published_predictions, published_model = payloads["1.0.0"]
    assert set(published_model) == LEGACY_LOGISTIC_MODEL_KEYS
    for version in ("1.1.0", "1.2.0"):
        declared_predictions, declared_model = payloads[version]
        assert published_predictions == declared_predictions, version
        stripped = {
            key: value for key, value in declared_model.items() if key not in MODEL_ENVELOPE_KEYS
        }
        assert stripped == published_model, version


@pytest.mark.parametrize(
    "breakage",
    ["features", "weights"],
)
def test_tampering_lineage_breaks_a_recheck(tmp_path, monkeypatch, breakage):
    """复核模型时血缘与权重载荷都要重算：改一处字节就拒，信封不是装饰。"""
    from radstudio.content_contract import ContentContractError, validate_content_file

    _, results, _ = run_chain(
        tmp_path, classifier_chain("LogisticRegression", "1.1.0"), monkeypatch
    )
    if breakage == "features":
        (tmp_path / "source/features.json").write_bytes(b'{"featureNames":[],"records":[]}')
        field_path = "/fit/features"
    else:
        (tmp_path / "clf/pipelines.joblib").write_bytes(b"not-a-pipeline")
        field_path = "/weights/sha256"
    with pytest.raises(ContentContractError) as error:
        validate_content_file(
            tmp_path / results["clf"]["model"]["path"],
            "model",
            model_contract("LogisticRegression"),
            reference_roots=[tmp_path],
        )
    assert error.value.details["fieldPath"] == field_path


@pytest.mark.parametrize("operator_type", TABLE_MODEL_TYPES)
def test_declared_version_changes_the_run_implementation(operator_type):
    """与 B24 的纯声明版本相反：这一族的 run 必须是新函数对象，且继承 1.0.0。"""
    from radstudio import operators_builtin, operators_radiomics  # noqa: F401
    from radstudio.operator import OPERATOR_REGISTRY, resolve_operator

    declared = resolve_operator(operator_type, "1.1.0")
    published = resolve_operator(operator_type, "1.0.0")
    assert issubclass(declared, published)
    assert declared.run is not published.run
    assert declared.operator_version == "1.1.0"
    assert OPERATOR_REGISTRY[operator_type] is fold_lineage_classifier(operator_type)


def fold_lineage_classifier(operator_type):
    """注册表按「后注册者优先」指向最新版本：折内血缘 1.2.0 才是当前直连落点。"""
    from radstudio import operators_builtin, operators_radiomics  # noqa: F401
    from radstudio.operator import resolve_operator

    return resolve_operator(operator_type, "1.2.0")


# ---------------------------------------------------------------------------
# radstudio/model@2：折内血缘。分类器 1.2.0 不再笼统声明「这些样本参与过拟合」，
# 而是逐折各自交代训练集与自己的权重文件；想按全集蒙过去就通不过。
# ---------------------------------------------------------------------------


def model_fold_contract(operator_type):
    return {
        "id": "radstudio/model",
        "version": "2",
        "task": "binary_classification",
        "dimension": "tabular",
        "modelFamily": MODEL_FAMILIES[operator_type],
    }


@pytest.mark.parametrize("operator_type", TABLE_MODEL_TYPES)
def test_fold_version_publishes_per_fold_lineage(tmp_path, monkeypatch, operator_type):
    """每一折的训练集必须与划分方案的该折训练集逐字对齐，权重各自成文件。"""
    from radstudio.content_contract import validate_content_file

    events, results, _ = run_chain(tmp_path, classifier_chain(operator_type, "1.2.0"), monkeypatch)
    assert events.count("node.completed") == 4
    model = read_json(tmp_path, results["clf"]["model"]["path"])
    plan = read_json(tmp_path, "split/split.json")
    assert sorted(model["fit"]) == ["features", "folds", "split"]
    assert len(model["fit"]["folds"]) == len(plan["folds"])

    ensemble = model["weights"]
    ensemble_digest = ensemble["sha256"]
    digests = set()
    paths = set()
    for declared, fold in zip(
        sorted(model["fit"]["folds"], key=lambda item: item["fold"]),
        sorted(plan["folds"], key=lambda item: item["fold"]),
    ):
        assert declared["fold"] == fold["fold"]
        assert declared["trainedImageIds"] == sorted(fold["trainIds"])
        assert declared["trainedCount"] == len(fold["trainIds"])
        weights = declared["weights"]
        assert weights["path"] != ensemble["path"]
        assert weights["path"] not in paths
        paths.add(weights["path"])
        payload = (tmp_path / weights["path"]).read_bytes()
        assert weights["sha256"] == hashlib.sha256(payload).hexdigest()
        assert weights["sizeBytes"] == len(payload)
        assert weights["sha256"] != ensemble_digest
        digests.add(weights["sha256"])
    assert len(digests) == len(plan["folds"])

    # 打包后的集成模型仍是同一份载荷：推理端 1.0.0 不需要跟着改版。
    assert model["pipelineArtifact"] == (
        f"file:{ensemble['path']}#sha256={ensemble['sha256']};type=model"
    )

    summary = validate_content_file(
        tmp_path / results["clf"]["model"]["path"],
        "model",
        model_fold_contract(operator_type),
        reference_roots=[tmp_path],
    )
    assert summary["rules"] == "artifact-content/1"
    assert summary["contract"] == "radstudio/model@2"
    # 折内契约的证据量是「逐折拟合数之和」，而不是并集样本数。
    assert summary["records"] == sum(item["trainedCount"] for item in model["fit"]["folds"])


def test_union_contract_refuses_the_fold_envelope(tmp_path, monkeypatch):
    """@1 只认一份全集声明：拿到折内信封必须当场拒绝，版本升级才是承重的。"""
    from radstudio.content_contract import ContentContractError, validate_content_file

    _, results, _ = run_chain(
        tmp_path, classifier_chain("LogisticRegression", "1.2.0"), monkeypatch
    )
    with pytest.raises(ContentContractError) as error:
        validate_content_file(
            tmp_path / results["clf"]["model"]["path"],
            "model",
            model_contract("LogisticRegression"),
            reference_roots=[tmp_path],
        )
    assert error.value.details["fieldPath"] == "/fit/trainedImageIds"
    assert error.value.details["contract"] == "radstudio/model@1"


def test_fold_weights_tampering_breaks_a_recheck(tmp_path, monkeypatch):
    """改某一折的权重字节，复核就要落在这一折上，而不是整份模型。"""
    from radstudio.content_contract import ContentContractError, validate_content_file

    _, results, _ = run_chain(
        tmp_path, classifier_chain("LogisticRegression", "1.2.0"), monkeypatch
    )
    model = read_json(tmp_path, results["clf"]["model"]["path"])
    victim = model["fit"]["folds"][1]
    (tmp_path / victim["weights"]["path"]).write_bytes(b"not-a-pipeline")
    with pytest.raises(ContentContractError) as error:
        validate_content_file(
            tmp_path / results["clf"]["model"]["path"],
            "model",
            model_fold_contract("LogisticRegression"),
            reference_roots=[tmp_path],
        )
    assert error.value.details["fieldPath"] == "/fit/folds/1/weights/sha256"


def test_both_lineage_forms_feed_the_same_consumer_run(tmp_path, monkeypatch):
    """消费端只读共用信封：同一张特征表接 1.1.0 或 1.2.0 的模型，预测逐字节相同。"""
    observed = {}
    for version in ("1.1.0", "1.2.0"):
        root = tmp_path / version
        events, results, _ = run_chain(
            root,
            classifier_chain("LogisticRegression", version, apply_model=True),
            monkeypatch,
        )
        assert events.count("node.completed") == 5
        observed[version] = (
            (root / results["apply"]["predictions"]["path"]).read_bytes(),
            read_json(root, results["clf"]["model"]["path"]),
        )
    union_predictions, union_model = observed["1.1.0"]
    fold_predictions, fold_model = observed["1.2.0"]

    # 数据面完全兼容：同一份权重载荷、同一组特征名，因而预测逐字节一致。
    assert union_model["pipelineArtifact"] == fold_model["pipelineArtifact"]
    assert union_model["featureNames"] == fold_model["featureNames"]
    assert fold_predictions == union_predictions
    # 声明面互斥：两份 fit 各说各话，谁也满足不了对方的契约。
    assert sorted(union_model["fit"]) == ["features", "split", "trainedCount", "trainedImageIds"]
    assert sorted(fold_model["fit"]) == ["features", "folds", "split"]
    assert union_model["fit"] != fold_model["fit"]
    assert json.loads(union_predictions)["evaluation"] == "external"


def test_model_apply_read_surface_is_the_shared_envelope_keys(tmp_path, monkeypatch):
    """记下 ModelApply 从 model 顶层读过的键：只有 pipelineArtifact 与 featureNames。

    这是消费端的承重证据——它不碰 fit，所以两种血缘形式对它都可用；哪天它开始读
    血缘字段，这个用例就当场变红，提醒必须给 model 契约升版本而不是就地绑端口。
    """
    from radstudio.operator import OperatorContext

    read_keys = set()

    class Tracked(dict):
        def get(self, key, default=None):
            read_keys.add(key)
            return dict.get(self, key, default)

    original = OperatorContext.input_json

    def tracked(self, name):
        payload = original(self, name)
        if name == "model" and isinstance(payload, dict):
            return Tracked(payload)
        return payload

    monkeypatch.setattr(OperatorContext, "input_json", tracked)
    for version in ("1.1.0", "1.2.0"):
        _, results, _ = run_chain(
            tmp_path / version,
            classifier_chain("LogisticRegression", version, apply_model=True),
            monkeypatch,
        )
        assert results["apply"]["predictions"]["path"]
    assert read_keys == {"pipelineArtifact", "featureNames"}
