"""Self-contained SDK source/resources plus frozen metadata; input data stays separate."""

import copy
import hashlib
import importlib.metadata
import io
import json
import re
import sys
import zipfile
from importlib.resources import files
from pathlib import Path, PurePosixPath

from .catalog_snapshot import resolve_request_catalog
from .json_contract import encode_contract_json, encode_runtime_json
from .replay import file_digest

REPRODUCE_SCRIPT = """import hashlib, json, sys
from pathlib import Path, PurePosixPath
base=Path(__file__).resolve().parent
for item in json.loads((base/'bundle-hashes.json').read_text(encoding='utf-8')):
 name=item['path']; relative=PurePosixPath(name)
 if relative.is_absolute() or ':' in name or '\\\\' in name or any(p in ('','.','..') for p in name.split('/')): raise ValueError('Invalid bundle path')
 path=(base/name).resolve()
 if not path.is_relative_to(base) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']: raise ValueError('Bundle integrity mismatch: '+name)
sys.path.insert(0,str(base))
from radstudio.replay import main
raise SystemExit(main(base))
"""


def _installed_distributions():
    return importlib.metadata.distributions()


def _portable_relative_path(raw):
    """归一到可移植相对路径；绝对路径、盘符、反斜杠与目录逃逸返回 None。"""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or "\\" in value or ":" in value:
        return None
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return None
    path = PurePosixPath(value)
    if path.is_absolute():
        return None
    return path


def _snapshot_node_segment(node_id):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", node_id).strip("._-")
    return f"{cleaned or 'node'}-{hashlib.sha256(node_id.encode('utf-8')).hexdigest()[:8]}"


def _resolve_snapshot_manifest_path(manifest_ref):
    """定位执行端暂存的快照 manifest：绝对路径直用，相对路径按工作区根解析。"""
    path = Path(manifest_ref)
    if path.is_absolute():
        return path
    candidates = [Path("/input") / path, Path.cwd() / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _resolve_snapshot_file_path(manifest_path, raw_path):
    """解析 manifest 文件条目：绝对路径直用，相对路径按工作区根解析。"""
    path = Path(str(raw_path or ""))
    if path.is_absolute():
        return path
    candidates = [Path("/input")]
    if manifest_path.parent.name == "snapshot":
        candidates.append(manifest_path.parent.parent)
    candidates.extend([manifest_path.parent, Path.cwd()])
    for candidate in candidates:
        resolved = candidate / path
        if resolved.is_file():
            return resolved
    return candidates[0] / path


def _export_snapshot_inputs(request, payloads, hashes):
    """把读取节点快照导出为可移植相对路径与哈希，并保留读取器配置/实现版本。

    只导出 manifest 声明的文件；manifest 重写为包内相对路径后重新记录哈希，
    未授权（未出现在 manifest 中）的文件不会被静默打包。
    """
    snapshot_inputs = request.get("snapshotInputs")
    if not snapshot_inputs:
        return
    if not isinstance(snapshot_inputs, dict):
        raise TypeError("snapshotInputs must be an object")
    exported = {item["path"] for item in hashes}
    for node_id in sorted(snapshot_inputs):
        entry = snapshot_inputs[node_id]
        if not isinstance(entry, dict):
            raise TypeError("Snapshot input entries must be objects")
        ref = entry.get("manifestPath")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("Snapshot input is missing its manifest path")
        manifest_path = _resolve_snapshot_manifest_path(ref)
        raw = manifest_path.read_bytes()
        expected = str(entry.get("manifestSha256") or "").strip().lower()
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Snapshot manifest hash mismatch: " + node_id)
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Snapshot manifest is not valid JSON: " + node_id) from error
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, dict):
            raise TypeError("Snapshot manifest is missing files: " + node_id)
        segment = _snapshot_node_segment(node_id)
        for file_id in sorted(files):
            file_entry = files[file_id]
            if not isinstance(file_entry, dict):
                raise TypeError("Snapshot file entries must be objects")
            source_path = _resolve_snapshot_file_path(manifest_path, file_entry.get("path"))
            if not source_path.is_file():
                raise ValueError("Snapshot input file is missing: " + file_id)
            relative = _portable_relative_path(file_entry.get("path")) or _portable_relative_path(
                file_id
            )
            if relative is None:
                raise ValueError("Snapshot input path cannot be exported portably: " + file_id)
            exported_path = str(PurePosixPath("inputs") / segment / relative)
            if exported_path in exported:
                raise ValueError("Duplicate snapshot input path: " + exported_path)
            digest = file_digest(source_path)
            declared = str(file_entry.get("sha256") or "").strip().lower()
            if declared and declared != digest:
                raise ValueError("Snapshot input content hash mismatch: " + file_id)
            exported.add(exported_path)
            hashes.append({"path": exported_path, "sha256": digest})
            file_entry["path"] = exported_path
            file_entry["sha256"] = digest
        encoded = encode_contract_json(manifest)
        bundle_path = "snapshot-inputs/" + segment + "/input-manifest.json"
        payloads[bundle_path] = encoded
        entry["manifestPath"] = bundle_path
        entry["manifestSha256"] = hashlib.sha256(encoded).hexdigest()
    request["snapshotInputs"] = snapshot_inputs


