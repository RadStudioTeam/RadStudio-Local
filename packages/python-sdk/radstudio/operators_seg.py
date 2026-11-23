"""分割算子：MONAI UNet 训练/推理、预测掩膜桥接与分割质量评估。

论文复现的两阶段流程里，第二阶段要用自动分割的掩膜替代真值掩膜再走影像组学：
``SegUNetTraining`` 产出逐折 UNet 权重，``SegUNetInference`` 把样本切成二值掩膜，
``SegMaskApply`` 把掩膜写回样本的 ``roiPath``（原真值保留到 ``groundTruthRoiPath``），
``SegmentationEvaluation`` 用真值掩膜对预测掩膜算 Dice/IoU/精确率/召回率/HD95。

torch/MONAI 属于 [dl] extra：模块本身只依赖 numpy/PIL，注册不受影响，运行期
导入失败时给出安装指引，与 ``operators_dl`` 的约定一致。
"""

from __future__ import annotations

import copy
import io
from pathlib import Path

import numpy as np
from PIL import Image

from .artifact_boundary import parse_reference
from .execution_contract import execution_parameters
from .operator import Operator, operator
from .runner import identity, read_image

_SEED = 42
_BACKBONES = ("unet",)
_LOSSES = ("dice", "bce", "dice_bce")
_AUGMENTATIONS = ("none", "light", "standard")
_SIZES = (64, 128, 256, 512)
_MEDSAM_SIZES = (512, 1024)


def _require_torch():
    try:
        import torch

        return torch
    except ImportError as exc:
        raise ImportError(
            "Segmentation operators require the [dl] extra: "
            "uv sync --extra dl (or pip install 'radstudio-sdk[dl]')"
        ) from exc


def _require_monai():
    try:
        import monai

        return monai
    except ImportError as exc:
        raise ImportError(
            "Segmentation operators require MONAI in the [dl] extra: "
            "uv sync --extra dl (or pip install 'radstudio-sdk[dl]')"
        ) from exc


def _image_tensor(torch, image, size):
    """2D 灰度 → resize → [0,1] 的单通道 CHW 张量。"""
    lo, hi = float(image.min()), float(image.max())
    scaled = (image - lo) / max(hi - lo, 1e-12)
    tensor = torch.from_numpy(scaled.astype(np.float32))[None, None]
    tensor = torch.nn.functional.interpolate(
        tensor, size=(size, size), mode="bilinear", align_corners=False
    )
    return tensor[0]


def _mask_tensor(torch, mask, size):
    """二值掩膜 → resize（最近邻）→ 0/1 的单通道 CHW 张量。"""
    tensor = torch.from_numpy((mask > 0).astype(np.float32))[None, None]
    tensor = torch.nn.functional.interpolate(tensor, size=(size, size), mode="nearest")
    return tensor[0]


def _build_unet(torch, backbone):
    if backbone not in _BACKBONES:
        raise ValueError(f"Unknown segmentation backbone: {backbone}")
    _require_monai()
    from monai.networks.nets import UNet

    return UNet(
        spatial_dims=2,
        in_channels=1,
        out_channels=1,
        channels=(16, 32, 64, 128),
        strides=(2, 2, 2),
        num_res_units=2,
        norm="batch",
    )


def _seg_loss(torch, name):
    if name not in _LOSSES:
        raise ValueError(f"Unknown segmentation loss: {name}")
    _require_monai()
    from monai.losses import DiceLoss

    dice = DiceLoss(sigmoid=True, squared_pred=True, reduction="mean")
    if name == "dice":
        return dice
    bce = torch.nn.BCEWithLogitsLoss()

    def combined(logits, target):
        return dice(logits, target) + bce(logits, target)

    return combined if name == "dice_bce" else bce


