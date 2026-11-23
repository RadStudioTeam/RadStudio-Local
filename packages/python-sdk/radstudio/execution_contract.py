"""Frozen logical parameters and platform execution controls; no deployment trust claim."""

import copy
import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from .json_contract import canonical_manifest_json, normalize_contract_json
from .parameter_validation import schema_value_issues, validate_parameter_values


class ExecutionContractError(ValueError):
    def __init__(self, code, message, issues=None):
        self.code = code
        self.details = {"issues": issues or []}
        super().__init__(message)


@lru_cache(maxsize=1)
def _definitions():
    return json.loads(
        files("radstudio")
        .joinpath("schemas/execution-contract.schema.json")
        .read_text(encoding="utf-8")
    )["$defs"]


def _validated(value, name):
    try:
        limits = {"max_bytes": 512 << 10, "max_depth": 96} if name == "parameterContract" else {}
        normalized = normalize_contract_json(value, **limits)
        issues = schema_value_issues(_definitions()[name], normalized)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        issues = [{"fieldPath": "", "message": "执行契约不是受支持的JSON"}]
    if issues:
        raise ExecutionContractError(
            "operator.contract_invalid", "执行契约字段无效，请检查平台与SDK版本", issues
        )
    return normalized


MAX_INVOCATION_BYTES = 32 << 20

_RESERVED = {"shardIndex", "shardCount", "mergeParts", "shardOffset", "foldIndices"}
_PROCESS_CONTROLS = {"workers", "checkpointEvery", "progressSeconds"}

# Built-ins keep platform defaults so published rows and frozen contracts stay byte-identical.
_BUILTIN_SHARDING = {
    "IBSI2DFeatureExtraction": {
        "split": "sample",
        "folds": False,
        "offset": False,
        "process": True,
        "keepInputs": False,
        "merge": (
            {
                "name": "features",
                "type": "feature_table",
                "artifact": "features.json",
                "port": "part{index}",
                "file": "part_{index}.json",
            },
        ),
    },
    "DLCNNTraining": {
        "split": "fold",
        "folds": True,
        "offset": False,
        "process": False,
        "keepInputs": True,
        "merge": (
            {
                "name": "model",
                "type": "model",
                "artifact": "checkpoint.pt",
                "port": "part{index}Model",
                "file": "part{index}_model.pt",
            },
            {
                "name": "predictions",
                "type": "prediction_table",
                "artifact": "predictions.json",
                "port": "part{index}Pred",
                "file": "part{index}_predictions.json",
            },
        ),
    },
    "DLCNNInference": {
        "split": "sample",
        "folds": False,
        "offset": True,
        "process": False,
        "keepInputs": False,
        "merge": (
            {
                "name": "predictions",
                "type": "prediction_table",
                "artifact": "predictions.json",
                "port": "part{index}Pred",
                "file": "part{index}_predictions.json",
            },
        ),
    },
}


def _is_merge_rule(port):
    rule = port.get("mergePart")
    return isinstance(rule, dict) and all(
        isinstance(rule.get(key), str) and rule[key] for key in ("artifact", "port", "file")
    )


def _sharding_profile(operator_type, ports):
    """Declared output merge rules opt in; built-ins fall back to the platform table."""
    declared = tuple(
        {
            "name": port["name"],
            "artifact": port["mergePart"]["artifact"],
            "port": port["mergePart"]["port"],
            "file": port["mergePart"]["file"],
        }
        for port in ((ports or {}).get("outputs") or [])
        if _is_merge_rule(port)
    )
    if not declared:
        return _BUILTIN_SHARDING.get(operator_type)
    split = (
        "fold"
        if any(port.get("type") == "split_plan" for port in ((ports or {}).get("inputs") or []))
        else "sample"
    )
    return {
        "split": split,
        "folds": split == "fold",
        "offset": split == "sample",
        "process": False,
        "keepInputs": split == "fold",
        "merge": declared,
    }


