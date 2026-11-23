"""混合模型算子：CNN 倒数第二层特征提取（[dl] extra）。

``CNNFeatureExtraction`` 复用 ``DLCNNTraining`` 产出的五折 checkpoint：按
checkpoint 的 backbone/library/imageSize/numClasses 重建每折模型并加载权重，
在分类头（最后一个 ``torch.nn.Linear``）上注册 forward pre-hook，取分类头输入
并展平，得到 penultimate 特征；多折结果按折平均后输出 ``feature_table``，
供 FeatureFilter/LogisticRegression 等传统分类器使用。本算子不产出概率、不做分类。

torch/torchvision/monai 属于 [dl] extra：模块级不导入 torch，运行期缺失时给出
安装指引，与 ``operators_dl`` / ``operators_seg`` 的约定一致。
"""

from __future__ import annotations

import io

from . import operators_dl
from .execution_contract import execution_parameters
from .operator import Operator, operator
from .runner import read_image

_DEFAULT_BATCH_SIZE = 32
_MAX_BATCH_SIZE = 256


def _require_torch():
    try:
        import torch

        return torch
    except ImportError as exc:
        raise ImportError(
            "CNN feature extraction requires the [dl] extra: "
            "uv sync --extra dl (or pip install 'radstudio-sdk[dl]')"
        ) from exc


def _classifier_head(torch, model):
    """最后一个全连接分类头；跳过 Inception 辅助头（推理前向不参与）。"""
    head = None
    for name, module in model.named_modules():
        if isinstance(module, torch.nn.Linear) and "AuxLogits" not in name:
            head = module
    if head is None:
        raise ValueError("Model has no linear classifier head to hook")
    return head


def _capture_hook(captured):
    """forward pre-hook：留下分类头输入张量（已 flatten(1)）。"""

    def hook(_module, args):
        captured.clear()
        captured.append(args[0].flatten(1))

    return hook


def _fold_average_features(torch, models, captures, batch):
    """逐折前向取分类头输入，校验各折特征维度一致后按折求平均。"""
    per_fold = []
    with torch.no_grad():
        for model, captured in zip(models, captures):
            captured.clear()
            model(batch)
            if len(captured) != 1:
                raise ValueError("Classifier head forward hook captured no input")
            per_fold.append(captured[0])
    widths = {int(feature.shape[1]) for feature in per_fold}
    if len(widths) != 1:
        raise ValueError(f"Feature dimension differs between folds: {sorted(widths)}")
    return torch.stack(per_fold).mean(dim=0)


@operator("CNNFeatureExtraction", version="1.0.0")
class CNNFeatureExtractionOperator(Operator):
    """CNN 特征提取：取每折分类头输入的 penultimate 特征并按折平均。

    只输出特征表，不写概率、不做分类；唯一需要与训练保持一致的是图像读取、
    resize 与 ImageNet 归一化口径，因此直接复用 ``operators_dl`` 的实现。
    """

    def run(self, ctx):
        torch = _require_torch()
        p = execution_parameters(ctx)
        samples = ctx.input_json("samples")
        if not samples:
            raise ValueError("CNNFeatureExtraction requires samples")
        for sample in samples:
            if not sample.get("imagePath"):
                raise ValueError(
                    f"Sample {sample.get('imageId')} is missing imagePath"
                )
        meta = ctx.input_json("model")
        checkpoint_ref = meta.get("checkpointArtifact")
        if not checkpoint_ref:
            raise ValueError("Input model lacks checkpointArtifact (not a DL model)")
        checkpoint = torch.load(
            io.BytesIO(ctx.store.read_bytes(checkpoint_ref)),
            map_location="cpu",
            weights_only=True,
        )
        backbone = checkpoint["backbone"]
        library = checkpoint.get("library") or operators_dl._backbone_library(backbone)
        num_classes = int(checkpoint.get("numClasses") or 1)
        size = int(checkpoint.get("imageSize", 64))
        folds = checkpoint.get("folds") or []
        if not folds:
            raise ValueError("Checkpoint contains no fold weights")
        batch_size = int(p.get("batchSize", _DEFAULT_BATCH_SIZE))
        if not 1 <= batch_size <= _MAX_BATCH_SIZE:
            raise ValueError(f"batchSize must be in [1, {_MAX_BATCH_SIZE}]")

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device.type == "cpu":
            ctx.progress(0, "CUDA unavailable; extracting features on CPU")

        models, captures, handles = [], [], []
        for entry in folds:
            model = operators_dl._build_backbone(
                torch, backbone, False, library, num_outputs=num_classes
            ).to(device)
            model.load_state_dict(entry["state_dict"])
            model.eval()
            captured = []
            handles.append(
                _classifier_head(torch, model).register_forward_pre_hook(
                    _capture_hook(captured)
                )
            )
            models.append(model)
            captures.append(captured)

        width = None
        records = []
        try:
            for start in range(0, len(samples), batch_size):
                ctx.check_cancelled()
                chunk = samples[start : start + batch_size]
                batch = operators_dl._normalize_images(
                    torch,
                    [
                        operators_dl._image_to_tensor(
                            torch, read_image(ctx.store.resolve_path(s["imagePath"])), size
                        )
                        for s in chunk
                    ],
                    device,
                )
                matrix = _fold_average_features(torch, models, captures, batch)
                if width is None:
                    width = int(matrix.shape[1])
                elif int(matrix.shape[1]) != width:
                    raise ValueError("Feature dimension differs between folds")
                for sample, values in zip(chunk, matrix.detach().cpu().tolist()):
                    records.append(
                        {
                            "imageId": sample["imageId"],
                            "patientId": sample["patientId"],
                            "label": sample.get("label"),
                            "values": values,
                        }
                    )
                ctx.progress(
                    100.0 * (start + len(chunk)) / len(samples),
                    f"features {start + len(chunk)}/{len(samples)}",
                )
        finally:
            for handle in handles:
                handle.remove()

        table = {
            "featureNames": [f"penultimate_{index}" for index in range(width or 0)],
            "records": records,
            "source": {"backbone": backbone, "folds": len(models), "imageSize": size},
            "count": len(records),
        }
        return {"features": ctx.output_json("features.json", table, "feature_table")}
