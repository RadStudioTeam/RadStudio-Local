# IBSI 2D reference data

This directory contains the IBSI-1 digital phantom and mask used by the P1
reproduction, plus a pointer to the extracted 2D reference values.

## Files

| File | Description |
| --- | --- |
| `phantom.nii.gz` | IBSI-1 digital phantom, 5 x 4 x 4 voxels, 2 x 2 x 2 mm |
| `mask.nii.gz` | Binary ROI mask, 74 of 80 voxels |
| `../ibsi-2d-reference-values.json` | 2D reference values extracted from the IBSI reference manual |

## Source

- Repository: <https://github.com/theibsi/data_sets>
- Commit: `6da96021bc91faf4c0cb7fd7fa56a4225d2064a8`
- Phantom directory: <https://github.com/theibsi/data_sets/tree/master/ibsi_1_digital_phantom>
- Raw files:
  - <https://raw.githubusercontent.com/theibsi/data_sets/master/ibsi_1_digital_phantom/nifti/image/phantom.nii.gz>
  - <https://raw.githubusercontent.com/theibsi/data_sets/master/ibsi_1_digital_phantom/nifti/mask/mask.nii.gz>
- Retrieved: 2026-11-22

## License and attribution

The phantom is licensed under the Creative Commons Attribution 4.0
International License (CC BY 4.0):
<https://creativecommons.org/licenses/by/4.0/>.

Original design by Alex Zwanenburg. DICOM-format of the phantom courtesy of
David Clunie. The reference manual is also CC BY 4.0; see the source block in
`../ibsi-2d-reference-values.json` for its citation.

## Checksums

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `phantom.nii.gz` | 126 | `83773ac2a288aa93cf819a98eaf301c18d35d5876182ec9c18733184e2b7a83a` |
| `mask.nii.gz` | 99 | `3032d340944b577b83bc559ea7ae9a6034b84633a840eff39570ec17aa1d6281` |

## Validation

Both files were read back after download:

- NIfTI-1 header size 348, dimensions `5 x 4 x 4`, voxel spacing `2 x 2 x 2 mm`.
- Phantom intensities are `1, 3, 4, 6, 9`; mask values are `0/1`.
- Mask contains 74 foreground voxels out of 80.

The 2D reference values are not stored in this directory; they are generated
from the official reference manual by
`scripts/import_ibsi_2d_reference_values.py`.
