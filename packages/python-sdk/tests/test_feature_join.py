"""FeatureTableJoin 1.0.0：按 imageId 稳定键合并多张特征表。

这些用例同时锁住协议 3 的 Python 读取面：cardinality=many 的输入只经
inputSets 提供，服务端冻结端口声明后不得再要求它们出现在单值 inputs 中。
"""

from radstudio import operators_builtin  # noqa: F401  # 导入即注册
from radstudio.operator import ArtifactStore, invoke, parse_ref

FEATURE_CONTRACT = {
    "id": "radstudio/feature-table",
    "version": "1",
    "task": "binary_classification",
    "dimension": "tabular",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "joinKey": {"type": "string", "enum": ["imageId"]},
        "join": {"type": "string", "enum": ["inner", "left"]},
        "conflict": {"type": "string", "enum": ["error", "prefix"]},
    },
    "additionalProperties": False,
}

PORTS = {
    "version": 1,
    "contentContracts": 1,
    "inputs": [
        {
            "name": "features",
            "type": "feature_table",
            "required": True,
            "cardinality": "many",
            "contract": dict(FEATURE_CONTRACT),
        }
    ],
    "outputs": [
        {
            "name": "features",
            "type": "feature_table",
            "required": True,
            "contract": dict(FEATURE_CONTRACT),
        },
        {"name": "report", "type": "report"},
    ],
}


def table(names, rows):
    return {
        "featureNames": list(names),
        "records": [
            {
                "imageId": image_id,
                "patientId": patient_id,
                "label": label,
                "values": list(values),
            }
            for image_id, patient_id, label, values in rows
        ],
    }


def _request(references, parameters, contract):
    request = {
        "requestId": "req-join",
        "runId": "run-1",
        "nodeId": "join",
        "operatorType": "FeatureTableJoin",
        "operatorVersion": "1.0.0",
        "parameters": parameters,
        "inputs": {},
        "inputSets": {"features": references},
    }
    if contract:
        from radstudio.execution_contract import freeze_parameter_contract

        request["parameterContract"] = freeze_parameter_contract(
            "FeatureTableJoin", "1.0.0", SCHEMA, PORTS
        )
    return request


def run_join(tmp_path, tables, parameters=None, contract=True, references=None):
    store = ArtifactStore(tmp_path / "workspace")
    if references is None:
        references = [
            store.write_json(f"features-{index}.json", value, "feature_table")
            for index, value in enumerate(tables)
        ]
    values = {"joinKey": "imageId", "join": "inner", "conflict": "error"}
    values.update(parameters or {})
    result = invoke(_request(references, values, contract), store)
    return result, store


def output_features(result, store):
    return store.read_json(result.outputs["features"])


def output_report(result, store):
    return store.read_json(result.outputs["report"])


def test_inner_join_aligns_by_image_id_not_row_order(tmp_path):
    first = table(
        ["a"],
        [
            ("i1", "p1", 0, [1.0]),
            ("i2", "p1", 1, [2.0]),
            ("i3", "p2", 0, [3.0]),
        ],
    )
    second = table(
        ["b"],
        [
            ("i3", "p2", 0, [30.0]),
            ("i1", "p1", 0, [10.0]),
        ],
    )
    result, store = run_join(tmp_path, [first, second])
    assert result.status == "completed", result.error

    features = output_features(result, store)
    assert features["featureNames"] == ["a", "b"]
    assert [row["imageId"] for row in features["records"]] == ["i1", "i3"]
    assert [row["values"] for row in features["records"]] == [
        [1.0, 10.0],
        [3.0, 30.0],
    ]

    report = output_report(result, store)
    assert report["join"] == "inner"
    assert report["joinKey"] == "imageId"
    assert report["kept"] == 2
    assert report["dropped"] == 1
    assert report["missing"] == 1
    assert report["duplicates"] == 0
    assert report["inputs"] == [
        {
            "index": 1,
            "records": 3,
            "uniqueKeys": 3,
            "columns": 1,
            "kept": 2,
            "dropped": 1,
            "missing": 0,
        },
        {
            "index": 2,
            "records": 2,
            "uniqueKeys": 2,
            "columns": 1,
            "kept": 2,
            "dropped": 0,
            "missing": 1,
        },
    ]
    assert report["renamed"] == []
    assert parse_ref(result.outputs["report"])["type"] == "report"


def test_left_join_keeps_anchor_rows_and_null_fills_missing_inputs(tmp_path):
    first = table(["a"], [("i1", "p1", 0, [1.0]), ("i2", "p1", 1, [2.0])])
    second = table(["b"], [("i2", "p1", 1, [20.0]), ("i3", "p2", 0, [30.0])])
    result, store = run_join(tmp_path, [first, second], {"join": "left"})
    assert result.status == "completed", result.error

    features = output_features(result, store)
    assert [row["imageId"] for row in features["records"]] == ["i1", "i2"]
    assert [row["values"] for row in features["records"]] == [
        [1.0, None],
        [2.0, 20.0],
    ]

    report = output_report(result, store)
    assert report["kept"] == 2
    assert report["dropped"] == 1
    assert report["missing"] == 1
    assert report["inputs"][1]["kept"] == 1
    assert report["inputs"][1]["dropped"] == 1
    assert report["inputs"][1]["missing"] == 1


