# KanagawaWave v2 QLoRA 训练包

本包用于在另一台配有 NVIDIA RTX 4060 8GB 的电脑上，以
LLaMA-Factory 对 `Qwen/Qwen3-4B-Instruct-2507` 做 4-bit NF4 QLoRA
微调。它只包含经审核的数据集成品、固定训练模板和离线校验信息；不包含
Reddit 原始归档、抓取元数据或断点文件。

## 1. 环境准备

建议使用 Linux、WSL2 或可正常运行 CUDA 的 Windows 环境，并先安装与显卡
驱动匹配的 PyTorch，再安装 LLaMA-Factory。训练机需要能够取得基础模型；如已
提前下载，可把配置里的 `model_name_or_path` 改成本地模型目录，但不要改动
`template: qwen3_nothink`。

在解压后的包根目录执行：

```bash
python scripts/validate_kanagawawave_v2_bundle.py .
```

只有校验结果为 `valid` 才开始训练。校验器不联网，也不要求安装
LLaMA-Factory。

## 2. 训练

RTX 4060 8GB 首选：

```bash
llamafactory-cli train configs/train_4060_8gb.yaml
```

如显存不足，改用低显存模板：

```bash
llamafactory-cli train configs/train_4060_low_memory.yaml
```

低显存版只降低截断长度、减少预处理进程、提高梯度累积，并使用单独输出目录。

## 3. 从检查点继续

不要覆盖既有输出目录。确认最新 `checkpoint-*` 目录后，把所选训练配置复制为
新文件，并将其中的 `resume_from_checkpoint` 从 `null` 改为该目录，再运行同一
条训练命令。

## 4. 本地对话检查

普通版训练结束后运行：

```bash
llamafactory-cli chat configs/chat.yaml
```

推理时必须使用 `INFERENCE_PROMPT.txt` 中的固定指令，并将待回复文本作为用户
输入内容；不要添加旧版 `STYLE=` 标签。

## 5. 合并模型

```bash
llamafactory-cli export configs/merge.yaml
```

如果训练使用了低显存输出目录，请先在 `chat.yaml` 和 `merge.yaml` 中把
`adapter_name_or_path` 改成低显存目录。

## 6. 返回部署机

请把以下内容打包返回：

- 合并后的 `outputs/qwen3-4b-kanagawawave-v2-merged/`；
- 实际使用的训练配置；
- 训练日志、最终评估损失和所选检查点说明。

不要返回基础数据源、缓存、模型下载缓存或任何账号凭据。部署机收到合并模型
后，再单独进行 Ollama 导入和 QQ 群绑定；本训练包本身不会更改线上机器人。
