# vllm-observability-kit

> 为 vLLM 补齐生产可观测:SLO 告警面板、停滞自动取证、request 级故障分诊 CLI、故障注入演示。
> 零 vLLM 源码修改(插件 + logging handler + sidecar 架构)。单机验证环境:RTX 3060 Ti 8GB / WSL2 / vLLM 0.32.0 / Qwen3-1.7B。

## 组件(按阶段交付)

| 阶段 | 组件 | 说明 |
| --- | --- | --- |
| Phase O | Spike | 环境与原生能力实证(docs/SPIKE.md) |
| 工具一 | dashboards/ + alerts/ + watchdog/ | SLO 面板、告警规则、停滞自动取证(py-spy) |
| 工具二 | forensics/ + tools/ | request 级取证 CLI、分诊规则库、故障注入器 |

方法论来源:《GLM5.2 大规模集群推理实践》(SLO 四件套、故障模式清单)。