def test_conflicting_columns_fail_by_default(tmp_path):
    first = table(["signal"], [("i1", "p1", 0, [1.0])])
    second = table(["signal"], [("i1", "p1", 0, [2.0])])
    result, _ = run_join(tmp_path, [first, second])
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"
    assert "signal" in result.error.message
    assert result.error.details["column"] == "signal"
    assert result.error.details["inputs"] == [1, 2]


def test_conflicting_columns_can_be_prefixed_with_input_index(tmp_path):
    first = table(["signal", "shared"], [("i1", "p1", 0, [1.0, 2.0])])
    second = table(["signal"], [("i1", "p1", 0, [3.0])])
    result, store = run_join(
        tmp_path, [first, second], {"conflict": "prefix"}
    )
    assert result.status == "completed", result.error

    features = output_features(result, store)
    assert features["featureNames"] == ["signal", "shared", "t2__signal"]
    assert features["records"][0]["values"] == [1.0, 2.0, 3.0]
    assert output_report(result, store)["renamed"] == [
        {"input": 2, "from": "signal", "to": "t2__signal"}
    ]


def test_prefix_rename_that_still_collides_is_rejected(tmp_path):
    first = table(["signal"], [("i1", "p1", 0, [1.0])])
    second = table(["signal"], [("i1", "p1", 0, [2.0])])
    third = table(["t2__signal"], [("i1", "p1", 0, [3.0])])
    result, _ = run_join(tmp_path, [first, second, third], {"conflict": "prefix"})
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"
    assert "t2__signal" in result.error.message


def test_duplicate_image_ids_are_reported_and_refused(tmp_path):
    first = table(["a"], [("i1", "p1", 0, [1.0])])
    second = table(
        ["b"],
        [("i1", "p1", 0, [2.0]), ("i1", "p1", 0, [3.0])],
    )
    # 无冻结端口时框架不做内容校验，由算子自己做防御性检查并给出计数。
    result, _ = run_join(tmp_path, [first, second], contract=False)
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"
    assert "1 行重复" in result.error.message
    assert result.error.details["duplicates"] == 1
    assert result.error.details["inputIndex"] == 2


def test_duplicate_image_ids_are_refused_by_content_contract_before_join(tmp_path):
    first = table(["a"], [("i1", "p1", 0, [1.0])])
    second = table(
        ["b"],
        [("i1", "p1", 0, [2.0]), ("i1", "p1", 0, [3.0])],
    )
    result, _ = run_join(tmp_path, [first, second])
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"
    assert "imageId重复" in result.error.message


def test_label_conflict_between_tables_is_refused(tmp_path):
    first = table(["a"], [("i1", "p1", 1, [1.0])])
    second = table(["b"], [("i1", "p1", 0, [2.0])])
    result, _ = run_join(tmp_path, [first, second])
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"
    assert "标签" in result.error.message
    assert result.error.details["imageId"] == "i1"


def test_patient_conflict_between_tables_is_refused(tmp_path):
    first = table(["a"], [("i1", "p1", 0, [1.0])])
    second = table(["b"], [("i1", "p9", 0, [2.0])])
    result, _ = run_join(tmp_path, [first, second])
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"
    assert "患者" in result.error.message
    assert result.error.details["imageId"] == "i1"


def test_null_label_adopts_the_known_label_from_another_table(tmp_path):
    first = table(["a"], [("i1", "p1", None, [1.0])])
    second = table(["b"], [("i1", "p1", 1, [2.0])])
    result, store = run_join(tmp_path, [first, second])
    assert result.status == "completed", result.error
    assert output_features(result, store)["records"][0]["label"] == 1


def test_input_set_reference_type_must_match_many_port(tmp_path):
    store = ArtifactStore(tmp_path / "workspace")
    report = store.write_json("report.json", {"note": "not features"}, "report")
    result, _ = run_join(tmp_path, [], references=[report])
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"
    assert "物理类型" in result.error.message


def test_many_port_requires_at_least_one_input_set_reference(tmp_path):
    result, _ = run_join(tmp_path, [], references=["file:none.json#sha256=" + "0" * 64 + ";type=feature_table"])
    assert result.status == "failed"
    assert result.error.code == "operator.input_invalid"


def test_parameters_default_to_inner_and_error_without_frozen_contract(tmp_path):
    first = table(["a"], [("i1", "p1", 0, [1.0]), ("i2", "p1", 1, [2.0])])
    second = table(["b"], [("i1", "p1", 0, [3.0])])
    result, store = run_join(
        tmp_path, [first, second], parameters={}, contract=False
    )
    assert result.status == "completed", result.error
    assert [row["imageId"] for row in output_features(result, store)["records"]] == ["i1"]


def test_unsupported_parameters_are_rejected_by_the_operator(tmp_path):
    first = table(["a"], [("i1", "p1", 0, [1.0])])
    result, _ = run_join(
        tmp_path, [first], {"join": "outer"}, contract=False
    )
    assert result.status == "failed"
    assert result.error.code == "operator.parameters_invalid"


def test_single_input_is_an_identity_join(tmp_path):
    first = table(["a"], [("i1", "p1", 0, [1.0]), ("i2", "p1", 1, [2.0])])
    result, store = run_join(tmp_path, [first])
    assert result.status == "completed", result.error
    features = output_features(result, store)
    assert features["featureNames"] == ["a"]
    assert [row["imageId"] for row in features["records"]] == ["i1", "i2"]
    report = output_report(result, store)
    assert report["kept"] == 2
    assert report["dropped"] == 0
    assert report["missing"] == 0