def validate_execution_contract(request):
    contract, controls = request.parameterContract, request.executionControls
    if contract is None and controls is None and request.invocationContract is None:
        return None
    digest = ""
    ports = None
    if contract is not None:
        value = _validated(contract, "parameterContract")
        digest = value.pop("digest")
        if (
            value["operatorType"] != request.operatorType
            or value["operatorVersion"] != request.operatorVersion
            or hashlib.sha256(canonical_manifest_json(value)).hexdigest() != digest
        ):
            raise ExecutionContractError(
                "operator.contract_invalid", "冻结参数定义与请求绑定或摘要不一致"
            )
        ports = value.get("artifactPorts")
        issues = validate_parameter_values(request.parameters, value["schema"])
        if issues:
            raise ExecutionContractError(
                "operator.parameters_invalid", "执行参数不符合冻结定义", issues
            )
    if contract is not None or controls is not None:
        require_logical_parameters(request.operatorType, request.parameters, ports)
    if controls is not None:
        value = _validated(controls, "executionControls")
        profile = _sharding_profile(request.operatorType, ports)
        if profile is None:
            raise ExecutionContractError("operator.contract_invalid", "该算子不支持平台执行控制")
        if "shardIndex" in value and (
            "shardCount" not in value or value["shardIndex"] >= value["shardCount"]
        ):
            raise ExecutionContractError("operator.contract_invalid", "分片索引必须小于分片数量")
        if "mergeParts" in value and (_RESERVED - {"mergeParts", "shardCount"}).intersection(value):
            raise ExecutionContractError(
                "operator.contract_invalid", "合并请求不能同时指定分片控制"
            )
        if not profile["process"] and _PROCESS_CONTROLS.intersection(value):
            raise ExecutionContractError(
                "operator.contract_invalid", "非特征提取算子不支持特征进程控制"
            )
        if ("foldIndices" in value and not profile["folds"]) or (
            "shardOffset" in value and not profile["offset"]
        ):
            raise ExecutionContractError("operator.contract_invalid", "折/样本控制与算子类型不匹配")
    if request.invocationContract is not None:
        actual = _validated(request.invocationContract, "invocationContract")
        expected = freeze_invocation_contract(request)
        if actual != expected:
            raise ExecutionContractError("operator.contract_invalid", "调用摘要与实际请求不一致")
        return {"version": 2, "parameterDigest": digest, "invocationDigest": actual["digest"]}
    return {"version": 1, "parameterDigest": digest}


def execution_parameters(ctx):
    """Legacy parameter controls are read only when no separated control envelope exists."""
    controls = getattr(getattr(ctx, "request", None), "executionControls", None)
    if controls is None:
        return ctx.parameters
    result = {
        **ctx.parameters,
        **{key: value for key, value in controls.items() if key != "version"},
    }
    return result


def require_logical_parameters(operator_type, parameters, ports=None):
    if _sharding_profile(operator_type, ports) is None or not _RESERVED.intersection(parameters):
        return
    reserved = _RESERVED.intersection(parameters)
    issues = [
        {
            "code": "parameter.execution_control",
            "fieldPath": "/" + key,
            "message": "此字段由平台调度管理，请从用户参数中移除；分片由管理员配置",
        }
        for key in sorted(reserved)
    ]
    raise ExecutionContractError(
        "operator.parameters_invalid", "分片与合并控制不能写入用户参数", issues
    )


def freeze_parameter_contract(operator_type, operator_version, schema, artifact_ports=None):
    body = {
        "version": 1,
        "dialect": "radstudio-parameters/1",
        "operatorType": operator_type,
        "operatorVersion": operator_version,
        "schema": copy.deepcopy({} if schema is None else schema),
    }
    if artifact_ports is not None:
        body["artifactPorts"] = copy.deepcopy(artifact_ports)
    return {**body, "digest": hashlib.sha256(canonical_manifest_json(body)).hexdigest()}


