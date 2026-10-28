# vllm-observability-kit

> **vLLM 的"行车记录仪 + 分诊台"**:自动记录每个请求的完整时间线,自动发现服务卡死并抓取现场,一条命令输出故障分诊报告。零 vLLM 源码修改。

## 30 秒看懂:这是什么,解决什么问题

你跑着一个 vLLM 服务,迟早会问两个问题:

1. **"这个请求为什么慢了?"** —— vLLM 原生日志给不出答案(没有 request id,只有一行 access log)
2. **"服务是不是卡死了?"** —— 进程活着但不出 token,没有任何告警,只能手工 py-spy 抓栈

本套件把这两件事自动化:

| 你的问题 | 套件做的事 |
| --- | --- |
| 请求为什么慢 | 记录代理给每个请求打 id 并记录时间线;取证 CLI 把请求时间轴与当时的引擎指标、GPU 状态关联,按 7 条规则分诊(排队积压/prefill bound/TPOT 劣化/host 瓶颈/…) |
| 服务是不是卡死 | watchdog 盯着 token 产出计数器,**在飞请求 15s 零产出 → 自动告警 + 抓取线程现场(黑匣子)落盘**,恢复时自动记录 |

设计原则:**零 vLLM 源码修改**(前置代理 + sidecar + 原生 /metrics),vLLM 升级不受影响。

## 一条命令看效果(强烈建议先跑这个)

```bash
bash demo/demo.sh
```

它会自动完成:起服务 → 启动监控 → 发 3 个正常请求 → **在飞请求时注入一次 20s 卡死** → watchdog 自动告警并抓现场 → 打印 `demo-hang-001` 的取证报告。看完这一屏输出,你就知道整个套件在干什么了。

正常情况下你会看到(节选):

```
━━━ 5/6 输出取证报告 ━━━
请求取证报告  id=demo-hang-001
  时间轴      : latency 39.9s(其中 ~20s 是注入的卡死)
  引擎指标    : running 峰值 1 | KV 水位峰值 …
分诊结论      :
  - [停滞] 窗口内出现 hang 事件(停滞 20s,栈: incidents/pyspy_xxx.txt)

  watchdog 事件流:
  [watchdog][ALERT] 疑似 hang:running=1 无产出 15s | 栈抓取: incidents/pyspy_….txt
  [watchdog][RECOVERED] 产出恢复 tokens=2539,停滞持续 23s
```

## 架构

```
客户端 ──(X-Request-Id)──▶ 记录代理 :9000 ──转发──▶ vLLM :8000(零修改)
                            │
                            ├─ requests.jsonl  每请求时间线/状态/token
watchdog ──3s 刮取──────────┼─ metrics.jsonl   引擎指标(running/KV/抢占/吞吐)
  │ 计数器冻结=卡死          └─ gpu.jsonl       NVML 卡侧指标(自研 exporter)
  ├─ py-spy 栈(生产 Linux)/ /proc 快照(WSL2 兜底)→ incidents/
  └─ incidents.jsonl
取证 CLI:按 request_id 关联三方数据 → 时间轴 + 窗口指标 + 7 条规则分诊
Prometheus + Grafana:9 面板 SLO 视图 + 8 条告警规则(dashboards/ alerts/)
```

## 手动部署(分组件)

| 终端 | 命令 | 作用 |
| --- | --- | --- |
| 1 | `bash scripts/serve.sh` | vLLM server(含 WSL2 启动地雷规避) |
| 2 | `python exporter/nvml_exporter.py --port 9835` | GPU 指标暴露(dcgm 在 WSL2 不可用的替代) |
| 3 | `python -m forensics.recorder --session records/demo1 --target http://$IP:8000 --port 9000 --gpu-exporter http://$IP:9835` | 记录代理 + 双采样器 |
| 4 | `python watchdog/stall_watchdog.py --metrics-url http://$IP:8000/metrics --pid <EngineCore> --threshold 15 --outdir incidents/` | 停滞检测 + 黑匣子 |
| 5 | `prometheus --config.file=prometheus.yml` + Grafana 导入 `dashboards/vllm-slo-dashboard.json` | SLO 面板与告警 |

> WSL2 注意:所有 HTTP 地址用 `hostname -I` 的 **eth0 IP**,127.0.0.1 可能拒连。

## 取证报告怎么读

```
时间轴   send → done, latency        ← 客户端视角总耗时
token    prompt / completion / max    ← 规模
窗口指标  running/waiting 峰值         ← 排队积压?
         KV 水位峰值                  ← 容量风险
         GPU 利用率均值               ← 低 = host 瓶颈嫌疑,高 = 算力饱和
分诊结论  7 条规则逐条判,如:
  [调度] 窗口内最大排队 3 → 调度积压
  [停滞] 窗口内出现 hang 事件(栈: incidents/…)
```

分诊规则与故障模式的一一对应关系见 `docs/DESIGN_AND_INTERVIEW.md` §1.5/§3。

## 故障注入(演示与测试)

```bash
python tools/fault_injector.py hang  --pid <EngineCore> --duration 25  # 冻结引擎,验证告警+黑匣子
python tools/fault_injector.py error --base http://$IP:9000 --model <path>  # 400 错误,验证取证分类
python tools/fault_injector.py longinput --base http://$IP:9000 --model <path> --tokens 3800  # prefill bound
```

## 组件清单

```
scripts/serve.sh             vLLM 启动(固化 5 类 WSL2 启动地雷的规避参数)
scripts/spike.sh             原生能力回归验证
exporter/nvml_exporter.py    GPU 指标 exporter(WSL2 替代 dcgm)
watchdog/stall_watchdog.py   停滞检测 → py-spy 栈 / /proc 快照黑匣子
forensics/recorder.py        记录代理 + metrics/GPU 双采样器
forensics/cli.py             取证 CLI(时间轴 + 分诊)
tools/fault_injector.py      故障注入(hang/error/longinput)
prometheus.yml alerts/ dashboards/   监控栈配置
demo/demo.sh                 一键端到端演示
```

## 文档

- `docs/SPIKE.md` — vLLM 0.32 原生能力实证 + 5 类启动地雷
- `docs/EXECUTION_LOG.md` — 全部实机验证的执行记录与结果
- `docs/DESIGN_AND_INTERVIEW.md` — 设计决策详解、25 问面试问答、待完善清单

## 已知限制

1. 仅支持非流式请求(SSE 记录未实现)
2. 单进程单卡验证(集群聚合未做)
3. 取证的引擎内部粒度靠聚合指标关联(要 span 级需改 core 或等 OTel 验证)
4. WSL2 下 py-spy 受 ptrace 限制,黑匣子降级为 /proc 快照(生产 Linux 正常)
5. watchdog 自身是单点;数据 JSONL 无轮转

## 方法论来源

《GLM5.2 大规模集群推理实践》的可观测性实践(SLO 四件套、request id 取证、故障注入演练),在本机 vLLM 上的单机复刻。