def _augment_pair(torch, image, mask, preset, generator):
    """影像与掩膜同步增强：翻转、90° 旋转、缩放（掩膜用最近邻）。"""
    if preset == "none":
        return image, mask
    if torch.rand(1, generator=generator).item() < 0.5:
        image, mask = torch.flip(image, dims=(2,)), torch.flip(mask, dims=(2,))
    if torch.rand(1, generator=generator).item() < 0.5:
        image, mask = torch.flip(image, dims=(1,)), torch.flip(mask, dims=(1,))
    if preset == "standard":
        if torch.rand(1, generator=generator).item() < 0.5:
            turns = int(torch.randint(1, 4, (1,), generator=generator).item())
            image, mask = torch.rot90(image, turns, dims=(1, 2)), torch.rot90(
                mask, turns, dims=(1, 2)
            )
        if torch.rand(1, generator=generator).item() < 0.4:
            scale = float(torch.empty(1).uniform_(0.9, 1.1, generator=generator).item())
            size = image.shape[-1]
            target = max(8, round(size * scale))
            image = torch.nn.functional.interpolate(
                image[None], size=(target, target), mode="bilinear", align_corners=False
            )[0]
            mask = torch.nn.functional.interpolate(mask[None], size=(target, target), mode="nearest")[0]
    return image, mask


