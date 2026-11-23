"""Closed platform content registry v1; no remote schemas or implicit legacy upgrade."""

import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from .json_contract import canonical_manifest_json, decode_content_json
from .parameter_validation import ParameterValidator
from .port_contract import validate_port_contract


class ContentContractError(ValueError):
    code = "artifact.content_invalid"

    def __init__(self, message, field="", contract=""):
        self.details = {"fieldPath": field, "contract": contract}
        super().__init__(message)


@lru_cache(maxsize=1)
def content_registry():
    return json.loads(
        files("radstudio")
        .joinpath("schemas/artifact-content-contracts.json")
        .read_text(encoding="utf-8")
    )


def resolve_content_contract(physical_type, contract):
    try:
        if contract is None:
            raise ValueError("内容校验需要显式契约")
        validate_port_contract(contract)
    except ValueError as error:
        raise ContentContractError("内容契约声明无效：" + str(error)) from error
    registry = content_registry()
    identity = contract["id"] + "@" + contract["version"]
    entry = registry["contracts"].get(identity)

    def fail(message, field=""):
        raise ContentContractError(message, field, identity)

    if entry is None:
        fail("平台尚未注册此内容契约，无法验证")
    if entry["physicalType"] != physical_type:
        fail("物理类型与内容契约不匹配")
    if entry["physicalType"] != "model" and (
        contract.get("task", "") not in ("", "binary_classification")
        or contract.get("dimension", "") not in ("", "tabular")
        or contract.get("modelFamily", "")
    ):
        fail("首批表格内容契约不支持此任务、维度或模型家族约束")
    return identity, entry


