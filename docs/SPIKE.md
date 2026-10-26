# SPIKE 报告:vLLM 0.32.0 原生可观测能力实证

> 2026-10-27。环境:WSL2 / RTX 3060 Ti 8GB / 独立 venv(venv-vllm)/ vLLM 0.32.0 / torch 2.13.0+cu130 / Qwen3-1.7B(BF16,max_model_len=4096)。
> 结论:**方案成立**。工具一(监控告警)的数据原生齐全;工具二(取证)OTel 带 request id 但默认日志没有 → 自建事件层正是价值空间。

## 1. 可用的启动命令(踩坑版完整记录)

```bash
VLLM_USE_FLASHINFER_SAMPLER=0 \
PATH=~/workspace/venv-vllm/bin:$PATH \
vllm serve ~/huggingface/Qwen3-1.7B \
  --gpu-memory-utilization 0.75 --max-model-len 4096 --port 8000
```

## 2. 地雷清单(每条都实际踩过)

| # | 现象 | 根因 | 解法 |
| --- | --- | --- | --- |
| 1 | 启动即失败:KV 需 4.38GiB > 可用 1.53GiB | Qwen3 原生 max_position_embeddings=40960,vLLM 默认按全长预留 | `--max-model-len 4096`(错误信息自带建议) |
| 2 | EngineCore 报 `No such file or directory: 'ninja'` | 用绝对路径调 vllm serve,venv bin 不在 PATH,子进程继承后找不到 ninja | `PATH=venv/bin:$PATH` |
| 3 | flashinfer sampling JIT 编译失败:`nvcc fatal : Unknown option '--compress-mode=size'` | 系统 nvcc 是 CUDA 12.0,不认识 12.8+ 参数;vLLM 0.32 栈是 CUDA 13 | 先试 `CUDA_HOME=<venv>/nvidia/cu13`(未解决,CCCL 头文件版本锁)→ **`VLLM_USE_FLASHINFER_SAMPLER=0` 绕过**(采样后端与本项目无关) |
| 4 | curl 127.0.0.1:8000 连接拒绝,但 ss 显示 LISTEN 0.0.0.0:8000 | WSL2 网络怪癖(loopback 不通) | **用 eth0 IP**(`hostname -I` → 192.168.1.4) |
| 5 | 首次启动 ~18 分钟 | torch.compile + 图捕获(无报错,纯慢) | 耐心;后续启动会快 |

## 3. 原生 /metrics 实证(工具一的数据基座)

60+ 个 `vllm:` 前缀指标,关键项全部存在:
- 延迟直方图:`time_to_first_token_seconds`、`inter_token_latency_seconds`(=TPOT)、`e2e_request_latency_seconds`、`request_decode/inference_time_seconds`
- 队列与并发:`num_requests_running/waiting`、**`num_requests_waiting_by_reason{reason="capacity|deferred"}`**
- KV:`kv_cache_usage_perc`、`num_preemptions_total`、`prefix_cache_hits/queries_total`(命中率可算)、`prompt_tokens_cached_total`
- 其他:`generation_tokens_total`、`admission_rejections`、`engine_sleep_state`、`estimated_flops/read/write_bytes_per_gpu`

## 4. request id 与日志(工具二的数据源结论)

- **透传生效**:请求带 `X-Request-Id: spike-001` → 响应 `id: chatcmpl-spike-001`
- **默认日志无 request id**(uvicorn access log 只有 IP/路径/状态码;engine 日志无该串)→ 取证链路 = **OTel span(原生带 id,`--otlp-traces-endpoint`,见 vllm/config/observability.py)+ 自建事件中间件**——后者正是本工具的价值空间
- OTel 依赖(opentelemetry-*)随 vLLM 原生安装

## 5. 对后续的影响

1. 工具一直接开工:dashboards/alerts 基于 §3 指标清单
2. 工具二先做"事件中间件 + 取证 CLI"(不依赖 OTel 粒度结论),OTel 集成并行验证
3. `scripts/spike.sh` 保留为回归工具;`exporter/nvml_exporter.py` 已就绪(WSL2 无 dcgm-exporter 的替代)
4. 启动命令固化进 `scripts/serve.sh`(含全部地雷规避参数)