def build_reproduction(context, catalog_loader):
    original = context.request
    request = {
        key: copy.deepcopy(original[key])
        for key in ("runId", "ir", "dataset", "snapshotInputs", "imageDigest", "targetNodeId")
        if key in original
    }
    request["operatorCatalogSnapshot"] = resolve_request_catalog(original, catalog_loader)
    hashes = []
    for index, sample in enumerate((request.get("dataset") or {}).get("samples") or []):
        for key in ("imagePath", "roiPath"):
            value = sample.get(key)
            if not value:
                continue
            original_path = Path(value)
            suffix = (
                ".nii.gz"
                if original_path.name.lower().endswith(".nii.gz")
                else original_path.suffix
            )
            if suffix and not re.fullmatch(r"(\.[a-zA-Z0-9]{1,16}){1,2}", suffix):
                raise ValueError("Input extension cannot be exported portably")
            relative = f"{index}_{key}{suffix}"
            hashes.append({"path": relative, "sha256": file_digest(original_path)})
            sample[key] = relative
    # 读取节点快照：只导出 manifest 声明的文件，保留读取器配置与实现版本。
    payloads = {}
    _export_snapshot_inputs(request, payloads, hashes)
    payloads.update(
        {
            "request.json": encode_runtime_json(request),
            "input-hashes.json": encode_contract_json(hashes),
            "operator-catalog.json": encode_contract_json(request["operatorCatalogSnapshot"]),
            "reproduce.py": REPRODUCE_SCRIPT.encode("utf-8"),
        }
    )
    deps = sorted(
        f"{name}=={item.version}"
        for item in _installed_distributions()
        if (name := item.metadata.get("Name"))
        and name.lower()
        not in {
            "radstudio-sdk",
            "pip",
            "setuptools",
            "pytest",
            "pytest-cov",
            "ruff",
            "coverage",
            "iniconfig",
            "pluggy",
            "pygments",
        }
    )
    from .pyradiomics_backend import PYRADIOMICS_REQUIREMENT

    deps = [
        PYRADIOMICS_REQUIREMENT if item.lower().startswith("pyradiomics==") else item
        for item in deps
    ]
    payloads["requirements.lock"] = ("\n".join(deps) + "\n").encode("utf-8")
    payloads["README.md"] = (
        "RadStudio SDK sequential replay bundle. Python "
        + sys.version.split()[0]
        + ".\nUse a compatible approved environment; requirements.lock observes installed versions, without distribution hashes or cloud qualification.\nInputs are intentionally separate: restore exact input-hashes.json names in --dataset-dir, then run python reproduce.py --dataset-dir PATH --output PATH.\nReader-node snapshot manifests are frozen under snapshot-inputs/<node>/input-manifest.json with reader config and implementation versions; only files declared by those manifests are listed in input-hashes.json.\nFrozen operator-catalog.json is used without reading the current installed catalog. SDK source and JSON schemas are included. No runtime pip install occurs.\nBundle hashes detect accidental changes, not malicious replacement of this bundle or its manifest. Declared image identity does not prove local/FC environment identity. No independent IBSI validation is claimed.\n"
    ).encode("utf-8")
    package = files("radstudio")
    for path in sorted(package.iterdir(), key=lambda item: item.name):
        if path.is_file() and path.name.endswith(".py"):
            payloads["radstudio/" + path.name] = path.read_bytes()
    for path in sorted(package.joinpath("schemas").iterdir(), key=lambda item: item.name):
        if path.is_file() and path.name.endswith(".json"):
            payloads["radstudio/schemas/" + path.name] = path.read_bytes()
    definitions = [
        entry["definition"] for entry in request["operatorCatalogSnapshot"]["nodes"].values()
    ]
    unique = {(item["type"], item["version"]): item for item in definitions}
    payloads["radstudio/operators.json"] = encode_contract_json(
        [unique[key] for key in sorted(unique)]
    )
    payloads["provenance.json"] = encode_runtime_json(
        {
            "imageDigest": request.get("imageDigest"),
            "catalogDigest": request["operatorCatalogSnapshot"]["digest"],
            "workflowDigest": hashlib.sha256(
                encode_contract_json(request["ir"], canonical=True)
            ).hexdigest(),
            "catalogSource": request["operatorCatalogSnapshot"]["source"],
            "python": sys.version,
            "artifacts": context.manifest,
            "implementations": getattr(context, "implementations", {}),
            "executionReceipts": getattr(context, "execution_receipts", {}),
        }
    )
    payloads["bundle-hashes.json"] = encode_contract_json(
        [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(payloads.items())
        ]
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()
