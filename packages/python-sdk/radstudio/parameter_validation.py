"""Controlled JSON Schema dialect shared by graph, registration and manifest validation."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, validators

from .json_contract import encode_contract_json, normalize_contract_json

_INTEGER = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer",
    lambda checker, value: (
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value())
    ),
)
ParameterValidator = validators.extend(Draft202012Validator, type_checker=_INTEGER)


def _issue(code: str, path: str, message: str) -> dict:
    return {"code": code, "severity": "error", "fieldPath": path, "message": message}


def _type_label(value: str) -> str:
    return {
        "object": "对象",
        "array": "列表",
        "string": "文本",
        "number": "数值",
        "integer": "整数",
        "boolean": "布尔值",
        "null": "空值",
    }.get(value, value)


def schema_value_issues(
    schema: dict, value: Any, prefix: str = "", *, code: str = "parameter.invalid"
) -> list[dict]:
    result = []
    for error in ParameterValidator(schema).iter_errors(value):
        path = prefix + "".join(
            "/" + str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path
        )
        if error.validator == "required":
            for key in error.validator_value:
                if key not in error.instance:
                    field = str(key).replace("~", "~0").replace("/", "~1")
                    result.append(
                        _issue(
                            code,
                            path + "/" + field,
                            "缺少必填字段：" + str(key),
                        )
                    )
        elif error.validator == "additionalProperties" and error.validator_value is False:
            for key in sorted(set(error.instance) - set(error.schema.get("properties", {}))):
                field = str(key).replace("~", "~0").replace("/", "~1")
                result.append(
                    _issue(
                        code,
                        path + "/" + field,
                        "不支持此字段，请检查拼写或查阅参数规范",
                    )
                )
        elif error.validator == "type":
            expected = error.validator_value
            if isinstance(expected, list):
                expected = "、".join(_type_label(item) for item in expected)
            result.append(_issue(code, path, "字段类型应为：" + _type_label(expected)))
        elif error.validator in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}:
            comparison = {
                "minimum": "大于或等于",
                "maximum": "小于或等于",
                "exclusiveMinimum": "大于",
                "exclusiveMaximum": "小于",
            }[error.validator]
            bound = encode_contract_json(error.validator_value, canonical=True).decode("utf-8")
            result.append(_issue(code, path, f"数值必须{comparison} {bound}"))
        elif error.validator in {"minItems", "maxItems"}:
            count = encode_contract_json(error.validator_value, canonical=True).decode("utf-8")
            action = "至少需要" if error.validator == "minItems" else "最多允许"
            result.append(_issue(code, path, f"列表{action} {count} 项"))
        elif error.validator == "uniqueItems":
            result.append(_issue(code, path, "列表包含重复项，请保留唯一值"))
        elif error.validator == "enum":
            result.append(_issue(code, path, "请从已声明的选项中选择，不能使用其他值"))
        else:
            result.append(_issue(code, path, "参数不符合声明约束：" + str(error.validator)))
        if len(result) >= 20:
            break
    return result[:20]


@lru_cache(maxsize=1)
def _parameter_meta() -> dict:
    return json.loads(
        files("radstudio")
        .joinpath("schemas/parameter-schema.schema.json")
        .read_text(encoding="utf-8")
    )


@lru_cache(maxsize=64)
def _compiled_parameter_validator(schema_json: bytes):
    # Bytes were bounded/normalized by validate_parameter_values before caching.
    schema = json.loads(schema_json, parse_int=Decimal, parse_float=Decimal)
    issues = schema_value_issues(_parameter_meta(), schema, code="parameter.schema_invalid")
    for issue in issues:
        issue["message"] = "算子参数规范无效，请联系作者或选择经过验证的版本：" + issue["message"]
    return (None, issues) if issues else (ParameterValidator(schema), [])


def validate_parameter_values(value: Any, schema: dict) -> list[dict]:
    try:
        normalized_schema = normalize_contract_json(schema)
        encoded = encode_contract_json(normalized_schema)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return [_issue("parameter.schema_invalid", "", "算子参数规范不是受支持的JSON")]
    validator, issues = _compiled_parameter_validator(encoded)
    if issues:
        return copy.deepcopy(issues)
    try:
        normalized = normalize_contract_json(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return [
            _issue("parameter.json_invalid", "", "参数不是受支持的JSON，或超过大小/数字/深度限制")
        ]
    return schema_value_issues(validator.schema, normalized)


class ParameterValidationError(ValueError):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__(
            "；".join(issue["fieldPath"] + ": " + issue["message"] for issue in issues)
        )


def require_valid_parameters(value: Any, schema: dict) -> None:
    issues = validate_parameter_values(value, schema)
    if issues:
        raise ParameterValidationError(issues)