def validate_content_bytes(data, physical_type, contract):
    registry = content_registry()
    identity, entry = resolve_content_contract(physical_type, contract)

    def fail(message, field=""):
        raise ContentContractError(message, field, identity)

    try:
        value = decode_content_json(
            data,
            max_bytes=registry["maxBytes"],
            max_depth=registry["maxDepth"],
            numeric_policy=registry["numericPolicy"],
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        fail("内容不是受支持的有界JSON：" + str(error))
    issue = next(ParameterValidator(entry["schema"]).iter_errors(value), None)
    if issue is not None:
        field = "".join(
            "/" + str(part).replace("~", "~0").replace("/", "~1") for part in issue.absolute_path
        )
        if issue.validator == "required":
            missing = next(key for key in issue.validator_value if key not in issue.instance)
            field += "/" + missing.replace("~", "~0").replace("/", "~1")
        fail("内容字段不符合契约结构", field)
    if physical_type == "split_plan":
        universe = validate_split_structure(value, fail)
        return {"rules": "artifact-content/1", "contract": identity, "records": len(universe)}
    if physical_type == "model":
        for key, label in (("task", "任务"), ("dimension", "维度"), ("modelFamily", "模型家族")):
            declared = contract.get(key, "")
            field = "family" if key == "modelFamily" else key
            if declared and declared != value[field]:
                fail(label + "与端口契约声明不一致", "/" + field)
        return {
            "rules": "artifact-content/1",
            "contract": identity,
            "records": model_fitted_count(value, identity),
        }
    if physical_type == "sample_collection":
        validate_sample_collection(value, fail)
        return {"rules": "artifact-content/1", "contract": identity, "records": len(value)}
    seen = set()
    for index, row in enumerate(value["records"]):
        prefix = f"/records/{index}"
        if row["imageId"] in seen:
            fail("样本键imageId重复，禁止按行号合并", prefix + "/imageId")
        seen.add(row["imageId"])
        if physical_type == "feature_table":
            if len(row["values"]) != len(value["featureNames"]):
                fail("特征值数量与featureNames不一致", prefix + "/values")
        else:
            role = value["evaluation"]
            if role != "inference" and row["label"] is None:
                fail("评估角色需要真实标签", prefix + "/label")
            if role == "out_of_fold" and row["fold"] is None:
                fail("OOF预测必须标记fold", prefix + "/fold")
            if role != "out_of_fold" and row["fold"] is not None:
                fail("非OOF预测不应携带训练折号", prefix + "/fold")
    return {"rules": "artifact-content/1", "contract": identity, "records": len(seen)}


def text_field(row, key):
    value = row.get(key)
    return value if isinstance(value, str) else ""


def sample_path_issue(path):
    """数据集存储路径允许绝对路径与空格：只有超预算、反斜杠、控制字符与越界名称段才拒绝。"""
    from .artifact_boundary import reference_rules

    if len(path.encode("utf-8")) > reference_rules()["maxPathBytes"]:
        return "样本路径超出引用字节预算"
    if "\\" in path:
        return "样本路径必须使用POSIX分隔符"
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return "样本路径不能包含控制字符"
    if any(part in (".", "..") for part in path.split("/")):
        return "样本路径包含越界名称段"
    return ""


def validate_sample_collection(value, fail):
    """核对样本集合的身份与引用路径，口径与 DataInput 的入库前自检一致。"""
    if not isinstance(value, list):
        fail("样本集合根节点必须是列表")
    seen = set()
    patient_labels = {}
    for index, row in enumerate(value):
        prefix = f"/{index}"
        if not isinstance(row, dict):
            fail("样本记录必须是对象", prefix)
        image = text_field(row, "imageId").strip()
        if not image:
            fail("样本键imageId为空白", prefix + "/imageId")
        if image in seen:
            fail("样本键imageId重复，禁止按行号合并", prefix + "/imageId")
        seen.add(image)
        patient = text_field(row, "patientId").strip()
        if not patient:
            fail("样本缺少患者身份，无法做患者隔离", prefix + "/patientId")
        image_path = text_field(row, "imagePath")
        if not image_path.strip():
            fail("样本影像路径为空白", prefix + "/imagePath")
        issue = sample_path_issue(image_path)
        if issue:
            fail(issue, prefix + "/imagePath")
        roi_path = text_field(row, "roiPath")
        if roi_path:
            if not roi_path.strip():
                fail("样本掩膜路径为空白", prefix + "/roiPath")
            issue = sample_path_issue(roi_path)
            if issue:
                fail(issue, prefix + "/roiPath")
        label = row.get("label")
        if label is not None:
            previous = patient_labels.get(patient)
            if previous is not None and previous != label:
                fail("同一患者标签不一致", prefix + "/label")
            patient_labels[patient] = label


def source_identity(table):
    return hashlib.sha256(
        canonical_manifest_json(
            [
                {key: row[key] for key in ("imageId", "patientId", "label")}
                for row in table["records"]
            ]
        )
    ).hexdigest()


def validate_split_structure(value, fail):
    from .artifact_boundary import parse_reference

    try:
        if parse_reference(value["source"])["type"] != "feature_table":
            fail("划分来源必须引用特征表", "/source")
    except ValueError:
        fail("划分来源引用无效", "/source")
    if value.get("scheme") == "holdout":
        seen = set()
        for name in ("train", "validation", "test"):
            group = set(value["partitions"][name])
            if seen & group:
                fail("留出集合存在重复样本", "/partitions/" + name)
            seen.update(group)
        return seen
    universe = None
    held_out = set()
    fold_ids = set()
    for index, fold in enumerate(value["folds"]):
        prefix = f"/folds/{index}"
        if fold["fold"] in fold_ids:
            fail("折编号重复", prefix + "/fold")
        fold_ids.add(fold["fold"])
        train, test = set(fold["trainIds"]), set(fold["testIds"])
        if train & test:
            fail("同一折训练与测试样本重叠", prefix)
        current = train | test
        if universe is None:
            universe = current
        if current != universe:
            fail("各折样本全集不一致", prefix)
        if held_out & test:
            fail("样本重复进入测试折", prefix + "/testIds")
        held_out.update(test)
    if fold_ids != set(range(len(value["folds"]))):
        fail("折编号必须从0连续编号", "/folds")
    if held_out != universe:
        fail("测试折未完整覆盖样本全集", "/folds")
    return universe


def validate_split_source(split, table):
    def fail(message, field):
        raise ContentContractError(message, field, "radstudio/split-plan@1")

    if source_identity(table) != split["identityHash"]:
        fail("划分来源样本身份摘要不匹配", "/identityHash")
    universe = validate_split_structure(split, fail)
    if any(not row[key].strip() for row in table["records"] for key in ("imageId", "patientId")):
        fail("划分来源样本或患者身份为空白", "/source")
    rows = {row["imageId"]: row for row in table["records"]}
    if universe != set(rows):
        fail("划分与来源特征表样本集合不一致", "/source")
    if any(row["label"] is None for row in rows.values()):
        fail("训练划分需要已知标签", "/source")
    patient_labels = {}
    for row in rows.values():
        patient = row["patientId"]
        if patient in patient_labels and patient_labels[patient] != row["label"]:
            fail("同一患者标签不一致", "/source")
        patient_labels[patient] = row["label"]
    if split.get("scheme") == "holdout":
        seen = set()
        for name in ("train", "validation", "test"):
            patients = {rows[key]["patientId"] for key in split["partitions"][name]}
            if seen & patients:
                fail("留出集合存在患者重叠", "/partitions/" + name)
            seen.update(patients)
    else:
        for index, fold in enumerate(split["folds"]):
            train = {rows[key]["patientId"] for key in fold["trainIds"]}
            test = {rows[key]["patientId"] for key in fold["testIds"]}
            if train & test:
                fail("同一折训练与测试患者重叠", f"/folds/{index}")


def model_fail(message, field, identity="radstudio/model@1"):
    raise ContentContractError(message, field, identity)


def is_fold_lineage(identity):
    """model@2 把拟合声明拆到每一折；model@1 只认一份全集声明。"""
    return identity.endswith("@2")


def is_image_lineage(identity):
    """model@3 按影像交叉验证折声明训练影像集，只有一份合并后的集成权重。"""
    return identity.endswith("@3")


def model_fitted_count(value, identity):
    fit = value["fit"]
    if is_fold_lineage(identity) or is_image_lineage(identity):
        return sum(fold["trainedCount"] for fold in fit["folds"])
    return len(fit["trainedImageIds"])


def load_model_lineage(roots, reference, physical_type, contract, field, fail):
    """Re-read a declared lineage file: it must still be the exact bytes the reference names."""
    from .artifact_boundary import parse_reference, workspace_path

    registry = content_registry()
    maximum = registry["maxBytes"]
    try:
        info = parse_reference(reference)
        if info["type"] != physical_type:
            fail("血缘引用的物理类型不符", field)
        data = None
        for root in roots:
            candidate = workspace_path(root, info["path"])
            if candidate.exists():
                with candidate.open("rb") as source:
                    data = source.read(maximum + 1)
                break
        if data is None:
            fail("血缘文件缺失或路径越界", field)
    except ContentContractError:
        raise
    except (ValueError, OSError) as error:
        fail("血缘引用或路径无效：" + str(error), field)
    if len(data) > maximum or hashlib.sha256(data).hexdigest() != info["sha256"]:
        fail("血缘文件超预算或哈希与引用不匹配", field)
    validate_content_bytes(data, physical_type, contract)
    return decode_content_json(
        data,
        max_bytes=registry["maxBytes"],
        max_depth=registry["maxDepth"],
        numeric_policy=registry["numericPolicy"],
    )


def validate_model_weights(weights, self_path, roots, prefix, fail, digests=None):
    from .artifact_boundary import hash_file, workspace_path

    path = None
    for root in roots:
        try:
            candidate = workspace_path(root, weights["path"])
        except ValueError as error:
            fail("权重文件路径无效：" + str(error), prefix + "/path")
        if candidate.exists():
            path = candidate
            break
    if path is None:
        fail("权重文件缺失或路径越界", prefix + "/path")
    if path == self_path:
        fail("权重引用不能是模型清单自身", prefix + "/path")
    digest, size = hash_file(path)
    if digest != weights["sha256"]:
        fail("权重字节与声明摘要不一致", prefix + "/sha256")
    if digests is not None:
        if weights["sha256"] in digests:
            fail("不同折声明了同一份权重字节", prefix + "/sha256")
        digests.add(weights["sha256"])
    if weights["sizeBytes"] != size:
        fail("权重实际大小与声明不一致", prefix + "/sizeBytes")


def validate_model_folds(folds, plan, rows, ensemble_path, self_path, roots, fail):
    """逐折对照：每一折的训练集必须与划分方案的训练集完全一致，权重各自成文件。"""
    plan_folds = {fold["fold"]: fold for fold in plan["folds"]}
    declared_numbers = set()
    declared_paths = set()
    declared_digests = set()
    for index, fold in enumerate(folds):
        prefix = f"/fit/folds/{index}"
        number = fold["fold"]
        if number in declared_numbers:
            fail("同一折编号被声明了两次", prefix + "/fold")
        declared_numbers.add(number)
        if number not in plan_folds:
            fail("声明的折不在划分方案的折集合内", prefix + "/fold")
        trained = list(fold["trainedImageIds"])
        if fold["trainedCount"] != len(trained):
            fail("本折拟合样本数量与trainedImageIds不一致", prefix + "/trainedCount")
        for sample in trained:
            row = rows.get(sample)
            if row is None:
                fail("本折拟合样本不在来源特征表中", prefix + "/trainedImageIds")
            if row["label"] is None:
                fail("本折拟合样本缺少已知标签", prefix + "/trainedImageIds")
        if set(trained) != set(plan_folds[number]["trainIds"]):
            fail("本折声明的拟合样本与划分方案的训练集不一致", prefix + "/trainedImageIds")
        weights = fold["weights"]
        if weights["path"] == ensemble_path:
            fail("折权重不能指向打包后的集成模型权重", prefix + "/weights/path")
        if weights["path"] in declared_paths:
            fail("不同折指向同一份权重文件", prefix + "/weights/path")
        declared_paths.add(weights["path"])
        validate_model_weights(
            weights, self_path, roots, prefix + "/weights", fail, declared_digests
        )
    if declared_numbers != set(plan_folds):
        fail("折声明集合与划分方案的折不一致", "/fit/folds")


def validate_model_image_folds(folds, plan, rows, fail):
    """逐折核对影像模型的拟合声明：训练影像集与划分一致、标签已知、测试数量对得上。

    影像模型只有一份合并后的集成权重，折内不再各自声明权重，因此这里只核折集合与样本口径。
    """
    plan_folds = {fold["fold"]: fold for fold in plan["folds"]}
    declared_numbers = set()
    for index, fold in enumerate(folds):
        prefix = f"/fit/folds/{index}"
        number = fold["fold"]
        if number in declared_numbers:
            fail("同一折编号被声明了两次", prefix + "/fold")
        declared_numbers.add(number)
        if number not in plan_folds:
            fail("声明的折不在划分方案的折集合内", prefix + "/fold")
        trained = list(fold["trainedImageIds"])
        if fold["trainedCount"] != len(trained):
            fail("本折拟合样本数量与trainedImageIds不一致", prefix + "/trainedCount")
        if fold["testCount"] != len(plan_folds[number]["testIds"]):
            fail("本折测试样本数量与划分方案不一致", prefix + "/testCount")
        for sample in trained:
            row = rows.get(sample)
            if row is None:
                fail("本折拟合样本不在来源特征表中", prefix + "/trainedImageIds")
            if row["label"] is None:
                fail("本折拟合样本缺少已知标签", prefix + "/trainedImageIds")
        if set(trained) != set(plan_folds[number]["trainIds"]):
            fail("本折声明的拟合样本与划分方案的训练集不一致", prefix + "/trainedImageIds")
    if declared_numbers != set(plan_folds):
        fail("折声明集合与划分方案的折不一致", "/fit/folds")


def validate_model_lineage(value, self_path, roots, identity="radstudio/model@1"):
    """Bind a model to the data it claims to have been fitted on, or refuse the artifact."""

    def fail(message, field):
        raise ContentContractError(message, field, identity)

    fold_form = is_fold_lineage(identity)
    image_form = is_image_lineage(identity)
    fit = value["fit"]
    features_reference = fit["features"]
    table = load_model_lineage(
        roots,
        features_reference,
        "feature_table",
        {"id": "radstudio/feature-table", "version": "1"},
        "/fit/features",
        fail,
    )
    if not image_form and table["featureNames"] != value["featureNames"]:
        fail("模型特征名与来源特征表顺序不一致", "/featureNames")
    rows = {row["imageId"]: row for row in table["records"]}
    trained = [] if fold_form or image_form else list(fit["trainedImageIds"])
    if not fold_form and not image_form:
        if fit["trainedCount"] != len(trained):
            fail("拟合样本数量与trainedImageIds不一致", "/fit/trainedCount")
        for sample in trained:
            row = rows.get(sample)
            if row is None:
                fail("拟合样本不在来源特征表中", "/fit/trainedImageIds")
            if row["label"] is None:
                fail("拟合样本缺少已知标签", "/fit/trainedImageIds")
    plan = load_model_lineage(
        roots,
        fit["split"],
        "split_plan",
        {"id": "radstudio/split-plan", "version": "1"},
        "/fit/split",
        fail,
    )
    if plan["source"] != features_reference:
        fail("划分并非取自声明的来源特征表", "/fit/split")
    validate_split_source(plan, table)
    if fold_form or image_form:
        if plan.get("scheme") == "holdout" or not isinstance(plan.get("folds"), list):
            fail("折内血缘要求交叉验证划分", "/fit/split")
        if image_form:
            validate_model_image_folds(fit["folds"], plan, rows, fail)
        else:
            validate_model_folds(
                fit["folds"], plan, rows, value["weights"]["path"], self_path, roots, fail
            )
    else:
        if plan.get("scheme") == "holdout":
            partitions = plan["partitions"]
            universe = (
                set(partitions["train"]) | set(partitions["validation"]) | set(partitions["test"])
            )
            held_out = set(partitions["test"])
        else:
            universe, held_out = set(), set()
            for fold in plan["folds"]:
                universe |= set(fold["trainIds"]) | set(fold["testIds"])
                held_out |= set(fold["testIds"])
        for sample in trained:
            if sample not in universe:
                fail("拟合样本不在划分全集内", "/fit/trainedImageIds")
        if plan.get("scheme") == "holdout" and held_out & set(trained):
            fail("留出测试集样本参与过拟合", "/fit/trainedImageIds")
    validate_model_weights(value["weights"], self_path, roots, "/weights", fail)


def validate_content_file(path, physical_type, contract, *, reference_roots=None):
    maximum = content_registry()["maxBytes"]
    with path.open("rb") as source:
        data = source.read(maximum + 1)
    evidence = validate_content_bytes(data, physical_type, contract)
    if physical_type == "split_plan":
        from .artifact_boundary import parse_reference, workspace_path

        value = decode_content_json(
            data,
            max_bytes=maximum,
            max_depth=content_registry()["maxDepth"],
            numeric_policy=content_registry()["numericPolicy"],
        )
        reference = parse_reference(value["source"])
        try:
            resolved = None
            for root in reference_roots or [path.parent]:
                candidate = workspace_path(root, reference["path"])
                if candidate.exists():
                    resolved = workspace_path(root, reference["path"], must_exist=True)
                    break
            if resolved is None:
                raise ContentContractError(
                    "划分来源文件缺失或哈希不匹配", "/source", "radstudio/split-plan@1"
                )
            with resolved.open("rb") as source:
                source_data = source.read(maximum + 1)
        except ContentContractError:
            raise
        except (ValueError, OSError) as error:
            raise ContentContractError(
                "划分来源路径无效或不可读取", "/source", "radstudio/split-plan@1"
            ) from error
        if (
            len(source_data) > maximum
            or hashlib.sha256(source_data).hexdigest() != reference["sha256"]
        ):
            raise ContentContractError(
                "划分来源文件超预算或哈希不匹配", "/source", "radstudio/split-plan@1"
            )
        validate_content_bytes(
            source_data, "feature_table", {"id": "radstudio/feature-table", "version": "1"}
        )
        table = decode_content_json(
            source_data,
            max_bytes=maximum,
            max_depth=content_registry()["maxDepth"],
            numeric_policy=content_registry()["numericPolicy"],
        )
        validate_split_source(value, table)
    if physical_type == "model":
        registry = content_registry()
        identity, _ = resolve_content_contract(physical_type, contract)
        value = decode_content_json(
            data,
            max_bytes=registry["maxBytes"],
            max_depth=registry["maxDepth"],
            numeric_policy=registry["numericPolicy"],
        )
        validate_model_lineage(
            value, path.resolve(), list(reference_roots or [path.parent]), identity
        )
    return evidence
