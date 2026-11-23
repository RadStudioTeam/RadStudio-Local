# manifests

- `images.json`：4 个切片行，每行绑定 `image` 与 `roi`；文件路径相对仓库根。
- `inputs.json`：体模、掩膜、参考值表、映射表与切片索引的 SHA-256。
- `input-hash.txt`：上述校验和的聚合哈希，用于快速核对输入是否一致。

体模与参考值均为 CC BY 4.0，可随复现包再分发；引用要求见包根 `README.md`。
