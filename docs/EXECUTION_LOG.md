# 执行记录:工具一 + 工具二 实机验证(2026-10-27)

> 环境:vLLM 0.32.0(venv-vllm)/ Qwen3-1.7B / 3060 Ti 8GB / WSL2。
> 组件全部为**零 vLLM 源码修改**:插件式记录代理 + NVML exporter + 独立 watchdog/取证 CLI。

## 1. 验证链总览(全部实机执行)

| 步骤 | 命令 | 结果 |
| --- | --- | --- |
| 起 server | `bash scripts/serve.sh` | ✅ 热启动 10~50s(首次 18min 为 torch.compile) |
| NVML exporter | `python exporter/nvml_exporter.py --port 9835` | ✅ /metrics 输出利用率/显存/温度/功率 |
| 记录代理 | `python -m forensics.recorder --session records/demo1 --target http://$IP:8000 --port 9000 --gpu-exporter http://$IP:9835` | ✅ requests/metrics/gpu 三路 JSONL |
| 打标负载 | 10 条请求带 `X-Request-Id: demo-01..10`,经代理 :9000 | ✅ 全部 200,~0.4s/条,ids 入档 |
| 错误注入 | `python tools/fault_injector.py error --base http://$IP:9000 --model <path>` | ✅ HTTP 400 入档 |
| 取证查询 | `python -m forensics.cli --session records/demo1 --request-id demo-05` | ✅ 时间轴+窗口指标+分诊(正常零误报) |
| 取证查询(错误) | `--request-id inject-error-001` | ✅ 正确分类"[错误] HTTP 400 → 参数越界" |
| **hang 注入** | `fault_injector.py hang --pid <EngineCore> --duration 25`(另有在飞长请求) | ✅ 见 §2 |

## 2. hang 注入 → 停滞检测 → 黑匣子(核心演示)

时序:长请求(max_tokens=3000)在飞 → `SIGSTOP` 冻结 EngineCore 25s → watchdog 阈值 15s:

```
[watchdog][ALERT] 疑似 hang:running=1 无产出 15s | 栈抓取: incidents/pyspy_20261027_232744_90358.txt
[watchdog][ALERT-持续] 已停滞 18s / 21s
[watchdog][RECOVERED] 产出恢复 tokens=4448,停滞持续 24s
```

黑匣子(/proc 快照,免 root):State=T(stopped)、61 线程全部 `wchan=do_signal_stop`、VmRSS 2.0GB、IO 统计——冻结现场完整。
交叉验证:长请求总耗时 39.97s = 正常 ~15s + 冻结 25s,与 incident 时间戳吻合。

## 3. 执行中发现并修复的问题(开发记录)

| 问题 | 修复 |
| --- | --- |
| injector 缺 `import os`;SIGSTOP/SIGCONT 无容错 | 补 import + ProcessLookupError 兜底 |
| watchdog 打错字 `[watchdown]` | 修正 |
| py-spy 在 WSL2 不可用(ptrace_scope=1 且无 CAP_SYS_PTRACE) | **/proc 快照兜底**(生产 Linux 上 py-spy 正常可用,已写注释) |
| SIGSTOP 打在后台任务托管的 API server 上 → 托管器杀进程(exit 147) | 改为冻结 **EngineCore 子进程**(更贴近真实 hang:API 存活、token 停滞) |
| recorder 抓 GPU 用 127.0.0.1 被拒(WSL2 loopback 怪癖) | 改用 eth0 IP |
| nohup 方式的 server 被会话清理终止 | 改用托管后台任务 + serve.sh |

## 4. 当前完成度与待办

已完成:埋点代理、三路数据、取证 CLI、分诊规则(7 条)、错误/hang 注入、watchdog 检测-告警-黑匣子-恢复闭环、NVML exporter、Spike 报告。

待办(按优先级):
1. Grafana/Prometheus 实装(dashboard JSON/rules 已写,二进制未下载;rules 语法需 promtool 校验)
2. OTel span 粒度验证(接 OTLP collector;决定 forensics 是否升级为 span 级)
3. LLM 摘要层(可选)
4. GitHub 建仓推送(等仓库创建)
5. 上游观察点:发现 vLLM 0.32 默认日志无 request id、无停滞检测——若做 core 改造可提 upstream

## 5. 复现手册

```bash
# 终端 1:server            bash scripts/serve.sh
# 终端 2:GPU exporter      python exporter/nvml_exporter.py --port 9835
# 终端 3:记录代理          python -m forensics.recorder --session records/demo1 \
#                            --target http://$(hostname -I | awk '{print $1}'):8000 \
#                            --port 9000 --gpu-exporter http://$(hostname -I | awk '{print $1}'):9835
# 终端 4:watchdog          python watchdog/stall_watchdog.py \
#                            --metrics-url http://$(hostname -I | awk '{print $1}'):8000/metrics \
#                            --pid <EngineCore pid> --threshold 15 --outdir incidents/
# 压测/注入                curl 经 :9000 代理发请求;python tools/fault_injector.py hang/error/longinput
# 取证                     python -m forensics.cli --session records/demo1 --request-id <id>
```

## 6. 补充验证(2026-10-28):监控栈闭环

| 步骤 | 命令 | 结果 |
| --- | --- | --- |
| Grafana dashboard | `dashboards/vllm-slo-dashboard.json`(9 面板,SLO 四件套) | ✅ JSON 校验通过 |
| 告警规则校验 | `promtool check rules alerts/rules.yml` | ✅ SUCCESS: 8 rules |
| Prometheus 实跑 | `prometheus --config.file=prometheus.yml`(45s) | ✅ targets: vllm=up, nvml-gpu=up |
| 即时查询 | `/api/v1/query` | ✅ vllm:num_requests_running / gpu_utilization 均返回数据 |

监控链路至此闭环:引擎/进程指标 → Prometheus 抓取与规则评估 → Grafana 面板(待导入)→ 告警。
复现:prometheus 二进制在 /tmp/prom(建议移入固定路径);Grafana 导入 dashboard JSON 后配置 Prometheus 数据源。
