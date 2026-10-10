# KanagawaWave v2 QLoRA 训练包

本包用于在另一台配有 NVIDIA RTX 4060 8GB 的电脑上，以
LLaMA-Factory 对 `Qwen/Qwen3-4B-Instruct-2507` 做 4-bit NF4 QLoRA
微调。它只包含经审核的数据集成品、固定训练模板和离线校验信息；不包含
Reddit 原始归档、抓取元数据或断点文件。

## 适配范围

本训练配方直接适配 `Qwen/Qwen3-4B-Instruct-2507`，并固定使用
`qwen3_nothink` 对话模板、LoRA rank 8、alpha 16 和 4-bit NF4 量化。训练得到的
adapter 必须与同一个基础模型及版本合并，不能直接套到其他模型家族，也不能把
Qwen3 adapter 挂到 Llama、Mistral、Gemma 或其他参数规模的 Qwen 模型上。

项目运行时通过 Ollama 模型标签发起标准聊天请求，因此运行时并不把模型名写死；
但“能够被调用”不等于“已经学会 KanagawaWave 风格”。本包只对上述 Qwen3-4B
训练链路负责。若更换基础模型，必须另行选择正确的对话模板、LoRA 目标层、显存参数，
重新训练和验收；当前配置不宣称全模型兼容。

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

## 6. 导入 Ollama

合并产物是 Hugging Face Safetensors 目录。先在
`outputs/qwen3-4b-kanagawawave-v2-merged/` 中创建 `Modelfile`：

```text
FROM .
```

然后在训练包根目录执行：

```bash
ollama create qwen3-4b-kanagawawave-v2 \
  -f outputs/qwen3-4b-kanagawawave-v2-merged/Modelfile
ollama run qwen3-4b-kanagawawave-v2
```

Ollama 只能直接导入其当前版本支持架构的 Safetensors。若当前 Ollama 拒绝该
Safetensors 目录，需要使用支持该 Qwen3 版本的 llama.cpp 将合并模型转换并量化为
GGUF，再让 `Modelfile` 使用 `FROM ./模型文件.gguf`。本项目不捆绑 llama.cpp，也不把
“Transformers 可以训练”视为“Ollama 一定可以直接导入”；实际导入结果以所安装的
Ollama 与 llama.cpp 版本为准。

导入完成后，把群配置的 `ollama.model` 或白名单私聊使用的模型标签设为
`qwen3-4b-kanagawawave-v2:latest`，再运行 readiness 并重启 Bridge。不要在确认新模型
可正常生成前覆盖原有可用模型标签。

## 7. 返回部署机

请把以下内容打包返回：

- 合并后的 `outputs/qwen3-4b-kanagawawave-v2-merged/`；
- 实际使用的训练配置；
- 训练日志、最终评估损失和所选检查点说明。

不要返回基础数据源、缓存、模型下载缓存或任何账号凭据。部署机收到合并模型
后，再单独进行 Ollama 导入和 QQ 群绑定；本训练包本身不会更改线上机器人。
