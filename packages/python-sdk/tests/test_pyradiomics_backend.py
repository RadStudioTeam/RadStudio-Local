"""PyRadiomics 后端：口径与参考训练集（original + wavelet = 851 特征）对齐。"""

import importlib.util

import numpy as np
import pytest

if importlib.util.find_spec("radiomics") is None or importlib.util.find_spec("SimpleITK") is None:
    pytest.skip("pyradiomics not installed", allow_module_level=True)

from radstudio.pyradiomics_backend import extract


@pytest.fixture()
def sample():
    rng = np.random.default_rng(5)
    image = rng.normal(100, 10, (16, 16))
    mask = np.zeros((16, 16))
    mask[2:14, 2:14] = 1
    return image, mask


ALL_FAMILIES = ["first_order", "shape_2d", "glcm", "glrlm", "glszm", "gldm", "ngtdm"]
REFERENCE_IMAGE_TYPES = ["original", "wavelet"]


def test_reference_cohort_feature_count(sample):
    """参考训练集 = original(107) + 8 个小波子带 x 93 = 851。"""
    image, mask = sample
    values = extract(image, mask, ALL_FAMILIES, REFERENCE_IMAGE_TYPES, bin_width=25)
    assert len(values) == 851
    assert sum(k.startswith("original_") for k in values) == 107
    assert sum(k.startswith("wavelet-") for k in values) == 744
    assert len({k.split("_")[0] for k in values if k.startswith("wavelet-")}) == 8
    # 命名遵循 PyRadiomics 约定，与训练集列名体系一致
    assert "original_firstorder_Mean" in values
    assert "wavelet-HHH_glcm_JointEnergy" in values
    assert all(np.isfinite(v) for v in values.values())


def test_single_image_type_subsets(sample):
    image, mask = sample
    original_only = extract(image, mask, ["first_order"], ["original"])
    assert set(original_only) == {
        k
        for k in extract(image, mask, ALL_FAMILIES, ["original"])
        if k.startswith("original_firstorder_")
    }


def test_no_diagnostics_metadata(sample):
    image, mask = sample
    values = extract(image, mask, ["first_order"], ["original"])
    assert not any(k.startswith(("diagnostics", "general")) for k in values)


def test_shape_complex_zero_imaginary_is_real(sample):
    """shape 特征分解返回虚部为 0 的复数时应转为实数（如 Elongation/Flatness）。"""
    image, mask = sample
    values = extract(image, mask, ["shape_2d"], ["original"])
    assert "original_shape_Elongation" in values
    assert all(isinstance(v, float) for v in values.values())


def test_input_validation(sample):
    image, mask = sample
    with pytest.raises(ValueError, match="2D"):
        extract(np.zeros((4, 4, 4)), mask, ["first_order"], ["original"])
    with pytest.raises(ValueError, match="shape"):
        extract(image, np.zeros((8, 8)), ["first_order"], ["original"])
    with pytest.raises(ValueError, match="empty"):
        extract(image, np.zeros_like(mask), ["first_order"], ["original"])
    with pytest.raises(ValueError, match="families"):
        extract(image, mask, ["nope"], ["original"])
    with pytest.raises(ValueError, match="image types"):
        extract(image, mask, ["first_order"], ["nope"])
