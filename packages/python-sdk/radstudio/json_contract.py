"""Strict bounded JSON codec shared by manifest and parameter contracts."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

MAX_BYTES = 256 << 10
_RUNTIME_SURROGATES = re.compile(r"[\ud800-\udfff]")


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON对象键重复")
        result[key] = value
    return result


def decode_manifest_json(data: bytes, *, max_bytes: int = MAX_BYTES, max_depth: int = 64) -> Any:
    return _decode_bounded_json(data, max_bytes, max_depth, _number)


def decode_content_json(
    data: bytes, *, max_bytes: int, numeric_policy: dict, max_depth: int = 64
) -> Any:
    return _decode_bounded_json(
        data, max_bytes, max_depth, lambda raw: _scientific_number(raw, numeric_policy)
    )


def _decode_bounded_json(data, max_bytes, max_depth, parse_number):
    if len(data) > max_bytes:
        raise ValueError(f"JSON文档超过{max_bytes >> 10}KiB")
    value = json.loads(
        data.decode("utf-8"),
        parse_int=parse_number,
        parse_float=parse_number,
        object_pairs_hook=_pairs,
        parse_constant=lambda raw: (_ for _ in ()).throw(ValueError("非法JSON数字")),
    )

    def check(node: Any, depth: int = 0) -> None:
        if depth > max_depth:
            raise ValueError(f"JSON嵌套超过{max_depth}层")
        if isinstance(node, str):
            node.encode("utf-8")  # rejects unpaired surrogates, preserves valid astral characters
        elif isinstance(node, dict):
            for key, child in node.items():
                check(key, depth + 1)
                check(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                check(child, depth + 1)

    check(value)
    return value


def _decimal(value: Decimal) -> str:
    # Bounds are applied to parsed value and raw text by the decoder separately.
    if not value.is_finite():
        raise ValueError("非法JSON数字")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def encode_contract_json(value: Any, *, canonical: bool = False) -> bytes:
    def emit(node: Any) -> str:
        if node is None:
            return "null"
        if isinstance(node, bool):
            return "true" if node else "false"
        if isinstance(node, (Decimal, int, float)):
            number = _number(str(node))
            return _decimal(number) if canonical else str(number)
        if isinstance(node, str):
            return json.dumps(node, ensure_ascii=False, separators=(",", ":"))
        if isinstance(node, list):
            return "[" + ",".join(emit(child) for child in node) + "]"
        if isinstance(node, dict):
            return "{" + ",".join(emit(key) + ":" + emit(node[key]) for key in sorted(node)) + "}"
        raise ValueError("未支持JSON值")

    return emit(value).encode("utf-8")


class ManifestNumericError(ValueError):
    pass


def _number(raw: str) -> Decimal:
    if len(raw) > 256:
        raise ManifestNumericError("数字表示过长")
    parts = raw.lower().split("e")
    exponent = int(parts[1]) if len(parts) == 2 else 0
    if not -308 <= exponent <= 308:
        raise ManifestNumericError("数字指数须在-308至308之间")
    mantissa = parts[0].lstrip("-")
    scale = (len(mantissa.split(".", 1)[1]) if "." in mantissa else 0) - exponent
    digits = len(mantissa.replace(".", ""))
    if scale < 0:
        digits -= scale
    if scale >= digits:
        digits = scale + 1
    if digits > 128:
        raise ManifestNumericError("数字展开后不超过128位（含小数尺度）")
    return Decimal(raw)


def canonical_manifest_json(value: Any) -> bytes:
    return encode_contract_json(value, canonical=True)


def normalize_contract_json(value: Any, *, max_bytes: int = MAX_BYTES, max_depth: int = 64) -> Any:
    return decode_manifest_json(
        encode_contract_json(value), max_bytes=max_bytes, max_depth=max_depth
    )


def decode_runtime_json(data: bytes, *, max_bytes: int = 512 << 20, max_depth: int = 96) -> Any:
    """Decode transport JSON without rounding; parameter budgets apply separately."""
    if len(data) > max_bytes:
        raise ValueError("request JSON exceeds transport byte budget")
    value = json.loads(
        data.decode("utf-8"),
        parse_float=Decimal,
        object_pairs_hook=_pairs,
        parse_constant=lambda raw: (_ for _ in ()).throw(ValueError("非法JSON数字")),
    )

    def check(node, depth=0):
        if depth > max_depth:
            raise ValueError("request JSON exceeds transport depth budget")
        if isinstance(node, str):
            if _RUNTIME_SURROGATES.search(node):
                raise ValueError("JSON含不成对Unicode代理项")
        elif isinstance(node, dict):
            for key, child in node.items():
                check(key, depth + 1)
                check(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                check(child, depth + 1)

    check(value)
    return value


def encode_runtime_json(value: Any) -> bytes:
    """Preserve Decimal output numbers without imposing parameter numeric budgets."""

    def key_text(key):
        if isinstance(key, str):
            return key
        if key is None:
            return "null"
        if isinstance(key, (bool, int, float)):
            return json.dumps(key, allow_nan=False)
        raise TypeError("unsupported JSON object key")

    def emit(node):
        if isinstance(node, Decimal):
            if not node.is_finite():
                raise ValueError("non-finite JSON number")
            return str(node)
        try:
            # Preserve native JSON bytes and the C encoder fast path for large
            # scientific outputs; fall back only for Decimal-containing values.
            return json.dumps(node, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except TypeError:
            if isinstance(node, dict):
                return (
                    "{"
                    + ", ".join(
                        json.dumps(key_text(key), ensure_ascii=False) + ": " + emit(child)
                        for key, child in sorted(node.items())
                    )
                    + "}"
                )
            if isinstance(node, (list, tuple)):
                return "[" + ", ".join(emit(child) for child in node) + "]"
            raise

    return emit(value).encode("utf-8")


def _scientific_number(raw, policy):
    if len(raw) > policy["maxTokenBytes"]:
        raise ValueError("科学数字表示超过契约字节预算")
    parts = raw.lower().split("e")
    exponent = int(parts[1]) if len(parts) == 2 else 0
    if not policy["minExponent"] <= exponent <= policy["maxExponent"]:
        raise ValueError("科学数字显式指数超出契约预算")
    return Decimal(raw)