def _patient_holdout(groups, rng):
    """折内按患者划出 20% 做早停验证，返回 (保留掩码, 验证掩码)。"""
    unique = sorted(set(groups.tolist()))
    rng.shuffle(unique)
    count = max(1, round(len(unique) * 0.2))
    held = set(unique[:count])
    mask = np.array([group in held for group in groups])
    if mask.all() or not mask.any():
        mask[:] = False
        mask[: max(1, len(mask) // 5)] = True
    return ~mask, mask


def _dice(prediction, target):
    prediction = prediction.astype(bool)
    target = target.astype(bool)
    denominator = prediction.sum() + target.sum()
    if denominator == 0:
        return 1.0
    return float(2.0 * np.logical_and(prediction, target).sum() / denominator)


def _iou(prediction, target):
    prediction = prediction.astype(bool)
    target = target.astype(bool)
    union = np.logical_or(prediction, target).sum()
    if union == 0:
        return 1.0
    return float(np.logical_and(prediction, target).sum() / union)


def _surface(mask):
    from scipy.ndimage import binary_erosion

    binary = mask.astype(bool)
    return binary & ~binary_erosion(binary)


def _hd95(prediction, target):
    """95% 豪斯多夫距离（2D 欧氏距离，边界点双向距离的 95 分位）。"""
    from scipy.ndimage import distance_transform_edt

    predicted_surface = _surface(prediction)
    target_surface = _surface(target)
    if not predicted_surface.any() or not target_surface.any():
        return None
    forward = distance_transform_edt(~target_surface)[predicted_surface]
    backward = distance_transform_edt(~predicted_surface)[target_surface]
    return float(np.percentile(np.concatenate([forward, backward]), 95))


def _nsd(prediction, target, tolerance=2.0):
    """对称归一化表面距离：边界点在对方容差内的比例，双向平均。"""
    from scipy.ndimage import distance_transform_edt

    predicted_surface = _surface(prediction)
    target_surface = _surface(target)
    if not predicted_surface.any() or not target_surface.any():
        return None
    distance_to_target = distance_transform_edt(~target_surface)
    distance_to_predicted = distance_transform_edt(~predicted_surface)
    overlap_pred = int((distance_to_target[predicted_surface] <= tolerance).sum())
    overlap_truth = int((distance_to_predicted[target_surface] <= tolerance).sum())
    return float(
        (overlap_pred + overlap_truth) / (predicted_surface.sum() + target_surface.sum())
    )


def _load_pair(ctx, sample, size, torch):
    image = read_image(ctx.store.resolve_path(sample["imagePath"]))
    mask = read_image(ctx.store.resolve_path(sample["roiPath"]))
    if image.shape != mask.shape:
        raise ValueError(f"Image and ROI shape mismatch for sample {sample['imageId']}")
    return _image_tensor(torch, image, size), _mask_tensor(torch, mask, size)


def _sample_identity(samples):
    return identity(
        {
            "records": [
                {
                    "imageId": sample["imageId"],
                    "patientId": sample["patientId"],
                    "label": sample.get("label"),
                }
                for sample in samples
            ]
        }
    )


@operator("SegUNetTraining", version="1.0.0")
class SegUNetTrainingOperator(Operator):
    """UNet 分割训练：按 split_plan 逐折训练，折内患者分组划出早停验证集。"""

    def run(self, ctx):
        torch = _require_torch()
        _require_monai()
        p = execution_parameters(ctx)
        samples = ctx.input_json("samples")
        if not samples:
            raise ValueError("SegUNetTraining requires samples")
        split = ctx.input_json("split")
        if split.get("identityHash") != _sample_identity(samples):
            raise ValueError("Split identity hash does not match the sample collection")
        folds = [fold for fold in split.get("folds", []) if fold.get("fold") in set(
            p.get("foldIndices") or [fold.get("fold") for fold in split.get("folds", [])]
        )]
        if not folds:
            raise ValueError("No folds selected for training")

        size = int(p.get("imageSize", 128))
        if size not in _SIZES:
            raise ValueError(f"imageSize must be one of {_SIZES}")
        loss_name = str(p.get("loss") or "dice_bce")
        augmentation = str(p.get("augmentation") or "none")
        if augmentation not in _AUGMENTATIONS:
            raise ValueError(f"Unknown augmentation preset: {augmentation}")
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device.type == "cpu":
            ctx.progress(0, "CUDA unavailable; training on CPU")

        ids = [sample["imageId"] for sample in samples]
        by_id = {image_id: index for index, image_id in enumerate(ids)}
        groups = np.asarray([sample["patientId"] for sample in samples])
        images, masks = [], []
        for sample in samples:
            image, mask = _load_pair(ctx, sample, size, torch)
            images.append(image)
            masks.append(mask)

        fold_states, fold_meta, predictions = [], [], []
        for local, fold in enumerate(folds):
            train_idx = [by_id[image_id] for image_id in fold["trainIds"]]
            test_idx = [by_id[image_id] for image_id in fold["testIds"]]
            if set(groups[train_idx]) & set(groups[test_idx]):
                raise ValueError("Patient leakage in split")
            torch.manual_seed(_SEED)
            model = _build_unet(torch, p.get("backbone", "unet")).to(device)
            state, epochs = _train_fold(
                torch,
                model,
                images,
                masks,
                groups,
                train_idx,
                p,
                device,
                ctx.check_cancelled,
                augmentation,
                loss_name,
            )
            model.load_state_dict(state)
            model.eval()
            scores = []
            with torch.no_grad():
                for index in test_idx:
                    logits = model(images[index][None].to(device))
                    probability = torch.sigmoid(logits)[0, 0].cpu().numpy()
                    predicted = probability >= float(p.get("threshold", 0.5))
                    truth = masks[index][0].cpu().numpy() > 0.5
                    dice, iou = _dice(predicted, truth), _iou(predicted, truth)
                    predictions.append(
                        {
                            "imageId": ids[index],
                            "patientId": samples[index]["patientId"],
                            "fold": fold["fold"],
                            "dice": dice,
                            "iou": iou,
                        }
                    )
                    scores.append((dice, iou))
            fold_states.append(
                {
                    "fold": fold["fold"],
                    "state_dict": state,
                    "trainCount": len(train_idx),
                    "testCount": len(test_idx),
                    "trainIds": [samples[index]["imageId"] for index in train_idx],
                    "testIds": [samples[index]["imageId"] for index in test_idx],
                    "epochs": epochs,
                }
            )
            fold_meta.append(
                {
                    "fold": fold["fold"],
                    "trainCount": len(train_idx),
                    "testCount": len(test_idx),
                    "epochs": epochs,
                    "meanDice": float(np.mean([item[0] for item in scores])),
                    "meanIou": float(np.mean([item[1] for item in scores])),
                }
            )
            ctx.progress(
                100.0 * (local + 1) / len(folds), f"seg fold {fold['fold'] + 1}/{len(folds)}"
            )

        checkpoint = {
            "format": "radstudio.seg.checkpoint.v1",
            "backbone": p.get("backbone", "unet"),
            "imageSize": size,
            "loss": loss_name,
            "augmentation": augmentation,
            "seed": _SEED,
            "device": device.type,
            "folds": fold_states,
        }
        buffer = io.BytesIO()
        torch.save(checkpoint, buffer)
        checkpoint_ref = ctx.output_bytes("seg-checkpoint.pt", buffer.getvalue(), "seg_model")
        model_meta = {
            "kind": "seg_unet_ensemble",
            "estimator": "seg_unet",
            "backbone": p.get("backbone", "unet"),
            "imageSize": size,
            "loss": loss_name,
            "augmentation": augmentation,
            "seed": _SEED,
            "threshold": float(p.get("threshold", 0.5)),
            "folds": fold_meta,
            "checkpointArtifact": checkpoint_ref,
            "metrics": {
                "meanDice": float(np.mean([item["dice"] for item in predictions])),
                "meanIou": float(np.mean([item["iou"] for item in predictions])),
                "count": len(predictions),
            },
        }
        return {
            "model": ctx.output_json("seg-model.json", model_meta, "seg_model"),
            "predictions": ctx.output_json(
                "seg-predictions.json",
                {"records": predictions, "identityHash": _sample_identity(samples)},
                "seg_prediction_table",
            ),
        }


def _train_fold(
    torch, model, images, masks, groups, train_idx, params, device, cancel_check, augmentation, loss_name
):
    """单折训练：Dice/BCE 组合损失 + 折内患者分组早停，返回 (最佳权重, 轮数)。"""
    epochs = int(params.get("epochs", 20))
    batch_size = max(1, int(params.get("batchSize", 4)))
    learning_rate = float(params.get("learningRate", 0.001))
    patience = int(params.get("earlyStopping", 3))
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_fn = _seg_loss(torch, loss_name)
    generator = torch.Generator().manual_seed(_SEED)

    fit_mask, val_mask = _patient_holdout(groups[train_idx], np.random.default_rng(_SEED))
    fit_idx = [index for index, keep in zip(train_idx, fit_mask) if keep]
    val_idx = [index for index, keep in zip(train_idx, val_mask) if keep]

    def forward(indices, augment):
        batch_images, batch_masks = [], []
        for index in indices:
            image, mask = images[index], masks[index]
            if augment:
                image, mask = _augment_pair(torch, image, mask, augmentation, generator)
            batch_images.append(image)
            batch_masks.append(mask)
        return (
            torch.stack(batch_images).to(device),
            torch.stack(batch_masks).to(device),
        )

    best_state = {key: value.clone() for key, value in model.state_dict().items()}
    best_loss = float("inf")
    stale = 0
    epoch = 0
    for epoch in range(1, epochs + 1):
        model.train()
        order = torch.randperm(len(fit_idx), generator=generator).tolist()
        for start in range(0, len(order), batch_size):
            if cancel_check is not None:
                cancel_check()
            indices = [fit_idx[position] for position in order[start : start + batch_size]]
            xb, yb = forward(indices, True)
            optimizer.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            optimizer.step()
        if val_idx and patience:
            model.eval()
            with torch.no_grad():
                xb, yb = forward(val_idx, False)
                value = float(loss_fn(model(xb), yb))
            if value < best_loss:
                best_loss = value
                stale = 0
                best_state = {key: tensor.clone() for key, tensor in model.state_dict().items()}
            else:
                stale += 1
                if stale >= patience:
                    break
    return best_state, epoch


@operator("SegUNetInference", version="1.0.0")
class SegUNetInferenceOperator(Operator):
    """UNet 分割推理：逐样本优先用其测试折模型（OOF），无覆盖时退回未见该样本的折平均。"""

    def run(self, ctx):
        torch = _require_torch()
        _require_monai()
        p = ctx.parameters
        samples = ctx.input_json("samples")
        if not samples:
            raise ValueError("SegUNetInference requires samples")
        meta = ctx.input_json("model")
        checkpoint_ref = meta.get("checkpointArtifact")
        if not checkpoint_ref:
            raise ValueError("Input model lacks checkpointArtifact (not a segmentation model)")
        checkpoint = torch.load(
            io.BytesIO(ctx.store.read_bytes(checkpoint_ref)), map_location="cpu", weights_only=True
        )
        size = int(checkpoint.get("imageSize", 128))
        threshold = float(p.get("threshold", meta.get("threshold", 0.5)))
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [0, 1]")
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        models = []
        for entry in checkpoint["folds"]:
            model = _build_unet(torch, checkpoint.get("backbone", "unet")).to(device)
            model.load_state_dict(entry["state_dict"])
            model.eval()
            models.append(model)
        # OOF 优先：样本落在某个测试折时只用该折模型；否则取未见该样本的折平均，
        # 都没有退回到全部折 —— 否则下游分类器的测试折患者信息会从掩膜里漏进来。
        test_sets = [set(entry.get("testIds") or []) for entry in checkpoint["folds"]]
        train_sets = [set(entry.get("trainIds") or []) for entry in checkpoint["folds"]]

        entries = []
        out_of_fold = 0
        for position, sample in enumerate(samples):
            ctx.check_cancelled()
            image = read_image(ctx.store.resolve_path(sample["imagePath"]))
            tensor = _image_tensor(torch, image, size)[None].to(device)
            image_id = sample["imageId"]
            chosen = [index for index, ids in enumerate(test_sets) if image_id in ids]
            if chosen:
                out_of_fold += 1
            else:
                chosen = [
                    index for index, ids in enumerate(train_sets) if image_id not in ids
                ]
            if not chosen:
                chosen = list(range(len(models)))
            probability = torch.zeros((size, size), device=device)
            with torch.no_grad():
                for index in chosen:
                    probability += torch.sigmoid(models[index](tensor))[0, 0]
            probability = (probability / len(chosen)).cpu().numpy()
            binary = (probability >= threshold).astype(np.uint8) * 255
            if binary.shape != image.shape:
                binary = np.asarray(
                    Image.fromarray(binary).resize(
                        (image.shape[1], image.shape[0]), resample=Image.NEAREST
                    )
                )
            buffer = io.BytesIO()
            Image.fromarray(binary).save(buffer, format="PNG")
            reference = ctx.output_bytes(
                f"masks/{sample['imageId']}.png", buffer.getvalue(), "mask"
            )
            entries.append(
                {
                    "imageId": sample["imageId"],
                    "patientId": sample.get("patientId"),
                    "file": parse_reference(reference)["path"],
                    # 完整产物引用：编排器按 ref 字符串把掩膜文件暂存进下游工作区
                    # （纯路径不在 imagePath/roiPath 白名单，文件不会跨节点传递）。
                    "fileRef": reference,
                    "foregroundRatio": float((binary > 0).mean()),
                }
            )
            ctx.progress(
                100.0 * (position + 1) / len(samples), f"masks {position + 1}/{len(samples)}"
            )
        return {
            "masks": ctx.output_json(
                "mask-index.json",
                {
                    "threshold": threshold,
                    "imageSize": size,
                    "folds": len(models),
                    "count": len(entries),
                    "outOfFoldCount": out_of_fold,
                    "entries": entries,
                },
                "mask_collection",
            )
        }


@operator("SegMaskApply", version="1.0.0")
class SegMaskApplyOperator(Operator):
    """把预测掩膜写回样本 ROI：全空/缺失掩膜的样本标记 blocked，不进入特征提取。"""

    def run(self, ctx):
        p = ctx.parameters
        samples = copy.deepcopy(ctx.input_json("samples"))
        index = ctx.input_json("masks")
        by_id = {entry["imageId"]: entry for entry in index.get("entries", [])}
        applied = blocked = 0
        for sample in samples:
            entry = by_id.get(sample["imageId"])
            if entry is None or not entry.get("file"):
                sample["qcStatus"] = "blocked"
                sample["roiSource"] = "predicted"
                blocked += 1
                continue
            path = ctx.store.resolve_path(entry["file"])
            if not path.exists():
                sample["qcStatus"] = "blocked"
                sample["roiSource"] = "predicted"
                blocked += 1
                continue
            mask = read_image(path)
            if not np.any(mask > 0):
                sample["qcStatus"] = "blocked"
                sample["roiSource"] = "predicted"
                blocked += 1
                continue
            if p.get("keepOriginalRoi", True) and sample.get("roiPath"):
                sample["groundTruthRoiPath"] = sample["roiPath"]
            sample["roiPath"] = entry["file"]
            sample["roiSource"] = "predicted"
            applied += 1
        return {
            "samples": ctx.output_json("samples.json", samples, "sample_collection"),
            "report": ctx.output_json(
                "mask-apply.json",
                {"applied": applied, "blocked": blocked, "count": len(samples)},
                "report",
            ),
        }


def _summarize(values, seed=_SEED, resamples=1000):
    """均值/中位数/IQR/95% CI（bootstrap 百分位法）。"""
    array = np.asarray(values, dtype=float)
    if not array.size:
        return None
    generator = np.random.default_rng(seed)
    draws = generator.integers(0, array.size, size=(resamples, array.size))
    means = array[draws].mean(axis=1)
    return {
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "q1": float(np.percentile(array, 25)),
        "q3": float(np.percentile(array, 75)),
        "ci95": [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))],
        "count": int(array.size),
    }


