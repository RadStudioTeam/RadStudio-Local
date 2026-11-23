"""深度学习算子：PyTorch/MONAI CNN 训练与推理（[dl] extra，独立 DL 算子镜像）。

DLCNNTraining 按 split_plan 分组交叉验证训练 CNN 骨干（torchvision
resnet18/34/50、efficientnet_b0，MONAI densenet121），输出 OOF 预测与五折
checkpoint；早停验证集按患者分组从训练折内划出，与既有防泄漏约定一致。
医学影像增强、FocalLoss 与 AUC 指标由 MONAI 提供；旧 torchvision
checkpoint 不记录 library 字段，推理时按 torchvision 解释，保持兼容。
DLCNNInference 加载 checkpoint 对样本集做五折集成推理。无 CUDA 设备时
自动回退 CPU 并经进度事件提示。

torch/torchvision/monai 为 [dl] extra 依赖——运行时导入失败时给出安装指引，
不影响基础算子镜像与其余算子。
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from .artifact_boundary import parse_reference
from .execution_contract import execution_parameters
from .operator import Operator, operator
from .runner import identity, read_image

_SEED = 42
# ImageNet 预训练归一化统计（迁移学习标准管线）
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)
_MONAI_BACKBONES = {"densenet121"}
_AUGMENTATION_PRESETS = ("none", "light", "standard")
_LOSS_KINDS = ("bce", "focal")
_CLASS_MODES = ("binary", "multiclass")


def _require_torch():
    try:
        import torch

        return torch
    except ImportError as exc:
        raise ImportError(
            "DLCNN operators require the [dl] extra: "
            "uv sync --extra dl (or pip install 'radstudio-sdk[dl]')"
        ) from exc


def _require_monai():
    try:
        import monai

        return monai
    except ImportError as exc:
        raise ImportError(
            "This DLCNN option requires MONAI in the [dl] extra: "
            "uv sync --extra dl (or pip install 'radstudio-sdk[dl]')"
        ) from exc


def _backbone_library(name):
    """骨干实现来源；旧 checkpoint 无 library 字段时按 torchvision 解码。"""
    return "monai" if name in _MONAI_BACKBONES else "torchvision"


def _augmentation_name(params):
    name = str(params.get("augmentation") or "none").lower()
    if name not in _AUGMENTATION_PRESETS:
        raise ValueError(f"Unknown augmentation preset: {name}")
    return name


def _loss_name(params):
    name = str(params.get("loss") or "bce").lower()
    if name not in _LOSS_KINDS:
        raise ValueError(f"Unknown loss: {name}")
    return name


def _focal_gamma(params):
    gamma = float(params.get("focalGamma", 2.0))
    if not 0.0 < gamma <= 10.0:
        raise ValueError("focalGamma must be in (0, 10]")
    return gamma


def _class_mode(params):
    """分类口径；缺省 binary 与 1.0.0/1.1.0 完全一致。"""
    mode = str(params.get("classMode") or "binary").lower()
    if mode not in _CLASS_MODES:
        raise ValueError(f"Unknown classMode: {mode}")
    return mode


def _num_classes(params, mode, labels):
    """多分类类别数；标签必须是 0..numClasses-1 的连续整数。"""
    if mode == "binary":
        return 1
    value = params.get("numClasses")
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        raise ValueError("classMode=multiclass requires numClasses >= 2")
    for label in labels:
        if label is None:
            raise ValueError("multiclass training requires a known label for every sample")
        if not 0 <= int(label) < value:
            raise ValueError(f"label {label} is outside 0..{value - 1}")
    return value


def _require_known_labels(samples):
    missing = [s["imageId"] for s in samples if s.get("label") is None]
    if missing:
        raise ValueError(
            "Training requires known labels; samples without a label: "
            + ", ".join(missing[:10])
            + (" ..." if len(missing) > 10 else "")
        )


def _image_to_tensor(torch, image, size):
    """2D 灰度 → resize → [0,1] 单通道 CHW；三通道与 ImageNet 归一化在组批时做。"""
    lo, hi = float(image.min()), float(image.max())
    scaled = (image - lo) / max(hi - lo, 1e-12)
    tensor = torch.from_numpy(scaled.astype(np.float32))[None, None]
    tensor = torch.nn.functional.interpolate(
        tensor, size=(size, size), mode="bilinear", align_corners=False
    )
    return tensor[0]


def _normalize_images(torch, images, device):
    """单通道 [0,1] 图像列表 → 3 通道 ImageNet 标准化批次。"""
    batch = torch.stack([torch.as_tensor(image, dtype=torch.float32) for image in images])
    batch = batch.repeat(1, 3, 1, 1)
    mean = torch.tensor(_IMAGENET_MEAN, dtype=batch.dtype).view(1, 3, 1, 1)
    std = torch.tensor(_IMAGENET_STD, dtype=batch.dtype).view(1, 3, 1, 1)
    return ((batch - mean) / std).to(device)


def _build_augmentation(name, seed):
    """MONAI 医学增强预设；逐样本作用于单通道 [0,1] 图像。"""
    if name == "none":
        return None
    _require_monai()
    from monai.transforms import (
        Compose,
        RandAdjustContrast,
        RandFlip,
        RandGaussianNoise,
        RandRotate,
        RandZoom,
    )

    if name == "light":
        transforms = [
            RandFlip(prob=0.5, spatial_axis=0),
            RandFlip(prob=0.5, spatial_axis=1),
            RandRotate(range_x=10.0, prob=0.3, keep_size=True, padding_mode="border"),
            RandZoom(prob=0.3, min_zoom=0.9, max_zoom=1.1, keep_size=True),
            RandGaussianNoise(prob=0.2, mean=0.0, std=0.02),
        ]
    else:
        transforms = [
            RandFlip(prob=0.5, spatial_axis=0),
            RandFlip(prob=0.5, spatial_axis=1),
            RandRotate(range_x=15.0, prob=0.5, keep_size=True, padding_mode="border"),
            RandZoom(prob=0.5, min_zoom=0.85, max_zoom=1.15, keep_size=True),
            RandGaussianNoise(prob=0.3, mean=0.0, std=0.03),
            RandAdjustContrast(prob=0.3, gamma=(0.8, 1.2)),
        ]
    pipeline = Compose(transforms)
    pipeline.set_random_state(seed=seed)
    return pipeline


def _augment_images(torch, images, indices, augmentation):
    augmented = []
    for index in indices:
        image = images[index]
        if augmentation is not None:
            image = torch.as_tensor(augmentation(image), dtype=torch.float32)
        augmented.append(image)
    return augmented


def _build_loss(torch, params, labels, device):
    """BCE（可选类别平衡）或 MONAI FocalLoss；标签取自本折实际训练子集。"""
    if _loss_name(params) == "focal":
        _require_monai()
        from monai.losses import FocalLoss

        return FocalLoss(
            include_background=True,
            to_onehot_y=False,
            gamma=_focal_gamma(params),
            reduction="mean",
        )
    pos_weight = None
    if bool(params.get("balanceClasses", False)):
        positives = float(sum(labels))
        negatives = float(len(labels) - positives)
        if positives > 0 and negatives > 0:
            pos_weight = torch.tensor([negatives / positives], dtype=torch.float32, device=device)
    return torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)


def _binary_metrics(torch, probabilities, labels):
    """阈值指标 + AUC；AUC 优先用 MONAI ROCAUCMetric，缺 MONAI 时回退 sklearn。"""
    pairs = [
        (float(probability), int(label))
        for probability, label in zip(probabilities, labels)
        if label is not None
    ]
    if not pairs:
        return None
    values = torch.tensor([p for p, _ in pairs], dtype=torch.float32).view(-1, 1)
    targets = torch.tensor([l for _, l in pairs], dtype=torch.float32).view(-1, 1)
    auc = None
    if len({l for _, l in pairs}) > 1:
        try:
            from monai.metrics import ROCAUCMetric

            metric = ROCAUCMetric()
            metric(y_pred=values, y=targets)
            aggregate = metric.aggregate()
            if hasattr(metric, "compute"):
                aggregate = metric.compute()
            value = float(aggregate)
            auc = value if np.isfinite(value) else None
        except ImportError:
            from sklearn.metrics import roc_auc_score

            auc = float(roc_auc_score(targets.tolist(), values.tolist()))
    predicted = values >= 0.5
    positive = targets >= 0.5
    tp = int((predicted & positive).sum())
    tn = int((~predicted & ~positive).sum())
    fp = int((predicted & ~positive).sum())
    fn = int((~predicted & positive).sum())
    return {
        "auc": auc,
        "accuracy": (tp + tn) / len(pairs),
        "sensitivity": tp / (tp + fn) if tp + fn else None,
        "specificity": tn / (tn + fp) if tn + fp else None,
        "threshold": 0.5,
        "count": len(pairs),
    }


def _multiclass_metrics(probabilities, labels, num_classes):
    """多分类指标：macro/weighted AUC、准确率、宏 F1、混淆矩阵与逐类召回。"""
    matrix = np.zeros((num_classes, num_classes), dtype=int)
    for row, label in zip(probabilities, labels):
        matrix[int(label), int(np.argmax(row))] += 1
    total = int(matrix.sum())
    if not total:
        return None
    correct = int(np.trace(matrix))
    recalls, precisions, f1s = [], [], []
    for index in range(num_classes):
        tp = matrix[index, index]
        fn = matrix[index].sum() - tp
        fp = matrix[:, index].sum() - tp
        recalls.append(float(tp / (tp + fn)) if tp + fn else None)
        precisions.append(float(tp / (tp + fp)) if tp + fp else None)
        denominator = 2 * tp + fp + fn
        f1s.append(float(2 * tp / denominator) if denominator else None)

    def _mean(values):
        present = [value for value in values if value is not None]
        return float(np.mean(present)) if present else None

    auc_macro = auc_weighted = None
    try:
        from sklearn.metrics import roc_auc_score

        y_true = np.asarray(labels, dtype=int)
        y_score = np.asarray(probabilities, dtype=float)
        if len(set(y_true.tolist())) == num_classes:
            auc_macro = float(
                roc_auc_score(y_true, y_score, multi_class="ovr", average="macro")
            )
            auc_weighted = float(
                roc_auc_score(y_true, y_score, multi_class="ovr", average="weighted")
            )
    except (ImportError, ValueError):
        pass
    return {
        "classMode": "multiclass",
        "numClasses": num_classes,
        "aucMacro": auc_macro,
        "aucWeighted": auc_weighted,
        "accuracy": correct / total,
        "f1Macro": _mean(f1s),
        "confusion": matrix.tolist(),
        "perClass": [
            {"classIndex": index, "recall": recalls[index], "precision": precisions[index],
             "f1": f1s[index], "support": int(matrix[index].sum())}
            for index in range(num_classes)
        ],
        "count": total,
    }


def _build_multiclass_loss(torch, params, labels, device, num_classes):
    """多分类交叉熵；balanceClasses 时按训练子集类别频次做逆频率加权。"""
    weight = None
    if bool(params.get("balanceClasses", False)):
        counts = np.bincount(np.asarray(labels, dtype=int), minlength=num_classes).astype(float)
        present = counts > 0
        ratios = np.zeros(num_classes, dtype=np.float64)
        if present.any():
            ratios[present] = counts[present].sum() / (present.sum() * counts[present])
        weight = torch.tensor(ratios, dtype=torch.float32, device=device)
    return torch.nn.CrossEntropyLoss(weight=weight)


def _build_backbone(torch, name, pretrained, library=None, num_outputs=1):
    if library is None:
        library = _backbone_library(name)
    if library == "monai":
        if name not in _MONAI_BACKBONES:
            raise ValueError(f"Unknown MONAI backbone: {name}")
        _require_monai()
        from monai.networks.nets import DenseNet121

        # MONAI 预训练权重会在加载时按形状过滤分类头，out_channels 直接可用。
        return DenseNet121(
            spatial_dims=2,
            in_channels=3,
            out_channels=int(num_outputs),
            pretrained=pretrained,
            progress=False,
        )
    if library != "torchvision":
        raise ValueError(f"Unknown backbone library: {library}")

    from torchvision import models

    builders = {
        "resnet18": models.resnet18,
        "resnet34": models.resnet34,
        "resnet50": models.resnet50,
        "efficientnet_b0": models.efficientnet_b0,
        "inception_v3": models.inception_v3,
    }
    if name not in builders:
        raise ValueError(f"Unknown backbone: {name}")
    if name == "inception_v3":
        # 预训练权重要求 aux_logits=True 才能对上键名；构造后关闭辅助头前向，
        # 否则训练模式下 forward 返回 (主输出, 辅助输出) 而破坏损失计算。
        model = builders[name](
            weights="IMAGENET1K_V1" if pretrained else None, aux_logits=bool(pretrained)
        )
        if model.AuxLogits is not None:
            model.AuxLogits.fc = torch.nn.Linear(
                model.AuxLogits.fc.in_features, int(num_outputs)
            )
        model.aux_logits = False
        model.fc = torch.nn.Linear(model.fc.in_features, int(num_outputs))
        return model
    model = builders[name](weights="IMAGENET1K_V1" if pretrained else None)
    # 二分类：输出单 logit，配 BCEWithLogitsLoss/FocalLoss；多分类输出各类 logits
    if hasattr(model, "fc"):
        model.fc = torch.nn.Linear(model.fc.in_features, int(num_outputs))
    else:
        model.classifier[1] = torch.nn.Linear(model.classifier[1].in_features, int(num_outputs))
    return model


def _patient_holdout(groups, rng):
    """按患者分组从训练折划 20% 为早停验证（患者不交叉，防泄漏）。"""
    patients = np.unique(groups)
    rng.shuffle(patients)
    n_val = max(1, len(patients) // 5)
    val_patients = set(patients[:n_val].tolist())
    val_mask = np.isin(groups, list(val_patients))
    return ~val_mask, val_mask


def _sample_table(samples):
    """与 DataSplit/identity() 一致的样本身份视图（训练分片合并时复用）。"""
    return {
        "records": [
            {"imageId": s["imageId"], "patientId": s["patientId"], "label": s["label"]}
            for s in samples
        ]
    }


def _selected_folds(params, folds):
    """分片参数 foldIndices（折编号）指定的折位置；缺省为全部折。"""
    raw = params.get("foldIndices")
    if raw is None:
        return list(range(len(folds)))
    try:
        wanted = sorted({int(value) for value in raw})
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid foldIndices: {exc}") from exc
    positions = [i for i, fold in enumerate(folds) if int(fold["fold"]) in wanted]
    if not positions or len(positions) != len(wanted):
        raise ValueError("foldIndices out of range")
    return positions


def _load_images(ctx, torch, samples, size, cancel_every=25):
    """预载全部单通道 [0,1] 张量；长数据集按批次检查取消（分片训练需要全量样本）。"""
    images = []
    for i, sample in enumerate(samples):
        if cancel_every and i % cancel_every == 0:
            ctx.check_cancelled()
        images.append(
            _image_to_tensor(torch, read_image(ctx.store.resolve_path(sample["imagePath"])), size)
        )
    return images


def _train_fold(
    torch,
    model,
    x,
    y,
    groups,
    train_idx,
    params,
    device,
    cancel_check=None,
    augmentation=None,
    validation_idx=None,
    multiclass=False,
):
    """训练单折：mini-batch Adam + 可选 MONAI 增强；earlyStopping>0 时早停。

    验证集的来源优先级：划分自带的 validationIds（官方验证集）> 折内患者分组
    留出 20%。前者是论文复现的关键——用官方验证集早停，测试集不参与选择。
    """
    epochs = int(params["epochs"])
    batch_size = max(1, int(params["batchSize"]))
    lr = float(params["learningRate"])
    patience = int(params.get("earlyStopping", 0))
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    generator = torch.Generator().manual_seed(_SEED)

    train_groups = groups[train_idx]
    fit_idx = list(train_idx)
    val_idx = []
    if patience:
        if validation_idx:
            val_idx = list(validation_idx)
        else:
            fit_mask, val_mask = _patient_holdout(train_groups, np.random.default_rng(_SEED))
            val_idx = [i for i, keep in zip(train_idx, val_mask) if keep]
            fit_idx = [i for i, keep in zip(train_idx, fit_mask) if keep]
    if multiclass:
        for index in fit_idx:
            if not 0 <= int(y[index]) < int(params["numClasses"]):
                raise ValueError(f"label {y[index]} is outside the configured class range")
    if multiclass:
        loss_fn = _build_multiclass_loss(
            torch, params, [int(y[i]) for i in fit_idx], device, int(params["numClasses"])
        )
    else:
        loss_fn = _build_loss(torch, params, [float(y[i]) for i in fit_idx], device)

    def target(indices):
        if multiclass:
            return torch.tensor(
                [int(y[i]) for i in indices], dtype=torch.long, device=device
            )
        return torch.tensor(
            [float(y[i]) for i in indices], dtype=torch.float32, device=device
        ).view(-1, 1)

    def forward(indices, augment):
        images = _augment_images(torch, x, indices, augment)
        return model(_normalize_images(torch, images, device))

    best_state = {k: v.clone() for k, v in model.state_dict().items()}
    best_loss = float("inf")
    stale = 0
    epoch = 0
    for epoch in range(1, epochs + 1):
        model.train()
        order = torch.randperm(len(fit_idx), generator=generator).tolist()
        for start in range(0, len(order), batch_size):
            if cancel_check is not None:
                cancel_check()
            idx = [fit_idx[i] for i in order[start : start + batch_size]]
            optimizer.zero_grad()
            loss = loss_fn(forward(idx, augmentation), target(idx))
            loss.backward()
            optimizer.step()
        if val_idx and patience:
            model.eval()
            with torch.no_grad():
                vloss = loss_fn(forward(val_idx, None), target(val_idx)).item()
            if vloss < best_loss - 1e-6:
                best_loss, stale = vloss, 0
                best_state = {k: v.clone() for k, v in model.state_dict().items()}
            else:
                stale += 1
                if stale >= patience:
                    break
        else:
            # 未启用早停时保留最后一轮权重
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
    return best_state, epoch


@operator("DLCNNTraining", version="1.0.0")
class DLCNNTrainingOperator(Operator):
    """CNN 训练：按 split_plan 分组交叉验证，输出 OOF 预测与五折 checkpoint。

    云函数分片：worker 把折按 foldIndices 分给多个实例并发训练，各分片输出
    部分 checkpoint/OOF；mergeParts 调用把折权重复原为完整五折模型并校验
    预测覆盖全部样本。
    """

    def run(self, ctx):
        torch = _require_torch()
        p = execution_parameters(ctx)
        samples = ctx.input_json("samples")
        if int(p.get("mergeParts") or 0):
            return self._merge(ctx, torch, samples)
        split = ctx.input_json("split")

        table_view = _sample_table(samples)
        if split.get("identityHash") != identity(table_view):
            raise ValueError("Sample identity does not match split plan")

        ids = [s["imageId"] for s in samples]
        patients = [s["patientId"] for s in samples]
        labels = [s["label"] for s in samples]
        class_mode = _class_mode(p)
        num_classes = _num_classes(p, class_mode, labels)
        multiclass = class_mode == "multiclass"
        _require_known_labels(samples)
        size = int(p.get("imageSize", 64))
        augmentation = _augmentation_name(p)
        loss_name = _loss_name(p)
        focal_gamma = _focal_gamma(p)
        balance_classes = bool(p.get("balanceClasses", False))
        x = _load_images(ctx, torch, samples, size)
        y = np.asarray(labels, dtype=float)
        groups = np.asarray(patients)

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device.type == "cpu":
            ctx.progress(0, "CUDA unavailable; training on CPU")

        folds = split["folds"]
        selected = _selected_folds(p, folds)
        by_id = {v: i for i, v in enumerate(ids)}
        batch_size = max(1, int(p["batchSize"]))
        predictions = []
        fold_states = []
        fold_meta = []
        for local, fold_index in enumerate(selected):
            ctx.check_cancelled()
            fold = folds[fold_index]
            torch.manual_seed(_SEED)
            ti = [by_id[v] for v in fold["trainIds"]]
            vi = [by_id[v] for v in fold["testIds"]]
            vv = [by_id[v] for v in (fold.get("validationIds") or [])]
            if set(groups[ti]) & set(groups[vi]):
                raise ValueError("Patient leakage in split")
            if set(vv) & set(vi):
                raise ValueError("Validation ids must not overlap test ids")
            if set(groups[ti]) & set(groups[vv]):
                raise ValueError("Patient leakage between train and validation split")
            model = _build_backbone(
                torch, p["backbone"], p.get("pretrained", True), num_outputs=num_classes
            ).to(device)
            fold_augmentation = _build_augmentation(augmentation, _SEED + int(fold["fold"]))
            state, trained_epochs = _train_fold(
                torch,
                model,
                x,
                y,
                groups,
                ti,
                p,
                device,
                ctx.check_cancelled,
                fold_augmentation,
                validation_idx=vv,
                multiclass=multiclass,
            )
            model.load_state_dict(state)
            model.eval()
            probs = []
            with torch.no_grad():
                for start in range(0, len(vi), batch_size):
                    chunk = vi[start : start + batch_size]
                    xb = _normalize_images(torch, [x[i] for i in chunk], device)
                    logits = model(xb)
                    if multiclass:
                        probs.extend(torch.softmax(logits, dim=1).tolist())
                    else:
                        probs.extend(torch.sigmoid(logits.squeeze(1)).tolist())
            for idx, prob in zip(vi, probs):
                row = {
                    "index": idx,
                    "imageId": ids[idx],
                    "patientId": patients[idx],
                    "label": int(y[idx]),
                    "fold": fold["fold"],
                }
                if multiclass:
                    values = [float(value) for value in prob]
                    row["probabilities"] = values
                    row["predictedClass"] = int(np.argmax(values))
                else:
                    row["probability"] = float(prob)
                predictions.append(row)
            fold_states.append(
                {
                    "fold": fold["fold"],
                    "state_dict": state,
                    "trainCount": len(ti),
                    "testCount": len(vi),
                    "epochs": trained_epochs,
                }
            )
            fold_meta.append(
                {
                    "fold": fold["fold"],
                    "trainCount": len(ti),
                    "testCount": len(vi),
                    "epochs": trained_epochs,
                }
            )
            if vv:
                fold_meta[-1]["validationCount"] = len(vv)
            ctx.progress(
                100.0 * (local + 1) / len(selected),
                f"fold {fold['fold'] + 1}/{len(folds)} done",
            )

        predictions.sort(key=lambda row: row["index"])
        if multiclass:
            metrics = _multiclass_metrics(
                [row["probabilities"] for row in predictions],
                [row["label"] for row in predictions],
                num_classes,
            )
        else:
            metrics = _binary_metrics(
                torch,
                [row["probability"] for row in predictions],
                [row["label"] for row in predictions],
            )
        evaluation = "out_of_fold" if len(folds) > 1 else "external"
        checkpoint = {
            "format": "radstudio.dl.checkpoint.v1",
            "backbone": p["backbone"],
            "library": _backbone_library(p["backbone"]),
            "pretrained": bool(p.get("pretrained", True)),
            "imageSize": size,
            "folds": fold_states,
            "seed": _SEED,
            "device": device.type,
            "augmentation": augmentation,
            "loss": loss_name,
            "focalGamma": focal_gamma,
            "balanceClasses": balance_classes,
        }
        if multiclass:
            checkpoint["classMode"] = class_mode
            checkpoint["numClasses"] = num_classes
        buffer = io.BytesIO()
        torch.save(checkpoint, buffer)
        payload = buffer.getvalue()
        checkpoint_ref = ctx.output_bytes("checkpoint.pt", payload, "model")
        model_meta = {
            "kind": "five_fold_ensemble",
            "estimator": "dl_cnn",
            "backbone": p["backbone"],
            "library": checkpoint["library"],
            "imageSize": size,
            "folds": fold_meta,
            "seed": _SEED,
            "evaluation": evaluation,
            "checkpointArtifact": checkpoint_ref,
            "augmentation": augmentation,
            "loss": loss_name,
            "metrics": metrics,
        }
        if multiclass:
            model_meta["classMode"] = class_mode
            model_meta["numClasses"] = num_classes
        outputs = self._model_outputs(ctx, model_meta, checkpoint_ref, len(payload), merge=False)
        outputs["predictions"] = ctx.output_json(
            "predictions.json",
            {
                "records": predictions,
                "evaluation": evaluation,
                "identityHash": identity(table_view),
                "metrics": metrics,
            },
            "prediction_table",
        )
        return outputs

    def _model_outputs(self, ctx, model_meta, checkpoint_ref, checkpoint_size, *, merge):
        """模型端口产物；1.1.0 覆写此钩子输出 model@3 血缘信封，1.0.0 保持旧清单。"""
        return {"model": ctx.output_json("model.json", model_meta, "model")}

    def _merge(self, ctx, torch, samples):
        """合并折分片：拼接 state_dict、按原始样本序重建 OOF 预测与 checkpoint。"""
        parts = int(execution_parameters(ctx).get("mergeParts") or 0)
        if parts < 1:
            raise ValueError("mergeParts must be positive")
        merged = None
        folds = []
        records = []
        identity_hashes = set()
        for i in range(parts):
            checkpoint = torch.load(
                io.BytesIO(ctx.store.read_bytes(ctx.inputs[f"part{i}Model"])),
                map_location="cpu",
                weights_only=True,
            )
            meta = {
                key: checkpoint.get(key)
                for key in (
                    "format",
                    "backbone",
                    "library",
                    "pretrained",
                    "imageSize",
                    "seed",
                    "augmentation",
                    "loss",
                    "focalGamma",
                    "balanceClasses",
                    "classMode",
                    "numClasses",
                )
            }
            if merged is None:
                merged = {**meta, "device": checkpoint.get("device")}
            elif meta != {key: merged.get(key) for key in meta}:
                raise ValueError(f"checkpoint metadata mismatch in part {i}")
            folds.extend(checkpoint.get("folds") or [])
            part = ctx.input_json(f"part{i}Pred")
            identity_hashes.add(part.get("identityHash"))
            records.extend(part.get("records") or [])

        table_view = _sample_table(samples)
        identity_hash = identity(table_view)
        if identity_hashes != {identity_hash}:
            raise ValueError("Training shards disagree on sample identity")
        folds.sort(key=lambda entry: int(entry["fold"]))
        if len({int(entry["fold"]) for entry in folds}) != len(folds):
            raise ValueError("Duplicate fold in merged checkpoint")
        records.sort(key=lambda row: int(row["index"]))
        if [row["imageId"] for row in records] != [s["imageId"] for s in samples]:
            raise ValueError("Merged OOF predictions do not match samples")

        merged["folds"] = folds
        evaluation = "out_of_fold" if len(folds) > 1 else "external"
        if merged.get("classMode") == "multiclass":
            metrics = _multiclass_metrics(
                [row["probabilities"] for row in records],
                [row["label"] for row in records],
                int(merged["numClasses"]),
            )
        else:
            metrics = _binary_metrics(
                torch,
                [row["probability"] for row in records],
                [row["label"] for row in records],
            )
        buffer = io.BytesIO()
        torch.save(merged, buffer)
        payload = buffer.getvalue()
        checkpoint_ref = ctx.output_bytes("checkpoint.pt", payload, "model")
        model_meta = {
            "kind": "five_fold_ensemble",
            "estimator": "dl_cnn",
            "backbone": merged["backbone"],
            "library": merged.get("library") or _backbone_library(merged["backbone"]),
            "imageSize": int(merged["imageSize"]),
            "folds": [
                {
                    "fold": entry["fold"],
                    "trainCount": int(entry.get("trainCount", 0)),
                    "testCount": int(entry.get("testCount", 0)),
                    "epochs": int(entry.get("epochs", 0)),
                }
                for entry in folds
            ],
            "seed": merged["seed"],
            "evaluation": evaluation,
            "checkpointArtifact": checkpoint_ref,
            "augmentation": merged.get("augmentation") or "none",
            "loss": merged.get("loss") or "bce",
            "metrics": metrics,
        }
        if merged.get("classMode") == "multiclass":
            model_meta["classMode"] = "multiclass"
            model_meta["numClasses"] = int(merged["numClasses"])
        outputs = self._model_outputs(ctx, model_meta, checkpoint_ref, len(payload), merge=True)
        outputs["predictions"] = ctx.output_json(
            "predictions.json",
            {
                "records": records,
                "evaluation": evaluation,
                "identityHash": identity_hash,
                "metrics": metrics,
            },
            "prediction_table",
        )
        return outputs


@operator("DLCNNTraining", version="1.1.0")
class DLCNNTrainingOperatorV11(DLCNNTrainingOperator):
    """CNN 训练：输出 model@3 影像血缘（逐折训练影像集 + 合并 checkpoint）。

    折分片只训一部分折时不产出 model 端口，只有合并出完整交叉验证模型才写
    model.json；划分必须携带来源特征表引用，否则无从核对影像样本的来源。
    """

    def _model_outputs(self, ctx, model_meta, checkpoint_ref, checkpoint_size, *, merge):
        split = ctx.input_json("split")
        folds = split.get("folds") or []
        if not merge and len(_selected_folds(execution_parameters(ctx), folds)) < len(folds):
            return {}
        source = split.get("source")
        if not source:
            raise ValueError("DLCNNTraining 1.1.0 requires a split plan carrying its source")
        plan = {int(fold["fold"]): fold for fold in folds}
        reference = parse_reference(checkpoint_ref)
        fit_folds = []
        for entry in model_meta["folds"]:
            number = int(entry["fold"])
            if number not in plan:
                raise ValueError(f"fold {number} is not in the split plan")
            fit_folds.append(
                {
                    "fold": number,
                    "trainedImageIds": list(plan[number]["trainIds"]),
                    "trainedCount": int(entry["trainCount"]),
                    "testCount": int(entry["testCount"]),
                }
            )
        envelope = {
            **model_meta,
            "task": "binary_classification",
            "dimension": "2d",
            "family": model_meta["backbone"],
            "fit": {"features": source, "split": ctx.inputs["split"], "folds": fit_folds},
            "weights": {
                "path": reference["path"],
                "sha256": reference["sha256"],
                "sizeBytes": int(checkpoint_size),
            },
        }
        return {"model": ctx.output_json("model.json", envelope, "model")}


@operator("DLCNNTraining", version="1.2.0")
class DLCNNTrainingOperatorV12(DLCNNTrainingOperator):
    """CNN 训练：官方固定划分（validationIds 早停）+ 多分类。

    固定划分按数据集自带的划分列切分，不携带来源特征表，model@3 的影像血缘
    无从核对，因此模型端口回到不绑契约的清单（与 1.0.0 一致）。需要 model@3
    血缘闸门的随机交叉验证流程继续钉 1.1.0。
    """


def _evaluation_role(parameters):
    role = str(parameters.get("evaluationRole") or "inference")
    if role not in {"inference", "external", "validation"}:
        raise ValueError("evaluationRole must be inference, external or validation")
    return role


@operator("DLCNNInference", version="1.0.0")
class DLCNNInferenceOperator(Operator):
    """CNN 推理：加载五折 checkpoint，集成概率输出 prediction_table。

    云函数分片：worker 按样本切片并发调用（shardOffset 保持全局序号），
    mergeParts 调用按序号拼接预测表。
    """

    def run(self, ctx):
        torch = _require_torch()
        p = execution_parameters(ctx)
        if int(p.get("mergeParts") or 0):
            return self._merge(ctx, p, torch)
        meta = ctx.input_json("model")
        checkpoint_ref = meta.get("checkpointArtifact")
        if not checkpoint_ref:
            raise ValueError("Input model lacks checkpointArtifact (not a DL model)")
        checkpoint = torch.load(
            io.BytesIO(ctx.store.read_bytes(checkpoint_ref)), map_location="cpu", weights_only=True
        )
        size = int(checkpoint.get("imageSize", 64))
        library = checkpoint.get("library") or _backbone_library(checkpoint["backbone"])
        class_mode = str(checkpoint.get("classMode") or "binary")
        num_classes = int(checkpoint.get("numClasses") or 1)
        multiclass = class_mode == "multiclass"
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device.type == "cpu":
            ctx.progress(0, "CUDA unavailable; inferring on CPU")
        models = []
        for entry in checkpoint["folds"]:
            m = _build_backbone(
                torch, checkpoint["backbone"], False, library, num_outputs=num_classes
            ).to(device)
            m.load_state_dict(entry["state_dict"])
            m.eval()
            models.append(m)

        batch_size = max(1, int(ctx.parameters.get("batchSize", 32)))
        offset = int(p.get("shardOffset") or 0)
        predictions = []
        samples = ctx.input_json("samples")
        for start in range(0, len(samples), batch_size):
            ctx.check_cancelled()
            chunk = samples[start : start + batch_size]
            xb = _normalize_images(
                torch,
                [
                    _image_to_tensor(
                        torch, read_image(ctx.store.resolve_path(s["imagePath"])), size
                    )
                    for s in chunk
                ],
                device,
            )
            if multiclass:
                probs = torch.zeros(len(chunk), num_classes, device=device)
                with torch.no_grad():
                    for m in models:
                        probs += torch.softmax(m(xb), dim=1)
                probs /= len(models)
            else:
                probs = torch.zeros(len(chunk), device=device)
                with torch.no_grad():
                    for m in models:
                        probs += torch.sigmoid(m(xb).squeeze(1))
                probs /= len(models)
            for j, (s, prob) in enumerate(zip(chunk, probs.tolist())):
                row = {
                    "index": offset + start + j,
                    "imageId": s["imageId"],
                    "patientId": s["patientId"],
                    "label": s.get("label"),
                    "fold": None,
                }
                if multiclass:
                    values = [float(value) for value in prob]
                    row["probabilities"] = values
                    row["predictedClass"] = int(np.argmax(values))
                else:
                    row["probability"] = float(prob)
                predictions.append(row)
            ctx.progress(
                100.0 * (start + len(chunk)) / max(len(samples), 1),
                f"inference {start + len(chunk)}/{len(samples)}",
            )
        if multiclass:
            metrics = _multiclass_metrics(
                [row["probabilities"] for row in predictions],
                [row["label"] for row in predictions],
                num_classes,
            )
        else:
            metrics = _binary_metrics(
                torch,
                [row["probability"] for row in predictions],
                [row["label"] for row in predictions],
            )
        evaluation = _evaluation_role(p)
        return {
            "predictions": ctx.output_json(
                "predictions.json",
                {
                    "records": predictions,
                    "evaluation": evaluation,
                    "backbone": checkpoint["backbone"],
                    "library": library,
                    "folds": len(models),
                    "metrics": metrics,
                },
                "prediction_table",
            )
        }

    def _merge(self, ctx, p, torch):
        """拼接样本分片预测：校验序号连续，按全局序号恢复样本顺序。"""
        parts = int(p.get("mergeParts") or 0)
        if parts < 1:
            raise ValueError("mergeParts must be positive")
        backbone = None
        library = None
        folds = None
        records = []
        for i in range(parts):
            part = ctx.input_json(f"part{i}Pred")
            part_backbone = part.get("backbone")
            part_library = part.get("library") or _backbone_library(part_backbone)
            part_folds = int(part.get("folds") or 0)
            if backbone is None:
                backbone, library, folds = part_backbone, part_library, part_folds
            elif (part_backbone, part_library, part_folds) != (backbone, library, folds):
                raise ValueError(f"inference shard metadata mismatch in part {i}")
            records.extend(part.get("records") or [])
        records.sort(key=lambda row: int(row["index"]))
        if [int(row["index"]) for row in records] != list(range(len(records))):
            raise ValueError("Inference shard predictions have gaps or duplicates")
        if records and "probabilities" in records[0]:
            metrics = _multiclass_metrics(
                [row["probabilities"] for row in records],
                [row["label"] for row in records],
                len(records[0]["probabilities"]),
            )
        else:
            metrics = _binary_metrics(
                torch, [row["probability"] for row in records], [row["label"] for row in records]
            )
        return {
            "predictions": ctx.output_json(
                "predictions.json",
                {
                    "records": records,
                    "evaluation": _evaluation_role(p),
                    "backbone": backbone,
                    "library": library,
                    "folds": folds,
                    "metrics": metrics,
                },
                "prediction_table",
            )
        }


@operator("DLCNNInference", version="1.1.0")
class DLCNNInferenceOperatorV11(DLCNNInferenceOperator):
    """CNN 推理：支持把独立外部/验证集预测标记为 external/validation。"""


@operator("DLCNNInference", version="1.2.0")
class DLCNNInferenceOperatorV12(DLCNNInferenceOperatorV11):
    """CNN 推理：接收 model@3 影像血缘模型（checkpoint 仍在信封内）。"""


@operator("DLCNNInference", version="1.3.0")
class DLCNNInferenceOperatorV13(DLCNNInferenceOperatorV11):
    """CNN 推理：支持多分类 checkpoint（probabilities + predictedClass）。

    模型端口不绑契约，接收 DLCNNTraining 1.2.0 固定划分产出的模型清单。
    """


_GRADCAM_COLORMAPS = ("jet", "viridis")
_GRADCAM_TARGETS = ("predicted", "positive", "negative")


def _last_conv_module(torch, model):
    """定位最后一个 2D 卷积层：Grad-CAM 的归因目标层。"""
    name, last = "", None
    for module_name, module in model.named_modules():
        if isinstance(module, torch.nn.Conv2d):
            name, last = module_name, module
    if last is None:
        raise ValueError("Model has no 2D convolution layer to explain")
    return name, last


def _grad_cam_map(torch, model, image, target, multiclass, size, device):
    """单模型单样本的 Grad-CAM：梯度加权通道和 → ReLU → 归一化 → 上采样。"""
    _name, module = _last_conv_module(torch, model)
    captured = {}

    def hook(_module, _inputs, output):
        captured["activation"] = output

    handle = module.register_forward_hook(hook)
    try:
        model.zero_grad(set_to_none=True)
        logits = model(_normalize_images(torch, [image], device))
        activation = captured["activation"]
        activation.retain_grad()
        if multiclass:
            score = logits[0, int(target)]
        else:
            score = logits[0, 0] if int(target) == 1 else -logits[0, 0]
        score.backward()
        gradient = activation.grad[0]
        weights = gradient.mean(dim=(1, 2), keepdim=True)
        cam = torch.relu((weights * activation[0]).sum(dim=0))
        cam = torch.nn.functional.interpolate(
            cam[None, None], size=(size, size), mode="bilinear", align_corners=False
        )[0, 0]
        cam = cam - cam.min()
        cam = cam / max(float(cam.detach().max()), 1e-12)
        return cam.detach().cpu().numpy()
    finally:
        handle.remove()


def _heatmap_overlay(gray, cam, alpha, colormap):
    """灰度图叠加热力图：返回 HxWx3 的 uint8 数组。"""
    import matplotlib

    matplotlib.use("Agg", force=False)
    from matplotlib import colormaps

    base = np.repeat(np.clip(gray, 0.0, 1.0)[:, :, None], 3, axis=2)
    heat = colormaps[colormap](np.clip(cam, 0.0, 1.0))[..., :3]
    blended = np.clip((1.0 - alpha) * base + alpha * heat, 0.0, 1.0)
    return (blended * 255).astype("uint8")


@operator("GradCAM", version="1.0.0")
class GradCAMOperator(Operator):
    """Grad-CAM 归因：对训练好的 CNN 集成分折出热力图并逐像素取平均。

    输入样本按集成置信度排序取前 sampleCount 个；每个样本都过所有折的模型，
    避免只展示单折结果造成结论误导。热力图是定性证据，不构成病灶定位准确性。
    """

    def run(self, ctx):
        torch = _require_torch()
        p = ctx.parameters
        samples = ctx.input_json("samples")
        if not samples:
            raise ValueError("GradCAM requires samples")
        meta = ctx.input_json("model")
        checkpoint_ref = meta.get("checkpointArtifact")
        if not checkpoint_ref:
            raise ValueError("Input model lacks checkpointArtifact (not a DL model)")
        checkpoint = torch.load(
            io.BytesIO(ctx.store.read_bytes(checkpoint_ref)), map_location="cpu", weights_only=True
        )
        backbone = checkpoint["backbone"]
        library = checkpoint.get("library") or _backbone_library(backbone)
        class_mode = str(checkpoint.get("classMode") or "binary")
        num_classes = int(checkpoint.get("numClasses") or 1)
        multiclass = class_mode == "multiclass"
        size = int(checkpoint.get("imageSize", 64))

        target_class = str(p.get("targetClass") or "predicted")
        if target_class not in _GRADCAM_TARGETS:
            raise ValueError(f"Unknown targetClass: {target_class}")
        if multiclass and target_class != "predicted":
            raise ValueError("targetClass positive/negative only applies to binary models")
        alpha = float(p.get("alpha", 0.45))
        if not 0.2 <= alpha <= 0.8:
            raise ValueError("alpha must be in [0.2, 0.8]")
        colormap = str(p.get("colormap") or "jet")
        if colormap not in _GRADCAM_COLORMAPS:
            raise ValueError(f"Unknown colormap: {colormap}")
        count = int(p.get("sampleCount", 16))
        if not 1 <= count <= 64:
            raise ValueError("sampleCount must be in [1, 64]")

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device.type == "cpu":
            ctx.progress(0, "CUDA unavailable; generating heatmaps on CPU")
        models = []
        for entry in checkpoint["folds"]:
            model = _build_backbone(
                torch, backbone, False, library, num_outputs=num_classes
            ).to(device)
            model.load_state_dict(entry["state_dict"])
            model.eval()
            models.append(model)

        def load_image(index):
            return _image_to_tensor(
                torch, read_image(ctx.store.resolve_path(samples[index]["imagePath"])), size
            )

        batch_size = 32
        ensemble = []
        for start in range(0, len(samples), batch_size):
            ctx.check_cancelled()
            chunk = [load_image(i) for i in range(start, min(start + batch_size, len(samples)))]
            xb = _normalize_images(torch, chunk, device)
            with torch.no_grad():
                if multiclass:
                    probs = torch.zeros(len(chunk), num_classes, device=device)
                    for model in models:
                        probs += torch.softmax(model(xb), dim=1)
                else:
                    probs = torch.zeros(len(chunk), device=device)
                    for model in models:
                        probs += torch.sigmoid(model(xb).squeeze(1))
            ensemble.extend((probs / len(models)).tolist())

        ranked = []
        for index, prob in enumerate(ensemble):
            if multiclass:
                predicted = int(np.argmax(prob))
                confidence = float(max(prob))
            else:
                predicted = 1 if float(prob) >= 0.5 else 0
                confidence = float(max(float(prob), 1.0 - float(prob)))
            ranked.append((confidence, index, predicted, prob))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = ranked[: min(count, len(ranked))]

        entries = []
        layer_name = ""
        for order, (confidence, index, predicted, prob) in enumerate(selected):
            ctx.check_cancelled()
            sample = samples[index]
            target = (
                1 if target_class == "positive" else 0 if target_class == "negative" else predicted
            )
            image = load_image(index)
            cam = np.zeros((size, size), dtype=np.float32)
            for model in models:
                layer_name, _module = _last_conv_module(torch, model)
                cam += _grad_cam_map(torch, model, image, target, multiclass, size, device)
            cam /= len(models)
            overlay = _heatmap_overlay(image[0].numpy(), cam, alpha, colormap)
            buffer = io.BytesIO()
            Image.fromarray(overlay).save(buffer, format="PNG")
            reference = ctx.output_bytes(
                f"heatmap_{order:02d}.png", buffer.getvalue(), "heatmap"
            )
            entry = {
                "index": index,
                "imageId": sample["imageId"],
                "patientId": sample.get("patientId"),
                "label": sample.get("label"),
                "targetClass": target,
                "confidence": confidence,
                "file": parse_reference(reference)["path"],
            }
            if multiclass:
                entry["probabilities"] = [float(value) for value in prob]
            else:
                entry["probability"] = float(prob)
            entries.append(entry)
            ctx.progress(
                100.0 * (order + 1) / len(selected),
                f"heatmap {order + 1}/{len(selected)}",
            )

        heatmaps = {
            "colormap": colormap,
            "alpha": alpha,
            "targetClass": target_class,
            "classMode": class_mode,
            "numClasses": num_classes,
            "backbone": backbone,
            "imageSize": size,
            "folds": len(models),
            "layer": layer_name,
            "normalization": {"min": 0.0, "max": 1.0},
            "count": len(entries),
            "entries": entries,
        }
        report = {
            "summary": {
                "sampleCount": len(entries),
                "foldCount": len(models),
                "classMode": class_mode,
                "targetClass": target_class,
                "meanConfidence": float(np.mean([item[0] for item in selected]))
                if selected
                else None,
                "layer": layer_name,
            },
            "normalization": {"min": 0.0, "max": 1.0},
            "limitations": [
                "Grad-CAM 反映模型关注区域，不是病灶定位准确性的定量证据",
                "热力图逐折平均，单个折的噪声不会单独展示",
            ],
        }
        return {
            "heatmaps": ctx.output_json("heatmaps.json", heatmaps, "plot_collection"),
            "report": ctx.output_json("report.json", report, "report"),
        }
