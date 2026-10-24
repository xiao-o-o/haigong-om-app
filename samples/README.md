# 样例音频来源与许可

本目录两段音频**不是**现场录音，均取自公开工业数据集 MIMII，仅用于演示检测链路。

| 文件 | 用途 | 时长 / 规格 |
| --- | --- | --- |
| `P01_sample.wav` | 演示「正常」路径 | 10 s / 16 kHz / 16 bit / 单声道 |
| `P04_sample.wav` | 演示「疑似异常」路径 | 10 s / 16 kHz / 16 bit / 单声道 |

文件名中的 P01、P04 是本项目模拟厂区的设备编号，与 MIMII 原始文件编号无关。

## 数据集

MIMII Dataset: Sound Dataset for Malfunctioning Industrial Machine Investigation and Inspection

- 发布方：Hitachi, Ltd.
- 地址：<https://zenodo.org/record/3384387>
- 许可：**Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)**
- 全文：<https://creativecommons.org/licenses/by-sa/4.0/>

## 引用

> H. Purohit, R. Tanabe, K. Ichige, T. Endo, Y. Nikaido, K. Suefusa, and Y. Kawaguchi,
> “MIMII Dataset: Sound Dataset for Malfunctioning Industrial Machine Investigation and Inspection,”
> in *Proc. 4th Workshop on Detection and Classification of Acoustic Scenes and Events (DCASE)*, 2019.
> arXiv:1909.09347

## 再分发要求

按 CC BY-SA 4.0：

1. 保留上述署名与许可声明，并给出许可全文链接；
2. 标注是否作过修改（本项目仅做了重采样/切片与重命名，未改变声音内容）；
3. 若基于这些音频创作改编作品，须以相同许可（CC BY-SA 4.0）分发。

如不希望承担该义务，可从仓库移除本目录，改用自己录制的音频；页面支持任意 WAV 上传，
`real` 模式不依赖本目录（`mock` 模式也不需要真实文件）。