def invocation_artifact_ports(request):
    """Derive merge input ports from frozen output declarations, not from operator types."""
    from .artifact_boundary import validate_kind
    from .content_contract import resolve_content_contract
    from .port_contract import validate_port_contract

    contract = request.parameterContract
    if contract is None or "artifactPorts" not in contract:
        return None, None, False
    # validate_execution_contract must run first; it checks shape, digest, identity and controls.
    ports = copy.deepcopy(contract["artifactPorts"])
    for direction in ("inputs", "outputs"):
        values = ports[direction]
        if values is not None:
            names = [port["name"] for port in values]
            if len(set(names)) != len(names):
                raise ExecutionContractError("operator.contract_invalid", "冻结端口名称重复")
            for index, port in enumerate(values):
                field_path = f"/artifactPorts/{direction}/{index}/type"
                try:
                    validate_kind(port["type"])
                    field_path = f"/artifactPorts/{direction}/{index}/contract"
                    validate_port_contract(port.get("contract"))
                    if port.get("contract") is not None:
                        resolve_content_contract(port["type"], port["contract"])
                except ValueError as error:
                    raise ExecutionContractError(
                        "operator.contract_invalid",
                        str(error),
                        [
                            {
                                "fieldPath": field_path,
                                "message": str(error),
                            }
                        ],
                    ) from error
    controls = request.executionControls or {}
    parts = int(controls.get("mergeParts", 0))
    if parts:
        profile = _sharding_profile(request.operatorType, ports)
        if profile is None:
            raise ExecutionContractError("operator.contract_invalid", "此算子没有合并端口规则")
        outputs = {port["name"]: port for port in (ports["outputs"] or [])}

        def part(name, rule):
            source = outputs.get(rule["name"])
            if source is None or ("type" in rule and source["type"] != rule["type"]):
                raise ExecutionContractError(
                    "operator.contract_invalid", "合并输入缺少对应的冻结输出声明"
                )
            declared = {key: item for key, item in source.items() if key != "mergePart"}
            return {**declared, "name": name, "required": True}

        derived = [
            part(rule["port"].format(index=index), rule)
            for rule in profile["merge"]
            for index in range(parts)
        ]
        if profile["keepInputs"]:
            ports["inputs"] = (ports["inputs"] or []) + derived
        else:
            ports["inputs"] = derived
    names = [port["name"] for port in (ports["inputs"] or [])]
    if len(set(names)) != len(names):
        raise ExecutionContractError("operator.contract_invalid", "派生合并端口与冻结输入名称冲突")
    return ports["inputs"], ports["outputs"], True


def freeze_invocation_contract(request):
    """Bind logical wire values, not transfer credentials or independently verified code/bytes."""
    if (
        any(
            not isinstance(getattr(request, key), str) or not getattr(request, key)
            for key in ("requestId", "runId", "nodeId", "operatorType", "operatorVersion")
        )
        or not isinstance(request.parameters, dict)
        or not isinstance(request.inputs, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in request.inputs.items()
        )
        or request.environment is not None
        and not isinstance(request.environment, dict)
        or request.executionControls is not None
        and not isinstance(request.executionControls, dict)
    ):
        raise ExecutionContractError(
            "operator.contract_invalid", "调用绑定缺少完整请求或字段类型无效"
        )
    if request.parameterContract is None or not request.operatorVersion:
        raise ExecutionContractError("operator.contract_invalid", "调用绑定需要精确版本和冻结定义")
    definition = _validated(request.parameterContract, "parameterContract")
    body = {
        "version": 1,
        "requestId": request.requestId,
        "runId": request.runId,
        "nodeId": request.nodeId,
        "operatorType": request.operatorType,
        "operatorVersion": request.operatorVersion,
        "parameterDigest": definition["digest"],
        "parameters": request.parameters,
        "inputs": request.inputs,
        "executionControls": request.executionControls or None,
        "environment": request.environment or None,
    }
    try:
        normalize_contract_json(request.parameters)
        normalized = normalize_contract_json(body, max_bytes=MAX_INVOCATION_BYTES, max_depth=96)
        digest = hashlib.sha256(canonical_manifest_json(normalized)).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ExecutionContractError(
            "operator.contract_invalid", "调用内容超过受支持的JSON预算"
        ) from error
    return {"version": 1, "digest": digest}
