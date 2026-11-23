"""Declaration compatibility; does not verify artifact scientific content."""

import re
import unicodedata

VERSION_PATTERN = re.compile(
    r"[0-9]+(?:\.[0-9]+){0,2}(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?"
)


class PortContractError(ValueError):
    code = "edge.type"

    def __init__(self, message, edge=None):
        self.details = (
            {
                "edge": {
                    key: edge[key]
                    for key in ("id", "source", "target", "sourceOutput", "targetInput")
                    if isinstance(edge.get(key), str)
                }
            }
            if edge is not None
            else {}
        )
        super().__init__(message)


def port_contract_error(output, input_port):
    if output["type"] != input_port["type"]:
        return "端口数据类型不兼容"
    try:
        validate_port_contract(output.get("contract"))
        validate_port_contract(input_port.get("contract"))
    except ValueError as error:
        return str(error)
    required = input_port.get("contract")
    if required is None:
        return None
    actual = output.get("contract")
    if actual is None:
        return "上游缺少输入要求的数据契约，请使用经过验证的适配算子"
    if actual.get("id") != required.get("id") or actual.get("version") != required.get("version"):
        return "数据契约或版本不兼容，请使用显式适配算子"
    for field, name in (("task", "任务"), ("dimension", "维度"), ("modelFamily", "模型家族")):
        if required.get(field) and actual.get(field, "") != required[field]:
            return name + "约束不匹配"
    return None


def validate_port_contract(contract):
    if contract is None:
        return
    if not isinstance(contract, dict) or any(
        not isinstance(contract.get(key), str) or not contract[key] for key in ("id", "version")
    ):
        raise ValueError("端口契约必须声明id和精确version")
    if set(contract) - {"id", "version", "task", "dimension", "modelFamily"}:
        raise ValueError("端口契约包含未支持的字段，请检查字段名称与契约版本")
    if VERSION_PATTERN.fullmatch(contract["version"]) is None:
        raise ValueError("端口契约version必须是精确数字版本，不支持范围或latest")
    for key in ("id", "version", "task", "modelFamily"):
        value = contract.get(key, "")
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > 256
            or any(char.isspace() or unicodedata.category(char) == "Cc" for char in value)
        ):
            raise ValueError("端口契约字段不能包含空白、控制字符或超过256字节")
    if contract.get("dimension", "") not in ("", "2d", "3d", "tabular"):
        raise ValueError("端口契约dimension必须为2d、3d或tabular")
