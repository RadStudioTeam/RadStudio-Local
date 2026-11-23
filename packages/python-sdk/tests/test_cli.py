import csv
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from radstudio import cli
from radstudio.local_data import (
    LocalDataError,
    build_runner_request,
    discover_dataset,
    find_reader_node,
    load_workflow,
    write_manifest,
)


def template_path() -> Path:
    return Path(__file__).parents[3] / "packages/workflow-schema/examples/default-workflow.json"


def make_dataset(root: Path, *, mask_names=("case1", "case2")) -> Path:
    data = root / "data"
    images = data / "images"
    masks = data / "masks"
    images.mkdir(parents=True)
    masks.mkdir()
    rng = np.random.default_rng(7)
    rows = []
    for index, name in enumerate(mask_names):
        image = rng.integers(0, 255, (8, 8), dtype=np.uint8)
        mask = np.zeros((8, 8), dtype=np.uint8)
        mask[2:6, 2:6] = 255
        Image.fromarray(image).save(images / f"{name}.png")
        Image.fromarray(mask).save(masks / f"{name}.png")
        rows.append([name, str(index % 2), f"p{index}"])
    with (data / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_id", "label", "patient_id"])
        writer.writerows(rows)
    return data


def test_discover_dataset_builds_read_images_manifest(tmp_path):
    data = make_dataset(tmp_path)
    ir = load_workflow(template_path())
    reader = find_reader_node(ir)
    manifest = discover_dataset(data, ir=ir, reader_node=reader)
    assert len(manifest["rows"]) == 2
    assert len(manifest["files"]) == 4
    assert manifest["config"]["labelField"] == "labelValue"
    assert manifest["config"]["positiveValue"] == "1"
    assert manifest["config"]["negativeValue"] == "0"
    fields = manifest["rows"][0]["fields"]
    assert fields["patientId"] == "p0"
    assert fields["labelValue"] in {"0", "1"}


def test_run_read_images_node_end_to_end(tmp_path):
    from radstudio import runner

    data = make_dataset(tmp_path)
    ir = load_workflow(template_path())
    reader = find_reader_node(ir)
    manifest = discover_dataset(data, ir=ir, reader_node=reader)
    out = tmp_path / "out"
    manifest_path, manifest_hash = write_manifest(manifest, out)
    request = build_runner_request(
        ir,
        reader_node_id="data_input",
        manifest_path=manifest_path,
        manifest_sha256=manifest_hash,
        run_id="test-run",
        target_node="data_input",
    )
    result = runner.run(request, out, lambda _t, _p: None)
    payload = json.loads((out / result["data_input"]["samples"]["path"]).read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert {sample["imageId"] for sample in payload} == {"case1", "case2"}
    assert all(Path(sample["imagePath"]).is_file() for sample in payload)
    assert {sample["label"] for sample in payload} == {0, 1}


def test_cli_run_and_report(tmp_path, capsys):
    data = make_dataset(tmp_path)
    out = tmp_path / "out"
    code = cli.main(
        [
            "run",
            str(template_path()),
            "--data",
            str(data),
            "--out",
            str(out),
            "--target-node",
            "data_input",
            "--quiet",
        ]
    )
    assert code == 0
    assert (out / "events.jsonl").is_file()
    assert (out / "_inputs" / "request.json").is_file()
    assert (out / "_inputs" / "read-images-manifest.json").is_file()
    report_code = cli.main(["report", str(out)])
    assert report_code == 0
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "RadStudio Local" in html
    assert "test" in html or "local-" in html


def test_flat_directory_pairs_mask_suffix(tmp_path):
    data = tmp_path / "flat"
    data.mkdir()
    rng = np.random.default_rng(3)
    Image.fromarray(rng.integers(0, 255, (8, 8), dtype=np.uint8)).save(data / "case1.png")
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[1:7, 1:7] = 1
    Image.fromarray(mask * 255).save(data / "case1_mask.png")
    (data / "labels.csv").write_text("sample_id,label\ncase1,1\n", encoding="utf-8")
    ir = load_workflow(template_path())
    reader = find_reader_node(ir)
    manifest = discover_dataset(data, ir=ir, reader_node=reader)
    assert len(manifest["rows"]) == 1
    assert len(manifest["files"]) == 2


def test_missing_mask_is_actionable(tmp_path):
    data = make_dataset(tmp_path)
    (data / "masks" / "case2.png").unlink()
    ir = load_workflow(template_path())
    reader = find_reader_node(ir)
    with pytest.raises(LocalDataError, match="没有配到掩膜"):
        discover_dataset(data, ir=ir, reader_node=reader)
    manifest = discover_dataset(data, ir=ir, reader_node=reader, allow_missing_roi=True)
    assert len(manifest["rows"]) == 2


def test_doctor_reports_environment():
    assert cli.main(["doctor"]) == 0
