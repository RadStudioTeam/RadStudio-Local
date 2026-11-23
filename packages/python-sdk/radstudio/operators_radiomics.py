"""影像组学扩展算子：滤波派生图像与三类 sklearn 分类器。

- ``ImageFilterBank``（samples→samples）：对每个样本应用 LoG/小波/平方/开方/
  对数/指数/梯度滤波，样本按 (原图, 派生) 展开，患者分组与标签继承，
  分组交叉验证语义保持；
- ``RandomForestClassifier`` / ``SVMClassifier`` / ``GradientBoostingClassifier``
  （features+split+strategy→model+predictions）：折内训练五折集成，
  与 LogisticRegression 共用 runner.train 的防泄漏约定（缺失填补、特征筛选、
  标准化均仅在训练折内拟合）。
"""

from __future__ import annotations

import io

import numpy as np
from scipy.ndimage import gaussian_laplace, sobel
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.svm import SVC

from .operator import Operator, operator
from .operators_tabular_base import TabularClassifierBase
from .runner import read_image

# 支持的滤波类型（与 operators.json ImageFilterBank.parametersSchema 的枚举同步）
SUPPORTED_FILTERS = (
    "square",
    "squareroot",
    "logarithm",
    "exponential",
    "gradient",
    "log",
    "wavelet",
)


def apply_image_filter(name, image, params):
    """对单幅 2D 图像应用一种滤波，返回 [(派生名, 数组), ...]。

    wavelet 产出 LL/LH/HL/HH 四个子带（各自经 idwt2 重建回原尺寸并裁剪，
    保证与 ROI 掩膜形状一致）；其余滤波各产出一个派生，派生名为空串
    （由调用方以滤波名命名）。
    """
    if name == "square":
        derived = [("", np.square(image))]
    elif name == "squareroot":
        if (image < 0).any():
            raise ValueError("squareroot filter requires a nonnegative image")
        derived = [("", np.sqrt(image))]
    elif name == "logarithm":
        if (image <= 0).any():
            raise ValueError("logarithm filter requires a strictly positive image")
        derived = [("", np.log(image))]
    elif name == "exponential":
        derived = [("", np.exp(image))]
    elif name == "gradient":
        derived = [("", np.hypot(sobel(image, axis=0), sobel(image, axis=1)))]
    elif name == "log":
        sigma = float(params.get("logSigma", 1.0))
        if sigma <= 0:
            raise ValueError("logSigma must be positive")
        derived = [("", gaussian_laplace(image, sigma=sigma))]
    elif name == "wavelet":
        import pywt

        wavelet = str(params.get("wavelet", "haar"))
        cA, (cH, cV, cD) = pywt.dwt2(image, wavelet)
        zeros = (np.zeros_like(cA), np.zeros_like(cH), np.zeros_like(cV), np.zeros_like(cD))
        # 每个子带单独重建（其余置零）回原尺寸：掩膜无需下采样即可对齐
        subbands = {
            "wavelet-LL": (cA, (zeros[1], zeros[2], zeros[3])),
            "wavelet-LH": (zeros[0], (cH, zeros[2], zeros[3])),
            "wavelet-HL": (zeros[0], (zeros[1], cV, zeros[3])),
            "wavelet-HH": (zeros[0], (zeros[1], zeros[2], cD)),
        }
        derived = [
            (label, pywt.idwt2(coeffs, wavelet)[: image.shape[0], : image.shape[1]])
            for label, coeffs in subbands.items()
        ]
    else:
        raise ValueError(f"Unknown filter: {name}")
    return derived


@operator("ImageFilterBank", version="1.0.0")
class ImageFilterBankOperator(Operator):
    """影像滤波库：按 (原图, 滤波派生) 展开样本集，派生图像为 float32 npy。

    展开规则：原始样本按 includeOriginal（默认 true）保留；每个滤波派生
    一条新样本，imageId 加 "#<派生名>" 后缀，patientId/label/roiPath 继承
    （同一患者的全部派生图像在分组交叉验证中进入同一折，无泄漏）。
    """

    def run(self, ctx):
        p = ctx.parameters
        filters = list(p["filters"])
        unknown = [f for f in filters if f not in SUPPORTED_FILTERS]
        if unknown:
            raise ValueError(f"Unknown filters: {unknown}")
        output = []
        for i, s in enumerate(ctx.input_json("samples")):
            image = read_image(ctx.store.resolve_path(s["imagePath"]))
            if p.get("includeOriginal", True):
                output.append(s)
            for name in filters:
                for derived_name, value in apply_image_filter(name, image, p):
                    if not np.isfinite(value).all():
                        raise ValueError(
                            f"Filter {derived_name or name} produced nonfinite values"
                        )
                    label = derived_name or name
                    buffer = io.BytesIO()
                    np.save(buffer, value.astype(np.float32))
                    derived = dict(s)
                    derived["imageId"] = f"{s['imageId']}#{label}"
                    derived["filter"] = label
                    derived["sourceImageId"] = s["imageId"]
                    derived["imagePath"] = ctx.store.write_local(
                        f"sample_{i}_{label}.npy", buffer.getvalue()
                    )
                    output.append(derived)
        return {"samples": ctx.output_json("samples.json", output, "sample_collection")}


