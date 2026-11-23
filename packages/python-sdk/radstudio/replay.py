"""Local replay from a verified bundle and separately provided input files."""

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path, PurePosixPath


def file_digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def bounded_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Invalid relative input path")
    parts = PurePosixPath(relative)
    if (
        parts.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or ":" in relative
    ):
        raise ValueError("Input path escapes dataset directory")
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Missing input or path escapes dataset directory: " + relative)
    return path


def _materialize_snapshot_inputs(bundle_dir, request, verified, consumed, workspace):
    """把包内快照 manifest 的文件路径改写为已校验的本地绝对路径。

    包内 manifest 只有相对路径与哈希；重写后重新计算 manifest 哈希，执行端
    仍按 manifestSha256 校验。未出现在哈希清单中的文件不会被静默读取。
    """
    snapshot_inputs = request.get("snapshotInputs") or {}
    if not isinstance(snapshot_inputs, dict):
        raise TypeError("Invalid snapshotInputs")
    for index, node_id in enumerate(sorted(snapshot_inputs)):
        entry = snapshot_inputs[node_id]
        if not isinstance(entry, dict):
            raise TypeError("Invalid snapshot input entry")
        ref = entry.get("manifestPath")
        if not isinstance(ref, str) or not ref:
            raise ValueError("Snapshot input is missing its manifest path")
        manifest_path = bounded_path(bundle_dir, ref)
        raw = manifest_path.read_bytes()
        expected = str(entry.get("manifestSha256") or "").strip().lower()
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Snapshot manifest hash mismatch: " + node_id)
        manifest = json.loads(raw.decode("utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, dict):
            raise TypeError("Snapshot manifest is missing files: " + node_id)
        for file_id in sorted(files):
            item = files[file_id]
            relative = item.get("path") if isinstance(item, dict) else None
            if relative not in verified:
                raise ValueError("Input is not bound to hash manifest: " + str(relative))
            item["path"] = str(verified[relative])
            consumed.add(relative)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", node_id) or "node"
        target = Path(workspace) / f"{index}-{safe}" / "input-manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        target.write_bytes(data)
        entry["manifestPath"] = str(target)
        entry["manifestSha256"] = hashlib.sha256(data).hexdigest()
    request["snapshotInputs"] = snapshot_inputs


def main(bundle_dir, argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--output", default="output")
    args = parser.parse_args(argv)
    from .catalog_snapshot import load_runner_request
    from .runner import run

    base = Path(bundle_dir)
    request = load_runner_request((base / "request.json").read_bytes())
    hashes = json.loads((base / "input-hashes.json").read_text(encoding="utf-8"))
    verified = {}
    for item in hashes:
        relative, digest = item["path"], item["sha256"]
        if (
            relative in verified
            or not isinstance(digest, str)
            or not re.fullmatch("[0-9a-f]{64}", digest)
        ):
            raise ValueError("Invalid or duplicate input manifest entry")
        path = bounded_path(args.dataset_dir, relative)
        if file_digest(path) != digest:
            raise ValueError("Input hash mismatch: " + relative)
        verified[relative] = path
    consumed = set()
    for sample in (request.get("dataset") or {}).get("samples") or []:
        for key in ("imagePath", "roiPath"):
            relative = sample.get(key)
            if not relative:
                continue
            if relative not in verified:
                raise ValueError("Input is not bound to hash manifest: " + str(relative))
            sample[key] = str(verified[relative])
            consumed.add(relative)
    with tempfile.TemporaryDirectory(prefix="radstudio-replay-") as workspace:
        _materialize_snapshot_inputs(base, request, verified, consumed, workspace)
        if consumed != set(verified):
            raise ValueError("Input manifest contains unbound files")
        run(request, Path(args.output))
    return 0
