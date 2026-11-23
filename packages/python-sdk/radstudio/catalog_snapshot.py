"""Digest-bound node definitions; code and dependency trust remain separate."""

import copy
import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from .json_contract import (
    canonical_manifest_json,
    decode_manifest_json,
    decode_runtime_json,
    encode_contract_json,
)
from .operator import legacy_fc_aliases
from .parameter_validation import schema_value_issues
from .port_contract import validate_port_contract

MAX_CATALOG_BYTES = 16 << 20
RULES = {
    "parameterDialect": "radstudio-parameters/1",
    "implementationSelection": "exact-v1",
    "contentContracts": 1,
    "artifactExecution": 1,
}
DEFINITION_FIELDS = {
    "type",
    "version",
    "name",
    "category",
    "description",
    "status",
    "parametersSchema",
    "defaults",
    "inputs",
    "outputs",
    "entrypoint",
}


class CatalogSnapshotError(ValueError):
    code = "runner.catalog_invalid"

    def __init__(self, message, details=None):
        self.details = details or {}
        super().__init__(message)


@lru_cache(maxsize=1)
def _schema():
    return json.loads(
        files("radstudio")
        .joinpath("schemas/operator-catalog-snapshot.schema.json")
        .read_text(encoding="utf-8")
    )


def catalog_digest(value):
    body = {key: child for key, child in value.items() if key != "digest"}
    return hashlib.sha256(canonical_manifest_json(body)).hexdigest()


def _nodes(ir):
    nodes = ir.get("nodes")
    if not isinstance(nodes, list) or not nodes or len(nodes) > 500:
        raise CatalogSnapshotError("冻结目录要求1—500个节点")
    indexed = {}
    for node in nodes:
        if not isinstance(node, dict) or any(
            not isinstance(node.get(key), str) or not node[key]
            for key in ("id", "type", "operatorVersion")
        ):
            raise CatalogSnapshotError("节点ID、算子类型和精确版本不能为空")
        if node["id"] in indexed:
            raise CatalogSnapshotError("冻结目录节点ID重复")
        indexed[node["id"]] = node
    return indexed


def validate_catalog_snapshot(ir, snapshot):
    try:
        normalized = decode_manifest_json(
            encode_contract_json(snapshot), max_bytes=MAX_CATALOG_BYTES, max_depth=96
        )
        issues = schema_value_issues(_schema(), normalized)
        if issues:
            raise CatalogSnapshotError(
                "冻结算子目录字段无效：" + issues[0]["fieldPath"] + " " + issues[0]["message"]
            )
        if catalog_digest(normalized) != normalized["digest"]:
            raise CatalogSnapshotError("冻结算子目录摘要不匹配")
        nodes = _nodes(ir)
        if set(nodes) != set(normalized["nodes"]):
            raise CatalogSnapshotError("冻结目录与工作流节点集合不一致")
        for nid, node in nodes.items():
            entry = normalized["nodes"][nid]
            definition = entry["definition"]
            for direction in ("inputs", "outputs"):
                for index, port in enumerate(definition[direction] or []):
                    try:
                        validate_port_contract(port.get("contract"))
                    except ValueError as error:
                        raise CatalogSnapshotError(
                            f"节点 {nid} 的端口契约无效：{error}",
                            {
                                "nodeId": nid,
                                "fieldPath": f"operatorCatalogSnapshot.nodes.{nid}.definition.{direction}.{index}.contract",
                            },
                        ) from error
            if (
                entry["requestedType"] != node["type"]
                or entry["requestedVersion"] != node["operatorVersion"]
                or definition["type"] != node["type"]
            ):
                raise CatalogSnapshotError("冻结目录节点类型/版本绑定不一致：" + nid)
            requested, declared = node["operatorVersion"], definition["version"]
            if (
                requested != declared
                and legacy_fc_aliases().get((node["type"], declared)) != requested
                and legacy_fc_aliases().get((node["type"], requested)) != declared
            ):
                raise CatalogSnapshotError("冻结定义不是精确版本或封闭FC映射：" + nid)
        return normalized
    except CatalogSnapshotError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise CatalogSnapshotError("冻结目录不是受支持的有界JSON") from exc


def capture_catalog_snapshot(ir, definitions, source="installed-catalog"):
    if not isinstance(definitions, list):
        raise CatalogSnapshotError("算子目录必须为列表")
    catalog = {}
    for value in definitions:
        key = (value["type"], value["version"])
        if key in catalog:
            raise CatalogSnapshotError("算子目录包含重复类型/版本")
        catalog[key] = value
    entries = {}
    for nid, node in _nodes(ir).items():
        definition = catalog.get((node["type"], node["operatorVersion"]))
        if definition is None:
            raise CatalogSnapshotError(
                "算子精确版本不在目录中：" + node["type"] + "@" + node["operatorVersion"]
            )
        entries[nid] = {
            "requestedType": node["type"],
            "requestedVersion": node["operatorVersion"],
            "definition": {
                key: copy.deepcopy(value)
                for key, value in definition.items()
                if key in DEFINITION_FIELDS
            },
        }
    value = {"version": 1, "source": source, "rules": dict(RULES), "nodes": entries}
    value["digest"] = catalog_digest(value)
    return validate_catalog_snapshot(ir, value)


def resolve_request_catalog(request, catalog_loader):
    snapshot = request.get("operatorCatalogSnapshot")
    if "operatorCatalogSnapshot" in request:
        return validate_catalog_snapshot(request["ir"], snapshot)
    definitions = decode_manifest_json(
        catalog_loader().read_bytes(), max_bytes=MAX_CATALOG_BYTES, max_depth=96
    )
    return capture_catalog_snapshot(request["ir"], definitions)


def load_runner_request(data):
    return decode_runtime_json(data, max_bytes=32 << 20, max_depth=128)