# ---------------------------------------------------------------------------
# 分类器：与 LogisticRegression 共用 runner.train 的折内防泄漏约定
# ---------------------------------------------------------------------------


def _random_forest(parameters):
    return RandomForestClassifier(
        n_estimators=int(parameters.get("nEstimators", 200)),
        max_depth=int(parameters["maxDepth"]) if parameters.get("maxDepth") else None,
        random_state=42,
        n_jobs=-1,
    )


def _svc(parameters):
    # sklearn 1.9 起弃用 SVC(probability=True)；改为等价的 sigmoid 校准包装
    # （Platt scaling，校准在训练折内交叉拟合，无泄漏）
    return CalibratedClassifierCV(
        SVC(
            C=float(parameters.get("C", 1.0)),
            kernel=str(parameters.get("kernel", "rbf")),
            gamma=str(parameters.get("gamma", "scale")),
        ),
        method="sigmoid",
        ensemble=False,
    )


def _gradient_boosting(parameters):
    return GradientBoostingClassifier(
        n_estimators=int(parameters.get("nEstimators", 100)),
        learning_rate=float(parameters.get("learningRate", 0.1)),
        max_depth=int(parameters.get("maxDepth", 3)),
        random_state=42,
    )


@operator("RandomForestClassifier", version="1.0.0")
class RandomForestClassifierOperator(TabularClassifierBase):
    """随机森林：折内训练五折集成，输出 OOF 预测与模型。"""

    estimator_factory = _random_forest
    estimator_kind = "random_forest"

    def run(self, ctx):
        return self._run_v100(ctx)


@operator("SVMClassifier", version="1.0.0")
class SVMClassifierOperator(TabularClassifierBase):
    """支持向量机：折内训练五折集成，输出 OOF 预测与模型。"""

    estimator_factory = _svc
    estimator_kind = "svm"

    def run(self, ctx):
        return self._run_v100(ctx)


@operator("GradientBoostingClassifier", version="1.0.0")
class GradientBoostingClassifierOperator(TabularClassifierBase):
    """梯度提升树：折内训练五折集成，输出 OOF 预测与模型。"""

    estimator_factory = _gradient_boosting
    estimator_kind = "gradient_boosting"

    def run(self, ctx):
        return self._run_v100(ctx)


@operator("ImageFilterBank", version="1.1.0")
class ImageFilterBankOperatorV11(ImageFilterBankOperator):
    """影像滤波库：绑定样本集合契约的声明版本，展开规则与 1.0.0 相同。

    派生样本的 ``imageId`` 带 ``#<滤波名>`` 后缀、``patientId`` 与 ``label``
    继承原样本，因此 (原图, 派生) 展开后影像身份仍然唯一、同一患者标签不冲突，
    分组交叉验证的患者隔离不受影响。
    """


# 表格模型契约声明版本：model 输出绑定 radstudio/model@1、predictions 绑定
# radstudio/prediction-table@1。血缘信封（task/dimension/family/fit/weights）是
# 新增的产出，run 不再是 1.0.0 的函数对象；1.0.0 的产物字节保持原样，
# 历史复现与固定版本的运行不受影响。


@operator("RandomForestClassifier", version="1.1.0")
class RandomForestClassifierOperatorV11(RandomForestClassifierOperator):
    """随机森林：输出声明模型血缘与 OOF 预测契约的声明版本。"""

    def run(self, ctx):
        return self._run_v110(ctx)


@operator("SVMClassifier", version="1.1.0")
class SVMClassifierOperatorV11(SVMClassifierOperator):
    """支持向量机：输出声明模型血缘与 OOF 预测契约的声明版本。"""

    def run(self, ctx):
        return self._run_v110(ctx)


@operator("GradientBoostingClassifier", version="1.1.0")
class GradientBoostingClassifierOperatorV11(GradientBoostingClassifierOperator):
    """梯度提升树：输出声明模型血缘与 OOF 预测契约的声明版本。"""

    def run(self, ctx):
        return self._run_v110(ctx)


# radstudio/model@2 折内血缘：逐折各自声明训练集与权重载荷，集成包与
# pipelineArtifact 保持不变，因此消费模型的算子不必跟着改版。


@operator("RandomForestClassifier", version="1.2.0")
class RandomForestClassifierOperatorV12(RandomForestClassifierOperatorV11):
    """随机森林：逐折声明模型血缘（radstudio/model@2）。"""

    def run(self, ctx):
        return self._run_v120(ctx)


@operator("SVMClassifier", version="1.2.0")
class SVMClassifierOperatorV12(SVMClassifierOperatorV11):
    """支持向量机：逐折声明模型血缘（radstudio/model@2）。"""

    def run(self, ctx):
        return self._run_v120(ctx)


@operator("GradientBoostingClassifier", version="1.2.0")
class GradientBoostingClassifierOperatorV12(GradientBoostingClassifierOperatorV11):
    """梯度提升树：逐折声明模型血缘（radstudio/model@2）。"""

    def run(self, ctx):
        return self._run_v120(ctx)
