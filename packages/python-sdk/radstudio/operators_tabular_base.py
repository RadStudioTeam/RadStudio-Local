"""表格分类器公共基类。

LR / RF / SVM / GradientBoosting 四个算子共享的折内五折集成训练、防泄漏与
血缘信封逻辑。子类只需声明 ``estimator_factory`` 和 ``estimator_kind``
两个类属性；三个版本的 run 方法由基类统一提供。

版本演进：
  v1.0.0: 无血缘信封
  v1.1.0: model@1 整包血缘
  v1.2.0: model@2 逐折血缘
"""

from __future__ import annotations

from .operator import Operator
from .runner import classifier_outputs, model_lineage, train


class TabularClassifierBase(Operator):
    """表格分类器公共基类。

    子类声明：
      estimator_factory: 函数 (parameters dict) → sklearn 分类器
                         （LR 保持 None，train() 会走默认 LogisticRegression）
      estimator_kind:    str，如 'random_forest' / 'svm' / 'gradient_boosting'
                         （LR 保持 None）
    """

    estimator_factory = None
    estimator_kind = None

    def _train_args(self, ctx, *, with_lineage: bool = False, fold_lineage: bool = False):
        """构建 runner.train 的 kwargs，四个版本共享。"""
        args = {
            "table": ctx.input_json("features"),
            "split": ctx.input_json("split"),
            "strategy": ctx.input_json("strategy"),
            "parameters": ctx.parameters,
        }
        # 用 type(self) 访问未绑定的类属性，避免普通函数通过实例访问变成 bound method
        # （Python 描述符协议：函数作为类属性时会自动 __get__ 绑定 self 作为首参）
        cls = type(self)
        if cls.estimator_factory is not None:
            args["estimator_factory"] = cls.estimator_factory
        if cls.estimator_kind is not None:
            args["estimator_kind"] = cls.estimator_kind
        if with_lineage:
            args["lineage"] = model_lineage(ctx)
        if fold_lineage:
            args["fold_lineage"] = True
        return args

    def _run_v100(self, ctx):
        """v1.0.0：无血缘信封，产物字节与已发布版本完全一致。"""
        return classifier_outputs(ctx, train(**self._train_args(ctx)))

    def _run_v110(self, ctx):
        """v1.1.0：model@1 整包血缘。"""
        return classifier_outputs(
            ctx,
            train(**self._train_args(ctx, with_lineage=True)),
            declare_lineage=True,
        )

    def _run_v120(self, ctx):
        """v1.2.0：model@2 逐折血缘。"""
        return classifier_outputs(
            ctx,
            train(**self._train_args(ctx, with_lineage=True, fold_lineage=True)),
            declare_lineage=True,
            fold_lineage=True,
        )
