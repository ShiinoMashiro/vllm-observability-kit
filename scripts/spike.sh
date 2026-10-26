#!/usr/bin/env bash
# Spike:验证 vLLM 原生可观测能力(前提:server 已在 :8000 起好)
# 注意:WSL2 下 127.0.0.1 可能拒连,用 eth0 IP(见 docs/SPIKE.md)
# 用法:bash scripts/spike.sh
set -u
OUT=docs/spike_metrics.txt
IP=$(hostname -I | awk '{print $1}')
BASE="http://$IP:8000"

echo "== 1. server 存活 ==" && curl -s -m 5 "$BASE/v1/models" | head -c 200 && echo

echo "== 2. 发一个带 X-Request-Id 的请求 ==" && curl -s -m 30 "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" -H "X-Request-Id: spike-001" \
  -d '{"model":"/home/yaojun/huggingface/Qwen3-1.7B","messages":[{"role":"user","content":"hi"}],"max_tokens":16}' | head -c 300 && echo

echo "== 3. /metrics 中 vllm 指标名清单(前 60 行)===" && curl -s -m 10 "$BASE/metrics" | grep -E "^vllm:" | cut -d'{' -f1 | sort -u | head -60 | tee "$OUT"

echo "== 4. 关键指标抽样 ==" && curl -s -m 10 "$BASE/metrics" | grep -E "vllm:(num_requests_(running|waiting)|time_to_first_token|time_per_output_token|gpu_cache_usage|prefix_cache)" | grep -v "^#" | head -15 | tee -a "$OUT"

echo "== 5. OTel/追踪相关启动参数 ==" && (PATH=~/workspace/venv-vllm/bin:$PATH vllm serve --help 2>&1 | grep -iE "otlp|trace|telemetry" | head -6) | tee -a "$OUT"

echo "== 完成,明细见 $OUT =="