def _evaluate_segmentation(ctx, *, with_nsd):
    """共享评估主体；1.0.0 不计算 NSD，1.1.0 逐样本记录并汇总 NSD。

    1.0.0 的输出结构（records 与 metrics 的键）必须保持不变，因此 NSD 相关字段
    全部由 with_nsd 控制，不得影响既有 Dice/IoU/精确率/召回率/HD95 口径。
    """
    p = ctx.parameters
    samples = ctx.input_json("samples")
    index = ctx.input_json("masks")
    threshold = float(p.get("threshold", 0.5))
    resamples = int(p.get("bootstrap", 1000))
    tolerance = float(p.get("nsdTolerance", 2.0))
    if with_nsd and not 0.5 <= tolerance <= 20.0:
        raise ValueError("nsdTolerance must be in [0.5, 20]")
    by_id = {entry["imageId"]: entry for entry in index.get("entries", [])}

    records = []
    empty_truth = []
    missing = []
    for sample in samples:
        entry = by_id.get(sample["imageId"])
        if not sample.get("roiPath") or entry is None or not entry.get("file"):
            missing.append(sample["imageId"])
            continue
        truth = read_image(ctx.store.resolve_path(sample["roiPath"])) > 0
        prediction = read_image(ctx.store.resolve_path(entry["file"])) > 0
        if prediction.shape != truth.shape:
            prediction = np.asarray(
                Image.fromarray(prediction.astype(np.uint8) * 255).resize(
                    (truth.shape[1], truth.shape[0]), resample=Image.NEAREST
                )
            ) > 0
        if not truth.any():
            empty_truth.append(sample["imageId"])
            continue
        tp = int(np.logical_and(prediction, truth).sum())
        fp = int(np.logical_and(prediction, ~truth).sum())
        fn = int(np.logical_and(~prediction, truth).sum())
        record = {
            "imageId": sample["imageId"],
            "patientId": sample.get("patientId"),
            "dice": _dice(prediction, truth),
            "iou": _iou(prediction, truth),
            "precision": float(tp / (tp + fp)) if tp + fp else None,
            "recall": float(tp / (tp + fn)) if tp + fn else None,
            "hd95": _hd95(prediction, truth),
        }
        if with_nsd:
            record["nsd"] = _nsd(prediction, truth, tolerance)
        records.append(record)
    if not records:
        raise ValueError("No sample has both a non-empty ground truth ROI and a predicted mask")

    def collect(key):
        return [row[key] for row in records if row.get(key) is not None]

    metrics = {
        "threshold": threshold,
        "dice": _summarize(collect("dice"), resamples=resamples),
        "iou": _summarize(collect("iou"), resamples=resamples),
        "precision": _summarize(collect("precision"), resamples=resamples),
        "recall": _summarize(collect("recall"), resamples=resamples),
        "hd95": _summarize(collect("hd95"), resamples=resamples),
        "count": len(records),
        "emptyGroundTruth": empty_truth,
        "missingMasks": missing,
    }
    if with_nsd:
        metrics["nsd"] = _summarize(collect("nsd"), resamples=resamples)
    report = {
        "summary": {
            "evaluated": len(records),
            "emptyGroundTruthExcluded": len(empty_truth),
            "missingMasksExcluded": len(missing),
            "bootstrapResamples": resamples,
        },
        "limitations": [
            "空真值掩膜的样本单独计数并从均值中排除，避免被当作 Dice=0 拉低统计",
            "2D 单层口径，与 3D 体积分割指标不可直接比较",
        ],
    }
    if with_nsd:
        report["summary"]["nsdTolerance"] = tolerance
        report["limitations"].append(
            "NSD 为容差内边界点重合率，容差越大越宽松，跨容差数值不可直接比较"
        )
    return {
        "metrics": ctx.output_json("seg-metrics.json", metrics, "metrics"),
        "report": ctx.output_json("seg-report.json", report, "report"),
    }


