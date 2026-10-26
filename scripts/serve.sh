#!/usr/bin/env bash
# 固化的 vLLM 启动命令(含 SPIKE.md 全部地雷规避)
# 用法:bash scripts/serve.sh [模型路径] [端口]
set -euo pipefail
MODEL=${1:-~/huggingface/Qwen3-1.7B}
PORT=${2:-8000}
VENV=~/workspace/venv-vllm
export VLLM_USE_FLASHINFER_SAMPLER=0        # 系统 CUDA12 nvcc 与 CUDA13 栈不兼容,绕过 flashinfer JIT
export PATH=$VENV/bin:$PATH                  # 保证子进程能找到 ninja
exec vllm serve "$MODEL" --gpu-memory-utilization 0.75 --max-model-len 4096 --port "$PORT"
