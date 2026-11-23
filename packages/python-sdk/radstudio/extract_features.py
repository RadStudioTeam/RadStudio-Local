"""特征提取命令行工具"""

import argparse
import json
import sys

import numpy as np

from .features import FeatureConfig, FeatureExtractor, FeatureFamily
from .preprocessing import ImagePreprocessor, PreprocessingConfig
from .validation import IBSIValidator, ReferenceDataManager


def load_image(image_path: str) -> np.ndarray:
    """加载图像文件"""
    try:
        from PIL import Image

        img = Image.open(image_path).convert("L")
        return np.array(img, dtype=np.float64)
    except ImportError:
        # 如果没有 PIL，尝试使用简单的方式
        if image_path.endswith(".npy"):
            return np.load(image_path)
        else:
            raise ValueError(f"Cannot load image: {image_path}")


def load_roi(roi_path: str) -> np.ndarray:
    """加载 ROI 文件"""
    try:
        from PIL import Image

        roi = Image.open(roi_path).convert("L")
        return np.array(roi, dtype=np.int32)
    except ImportError:
        if roi_path.endswith(".npy"):
            return np.load(roi_path)
        else:
            raise ValueError(f"Cannot load ROI: {roi_path}")


def extract_features(
    image_path: str,
    roi_path: str,
    output_path: str | None = None,
    families: list | None = None,
    pixel_spacing: tuple | None = None,
    reference_data_path: str | None = None,
) -> dict:
    """
    提取特征

    Args:
        image_path: 图像文件路径
        roi_path: ROI 文件路径
        output_path: 输出文件路径（可选）
        families: 启用的特征族列表
        pixel_spacing: 像素间距 (x, y)
        reference_data_path: 参考数据文件路径

    Returns:
        特征提取结果
    """
    # 加载数据
    image = load_image(image_path)
    roi_mask = load_roi(roi_path)

    # 预处理配置
    preproc_config = PreprocessingConfig()
    preprocessor = ImagePreprocessor(preproc_config)

    # 预处理
    preproc_result = preprocessor.preprocess(image, roi_mask, pixel_spacing)

    # 特征提取配置
    feature_config = FeatureConfig()
    if families:
        feature_config.enabled_families = [FeatureFamily(f) for f in families]

    extractor = FeatureExtractor(feature_config)

    # 提取特征
    features = extractor.extract(
        preproc_result.image,
        preproc_result.roi_mask,
        preproc_result.discretized_image,
        pixel_spacing,
    )

    # 验证（如果提供了参考数据）
    validation_report = None
    if reference_data_path:
        references = ReferenceDataManager.load_from_json(reference_data_path)
        validator = IBSIValidator(references)
        validation_report = validator.validate_features(features.to_dict(), preproc_result.metadata)

    # 构建结果
    result = {
        "features": features.to_table(),
        "metadata": preproc_result.metadata,
        "intensity_stats": preproc_result.intensity_stats,
        "computation_time": features.computation_time,
        "summary": {
            "total_features": len(features.features),
            "families": list({f.family.value for f in features.features}),
        },
    }

    if validation_report:
        result["validation"] = validation_report.to_dict()

    # 保存结果
    if output_path:
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

    return result


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="RadStudio Feature Extraction")
    parser.add_argument("--image", required=True, help="Image file path")
    parser.add_argument("--roi", required=True, help="ROI file path")
    parser.add_argument("--output", help="Output JSON file path")
    parser.add_argument("--families", nargs="+", help="Feature families to extract")
    parser.add_argument("--pixel-spacing", nargs=2, type=float, help="Pixel spacing (x, y)")
    parser.add_argument("--reference", help="Reference data JSON file path")

    args = parser.parse_args()

    try:
        pixel_spacing = tuple(args.pixel_spacing) if args.pixel_spacing else None

        result = extract_features(
            image_path=args.image,
            roi_path=args.roi,
            output_path=args.output,
            families=args.families,
            pixel_spacing=pixel_spacing,
            reference_data_path=args.reference,
        )

        # 输出到 stdout
        print(json.dumps(result, indent=2))

    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