@operator("SegmentationEvaluation", version="1.0.0")
class SegmentationEvaluationOperator(Operator):
    """分割质量评估：预测掩膜对真值掩膜的 Dice/IoU/精确率/召回率/HD95。"""

    def run(self, ctx):
        return _evaluate_segmentation(ctx, with_nsd=False)


@operator("SegmentationEvaluation", version="1.1.0")
class SegmentationEvaluationOperatorV11(Operator):
    """分割质量评估（含 NSD）：在 1.0.0 指标基础上增加容差内对称边界重合率。"""

    def run(self, ctx):
        return _evaluate_segmentation(ctx, with_nsd=True)


# ---------------------------------------------------------------------------
# MedSAM 零样本 box-prompt 推理
# ---------------------------------------------------------------------------


def _build_predictor(checkpoint_path, device):
    """加载 MedSAM（SAM ViT-B）权重，返回提供 ``predict(image_rgb, box)`` 的推理对象。

    torch 与 segment_anything 在此延迟导入：模块注册不依赖它们，只在真正推理时
    才要求安装。返回对象的 ``predict`` 接受 HxWx3 的 uint8 图像与 ``[x0, y0, x1, y1]``
    box，输出与输入同尺寸的浮点 logits/probability 图。
    """

    try:
        import torch
        from segment_anything import SamPredictor, sam_model_registry
    except ImportError as exc:
        raise ImportError(
            "MedSAMInference requires torch and segment_anything: "
            "pip install torch 'segment-anything @ "
            "git+https://github.com/facebookresearch/segment-anything.git'；"
            "再从 https://github.com/bowang-lab/MedSAM 下载权重 medsam_vit_b.pth "
            "并配置 checkpointPath（工作区相对路径）"
        ) from exc

    if not checkpoint_path or not Path(checkpoint_path).is_file():
        raise FileNotFoundError(
            "MedSAM checkpoint 权重缺失："
            f"{checkpoint_path or '<empty>'}；请从 https://github.com/bowang-lab/MedSAM "
            "下载 medsam_vit_b.pth 并配置 checkpointPath"
        )
    if device not in ("auto", "cpu", "cuda"):
        raise ValueError(f"Unknown device: {device}")
    if device == "auto":
        resolved = "cuda" if torch.cuda.is_available() else "cpu"
    elif device == "cuda" and not torch.cuda.is_available():
        raise ValueError("device=cuda requested but CUDA is unavailable")
    else:
        resolved = device

    model = sam_model_registry["vit_b"](checkpoint=str(checkpoint_path))
    model.to(resolved)
    model.eval()
    sampler = SamPredictor(model)

    class _BoxPredictor:
        def predict(self, image_rgb, box):
            sampler.set_image(np.asarray(image_rgb, dtype=np.uint8))
            _masks, _scores, logits = sampler.predict(
                box=np.asarray(box, dtype=np.float32).reshape(1, 4),
                multimask_output=False,
            )
            return np.asarray(logits[0], dtype=np.float32)

    return _BoxPredictor()


