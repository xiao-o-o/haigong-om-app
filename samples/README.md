# 样例音频来源说明

本目录两段音频是团队此前**合成演示包**中的演示录音，不是现场录音，
也不是本轮新生成或重新训练得到的音频。它们只用于演示“上传 → 检测 → 规划 → 绘图”链路。

| 文件 | 用途 | 时长 / 规格 | SHA-256 |
| --- | --- | --- | --- |
| `P01_sample.wav` | 演示“正常”路径 | 10 s / 16 kHz / 16 bit / 单声道 | `2DC862956EEF6A1BD4944706BDE8F9D5AA049C5BD330E7BBA2A1F3C480C72607` |
| `P04_sample.wav` | 演示“疑似异常”路径 | 10 s / 16 kHz / 16 bit / 单声道 | `AAD01FB99E6625CF90934AAEED55B4EC2A62201E4C11AFDC33F56E7003C93886` |

文件名中的 P01、P04 是本项目模拟厂区的设备编号，与任何公开数据集的原始文件编号无关。
本轮没有改动这两个 WAV 文件。

## 来源口径（2026-10-24 核对）

- `models/model_config.json` 中的 `dataset` 字段仍写着 MIMII，但 `data_root` 是
  `data\demo_raw`，训练/验证样本数为 24 / 8，仓库内没有真实数据训练记录。
- 因此当前模型与这两段录音按团队**合成演示**口径说明；
  **不能据此声称已经完成 MIMII 真实数据训练，也不能用于评价声音检测准确率。**
- 本轮保留模型、训练配置和录音原样，不用修改来源字段的方式制造“来源已更新”的假象。

## 计划采用的公开数据集

MIMII Dataset: Sound Dataset for Malfunctioning Industrial Machine Investigation and Inspection

- 发布方：Hitachi, Ltd.
- 地址：<https://zenodo.org/record/3384387>
- 许可：**Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)**
- 全文：<https://creativecommons.org/licenses/by-sa/4.0/>

MIMII 仅作为后续计划采用的公开数据集参考。等 A 提供新的数据清单、训练记录和模型后，
再按实际使用的数据恢复或补充署名、许可与引用说明。

引用格式：

> H. Purohit, R. Tanabe, K. Ichige, T. Endo, Y. Nikaido, K. Suefusa, and Y. Kawaguchi,
> “MIMII Dataset: Sound Dataset for Malfunctioning Industrial Machine Investigation and Inspection,”
> in *Proc. 4th Workshop on Detection and Classification of Acoustic Scenes and Events (DCASE)*, 2019.
> arXiv:1909.09347