def _box_prompt(mask, margin):
    """真值掩膜 >0 的最小外接框，按长边比例外扩并裁剪到图像边界。"""
    rows, cols = np.nonzero(mask > 0)
    height, width = mask.shape
    y0, y1 = int(rows.min()), int(rows.max()) + 1
    x0, x1 = int(cols.min()), int(cols.max()) + 1
    pad = float(margin) * max(x1 - x0, y1 - y0)
    return [
        float(min(max(x0 - pad, 0.0), width)),
        float(min(max(y0 - pad, 0.0), height)),
        float(min(max(x1 + pad, 0.0), width)),
        float(min(max(y1 + pad, 0.0), height)),
    ]


def _medsam_image(image, size):
    """灰度图 → 三通道 uint8 的 size×size 输入。"""
    array = np.asarray(image, dtype=np.float64)
    if array.size and float(array.max()) <= 1.0:
        array = array * 255.0
    gray = np.clip(array, 0.0, 255.0).astype(np.uint8)
    rgb = np.repeat(gray[:, :, None], 3, axis=2)
    if rgb.shape[:2] != (size, size):
        rgb = np.asarray(
            Image.fromarray(rgb, mode="RGB").resize((size, size), resample=Image.BILINEAR)
        )
    return rgb


def _resize_probability(probability, width, height):
    """概率图缩放回原图尺寸（双线性）。"""
    array = np.asarray(probability, dtype=np.float32)
    if array.shape == (height, width):
        return array.astype(np.float64)
    resized = Image.fromarray(array, mode="F").resize(
        (width, height), resample=Image.BILINEAR
    )
    return np.asarray(resized, dtype=np.float64)


def _sigmoid(values):
    return 1.0 / (1.0 + np.exp(-np.clip(np.asarray(values, dtype=np.float64), -60.0, 60.0)))


def _probabilities(output):
    """模型输出可能是 logits 或概率：落在 [0,1] 内视为概率，否则做 sigmoid。"""
    array = np.asarray(output, dtype=np.float64)
    if array.size and float(array.min()) >= 0.0 and float(array.max()) <= 1.0:
        return array
    return _sigmoid(array)


@operator("MedSAMInference", version="1.0.0")
class MedSAMInferenceOperator(Operator):
    """MedSAM 零样本 box-prompt 推理。

    这是零样本推理协议，不是 MedSAM 训练过程的复现：box prompt 直接取自真值掩膜
    的外接框，因此评测结果属于带提示的上界参考，不能当作全自动分割性能。缺少
    roiPath、文件不存在或真值掩膜全空的样本写入 report 的 blocked 列表，不产出
    掩膜也不报错；图像与掩膜尺寸不一致则显式失败。
    """

    def run(self, ctx):
        p = ctx.parameters
        samples = ctx.input_json("samples")
        if not samples:
            raise ValueError("MedSAMInference requires samples")
        checkpoint_path = str(p.get("checkpointPath") or "").strip()
        size = int(p.get("imageSize", 1024))
        if size not in _MEDSAM_SIZES:
            raise ValueError(f"imageSize must be one of {_MEDSAM_SIZES}")
        box_margin = float(p.get("boxMargin", 0.0))
        if not 0.0 <= box_margin <= 0.5:
            raise ValueError("boxMargin must be in [0, 0.5]")
        threshold = float(p.get("threshold", 0.0))
        if not -10.0 <= threshold <= 10.0:
            raise ValueError("threshold must be in [-10, 10]")
        device = str(p.get("device") or "auto")
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError("device must be one of auto/cpu/cuda")

        predictor = None
        entries = []
        blocked = []
        for position, sample in enumerate(samples):
            ctx.check_cancelled()
            image_id = sample["imageId"]
            roi_path = sample.get("roiPath")
            if not roi_path:
                blocked.append({"imageId": image_id, "reason": "missingRoiPath"})
                continue
            roi_file = ctx.store.resolve_path(roi_path)
            if not roi_file.is_file():
                blocked.append({"imageId": image_id, "reason": "roiFileMissing"})
                continue
            image = read_image(ctx.store.resolve_path(sample["imagePath"]))
            truth = read_image(roi_file)
            if image.shape != truth.shape:
                raise ValueError(f"Image and ROI shape mismatch for sample {image_id}")
            if not np.any(truth > 0):
                blocked.append({"imageId": image_id, "reason": "emptyGroundTruth"})
                continue
            box = _box_prompt(truth, box_margin)
            if predictor is None:
                predictor = _build_predictor(
                    str(ctx.store.resolve_path(checkpoint_path)) if checkpoint_path else "",
                    device,
                )
            output = predictor.predict(_medsam_image(image, size), box)
            if np.asarray(output).ndim != 2:
                raise ValueError("MedSAM predictor must return a 2D probability map")
            probability = _resize_probability(output, image.shape[1], image.shape[0])
            binary = (probability >= threshold).astype(np.uint8) * 255
            buffer = io.BytesIO()
            Image.fromarray(binary).save(buffer, format="PNG")
            reference = ctx.output_bytes(f"masks/{image_id}.png", buffer.getvalue(), "mask")
            foreground = binary > 0
            confidence = 0.0
            if foreground.any():
                confidence = float(_probabilities(probability)[foreground].mean())
            entries.append(
                {
                    "imageId": image_id,
                    "patientId": sample.get("patientId"),
                    "file": parse_reference(reference)["path"],
                    "fileRef": reference,
                    "foregroundRatio": float(foreground.mean()),
                    "confidence": min(max(confidence, 0.0), 1.0),
                }
            )
            ctx.progress(
                100.0 * (position + 1) / len(samples), f"masks {position + 1}/{len(samples)}"
            )
        return {
            "masks": ctx.output_json(
                "medsam-mask-index.json",
                {
                    "threshold": threshold,
                    "imageSize": size,
                    "boxMargin": box_margin,
                    "count": len(entries),
                    "entries": entries,
                },
                "mask_collection",
            ),
            "report": ctx.output_json(
                "medsam-report.json",
                {
                    "total": len(samples),
                    "succeeded": len(entries),
                    "blocked": blocked,
                    "parameters": {
                        "checkpointPath": checkpoint_path,
                        "imageSize": size,
                        "boxMargin": box_margin,
                        "threshold": threshold,
                        "device": device,
                    },
                    "protocol": "zero-shot box prompt from ground-truth mask; upper-bound reference only",
                },
                "report",
            ),
        }


