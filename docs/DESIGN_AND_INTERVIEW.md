# vllm-observability-kit:设计文档 · 使用手册 · 面试问答

> 版本:2026-10-28。定位:为 vLLM 补齐生产可观测的零侵入套件——SLO 告警、停滞自动取证、request 级故障分诊、故障注入演示。
> 实证环境:WSL2 / RTX 3060 Ti 8GB / vLLM 0.32.0(torch 2.13.0+cu130)/ Qwen3-1.7B。所有结论均实机验证(docs/SPIKE.md、docs/EXECUTION_LOG.md)。

---

## 第一部分:设计文档

### 1.1 问题定义:vLLM 原生缺什么(实证)

vLLM 0.32 原生具备:OpenAI 兼容 server、Prometheus /metrics(60+ 指标)、OTel 入口(`--otlp-traces-endpoint`)、请求 id 透传(`X-Request-Id` → 响应 `chatcmpl-<id>`)。

原生**缺失**(= 本项目的存在意义,均有实证):
1. **默认日志无 request id**——服务端无法按请求串联证据链(uvicorn access log 只有 IP/路径/状态码);
2. **无停滞/hang 检测**——集合通信 divergence、d2h 死锁时进程"活着但不产出",没有任何告警;
3. **无 request 级取证**——无法回答"这个请求为什么慢/为什么失败";
4. **无故障注入**——无法演练"检测得到吗";
5. **无 SLO 化告警**——指标有了,但没有 TTFT/TPOT 分位、P95/P50 gap、命中率的规则与面板。

### 1.2 核心架构决策(每个都有权衡)

**D1 零源码修改(sidecar + logging 层 + 插件位)**
不 fork vLLM。监控从外部挂:metrics 直接抓原生端点;请求事件用前置记录代理;取证用 sidecar(watchdog)。理由:①vLLM 大版本迭代快,零侵入保证升级不受影响;②可复现(Spike 绑定固定版本);③合规与维护成本。**代价**:拿不到引擎内部粒度(如单请求的调度细节)——用"客户端时间线 + 引擎聚合指标 + GPU 时间线"三方关联补偿;若未来需要 core 粒度,升级路径是提 upstream PR(如 request 级 timeline span)。

**D2 request_id 放在代理层,而不是引擎里**
实证:vLLM 默认日志无 request id,但 `X-Request-Id` 会透传到响应 id。所以最薄、最可靠的注入点就是**前置记录代理**:分配/透传 id,记录每个请求的 send/done 时间戳、状态码、token 数。这是"证据链的第一环"。

**D3 停滞检测 = 单调计数器冻结判定**
`vllm:generation_tokens_total` 是单调递增计数器,只在产出 token 时增长。每 3s 刮一次:**计数器冻结 + running>0 + 持续超过阈值(15s)= hang 疑似**。
为什么不用别的:①轮询日志找关键字太脆;②心跳文件需要改 vLLM;③counter 刮取是零侵入且语义精确("在飞但零产出"就是 hang 的定义)。
阈值选择:TPOT SLO 50ms → 正常 15s 内应有数百 token;取 15s ≈ 正常最坏步时间的数百倍,误报率极低;且仅在 `running>0` 时判定,空闲期不误报。

**D4 黑匣子两层:py-spy 优先,/proc 兜底**
- **py-spy**(生产 Linux 主力):`process_vm_readv` 远程读内存 → 遍历 CPython `PyThreadState` 帧链 → 所有线程的 Python 栈。零注入、零符号依赖。门槛:ptrace(Yama ptrace_scope=1 限制非子进程)。
- **/proc 快照兜底**(WSL2 实际路径):`status`(State/VmRSS/上下文切换)+ 每线程 `wchan`(内核等待通道)+ `stat` + `io`。免 root,可回答"卡在哪类等待":`nvidia_ioctl`(D 状态)→ GPU 驱动 hang;`futex_wait` → 锁/同步;`poll_schedule_timeout` → 网络/IPC;`do_signal_stop` → 被人为冻结。
- **先落盘再告警**:进程随后真死,证据也在(飞行记录仪原则)。

**D5 分诊用规则库,LLM 只做摘要**
每条规则对应一类可证伪的故障模式(信号 → 结论),可解释、可单测、可回归;LLM 只把结构化报告转自然语言。理由:文章作者原话"AI 直接修系统级问题还很难"——证据链必须确定性,LLM 的位置在表述层。

**D6 故障注入器是演示的灵魂**
没有故障就无法演示"检测到了什么"。三种注入对应三类分诊:hang(SIGSTOP EngineCore,配在飞请求)→ 停滞告警 + 黑匣子;error(参数越界)→ 错误分类;longinput(贴近 max_model_len)→ prefill bound 分诊。

### 1.3 数据流全景

```
客户端 ──(带 X-Request-Id)──▶ 记录代理 :9000 ──转发──▶ vLLM :8000
  requests.jsonl(id/timeline/status/tokens)      │
                                                    ▼
watchdog ◀── 3s 刮取 ── vllm:/metrics(60+ 指标)   │
   │  (counter 冻结 + running>0 → 停滞)             │
   ├─ py-spy 栈(生产 Linux)                        │
   ├─ /proc 快照(WSL2 兜底:wchan/状态/IO)          │
   └─ incidents.jsonl                               │
NVML exporter :9835 ◀── scrape ──┐                  │
metrics.jsonl ◀── 1s 刮取 ───────┤                  │
gpu.jsonl    ◀───────────────────┘                  │
                                                     ▼
forensics.cli --request-id X → 时间轴 + 窗口指标 + GPU + 分诊结论(7 条规则)
prometheus scrape(vllm + nvml)→ 8 条告警规则 → Grafana 9 面板
```

### 1.4 监控指标详解(都实证过)

| 指标 | 含义 | 用途 |
| --- | --- | --- |
| `vllm:time_to_first_token_seconds`(histogram) | TTFT 分布 | prefill SLO、排队/prefill bound 分诊 |
| `vllm:inter_token_latency_seconds` | TPOT 分布 | decode SLO |
| `vllm:e2e_request_latency_seconds` | 端到端延迟分布 | P95/P50 gap(文章:"gap≤1 倍算好设计") |
| `vllm:num_requests_running/waiting` | 在跑/排队 | 并发水位、积压检测 |
| `vllm:num_requests_waiting_by_reason` | 排队原因(capacity/deferred) | 区分"容量不足"与"被延迟" |
| `vllm:kv_cache_usage_perc` | KV 水位 | 容量告警、抢占前兆 |
| `vllm:num_preemptions_total` | 抢占次数 | 抢占-重算循环(吞吐崩塌前兆) |
| `vllm:prefix_cache_hits/queries_total` | 前缀命中 | agent 场景成本指标(文章:命中 90%+) |
| `vllm:generation_tokens_total` | 产出 token 计数 | 吞吐 + **停滞检测的心跳** |
| `gpu_utilization / gpu_memory_*`(NVML) | 卡侧 | GPU bound vs host bound 判据 |

### 1.5 告警规则与故障模式映射(8 条,每条可溯源)

| 告警 | 触发条件 | 对应故障模式(文章踩坑) |
| --- | --- | --- |
| VLLMTTFTHigh | TTFT P95 > 5s 持续 2m | 长输入 prefill bound("血亏请求需限流") |
| VLLMTPOTHigh | TPOT P95 > 50ms 持续 2m | decode 算力饱和 / host 回读 |
| VLLMP95P50GapWide | E2E P95/P50 > 2 持续 5m | 长尾混入 → 分桶调度缺失 |
| VLLMQueueBacklog | 5m 内 waiting 恒 >0 | 调度积压(并发超容量) |
| VLLMKVCacheWaterHigh | KV 水位 >90% 持续 2m | 抢占前兆 |
| VLLMPreemptionStorm | 10m 内 preemption>0 | 抢占-重算循环(吞吐崩塌) |
| VLLMPrefixHitDrop | 命中率 <50% 持续 10m | 缓存 evict / 路由失配 |
| VLLMEngineDown | up==0 持续 1m | 进程退出 / 整机 hang |

---

## 第二部分:使用手册

### 2.1 启动全家桶(四个终端)

```bash
# 终端 1:vLLM server(固化了 5 类地雷规避,见 docs/SPIKE.md)
bash scripts/serve.sh

# 终端 2:GPU exporter(WSL2 无 dcgm 的替代)
python exporter/nvml_exporter.py --port 9835

# 终端 3:记录代理(取证数据基座;发请求走 :9000)
IP=$(hostname -I | awk '{print $1}')
python -m forensics.recorder --session records/demo1 \
  --target http://$IP:8000 --port 9000 \
  --gpu-exporter http://$IP:9835 [--delay-ms 200]   # delay 即"慢故障"注入

# 终端 4:停滞 watchdog(pid 为 EngineCore,ps --ppid <api_pid> 查)
python watchdog/stall_watchdog.py \
  --metrics-url http://$IP:8000/metrics \
  --pid <EngineCore_pid> --threshold 15 --interval 3 --outdir incidents/
```

### 2.2 监控栈(Prometheus + Grafana)

```bash
prometheus --config.file=prometheus.yml --storage.tsdb.path=/tmp/prom/data --web.listen-address=:9090
# Grafana → Import dashboards/vllm-slo-dashboard.json → 数据源 Prometheus(:9090)
# 校验:promtool check rules alerts/rules.yml
# 注意:WSL2 下 target 地址用 eth0 IP(127.0.0.1 会拒连)
```

### 2.3 典型工作流

**日常观测**:Grafana 面板看 SLO 四件套(TTFT/TPOT 分位、gap、命中率、KV 水位)。
**慢请求取证**:
```bash
python -m forensics.cli --session records/demo1 --request-id <id>
# 输出:时间轴(send→done)、token 计量、窗口内引擎/GPU 指标峰值、分诊结论
```
**hang 应急**:watchdog 自动告警 → 黑匣子自动落盘(`incidents/pyspy_*.txt`)→ 按 wchan 分层定责(驱动/锁/网络)→ 恢复后 RECOVERED。
**演练**:故障注入 → 观察告警触发 → 取证报告,全流程闭环:
```bash
python tools/fault_injector.py hang --pid <EngineCore> --duration 25   # 停滞演示
python tools/fault_injector.py error --base http://127.0.0.1:9000 --model <path>
python tools/fault_injector.py longinput --base http://127.0.0.1:9000 --model <path> --tokens 3800
```

---

## 第三部分:面试问答(按被问概率排序)

### A. 动机与定位

**Q1 为什么要做这个?vLLM 不是自带监控吗?**
vLLM 自带 60+ Prometheus 指标和 OTel 入口,但生产化缺口真实存在:我逐项实证过——默认日志没有 request id(只有 uvicorn access log 的 IP/路径/状态码);没有任何停滞/hang 检测(进程活着但不产出时无告警);没有 request 级取证(答不了"这个请求为什么慢");没有故障注入能力。我的项目不是重复造轮子,而是补这四个缺口,并且全部零源码修改。

**Q2 为什么不改 vLLM 源码?**
三个理由:①vLLM 大版本迭代极快,零侵入保证升级不受影响;②我的目标场景是"给固定版本的生产运行时加监控",sidecar/logging-handler/前置代理完全够用;③改动越小,越容易说服别人用。什么时候会改:如果需要引擎内部粒度(比如单请求的调度 timeline span),那正确路径是提 upstream PR 而不是自己维护 fork——这也是我留给上游的观察点。

**Q3 这个单机版和文章里的集群版差距在哪?**
诚实差距三点:多节点(PD 分离、跨节点 request id 聚合)、RDMA 网络层指标(ECN/CNP、TE 带宽)、以及 per-rank 引擎指标。单机版保留的是与规模无关的方法论:SLO 化指标、事件链、故障分诊、停滞取证。上集群要改什么我有清单:OTel collector 集中收 span、多实例 label、RDMA exporter、按 request id 跨节点聚合。

### B. 架构与设计

**Q4 讲一下整体架构和数据流。**
一次采集、三个消费端。前置代理(:9000)分配 request_id 并记录每个请求的时间线到 requests.jsonl;两个 1s 采样器分别刮 vLLM /metrics 和 NVML exporter 到 metrics.jsonl/gpu.jsonl;watchdog 独立刮计数器做停滞检测,触发时抓栈写 incidents.jsonl;取证 CLI 把三方数据按 request_id 和时间窗关联,输出时间轴+分诊;Prometheus 抓原生 /metrics 做面板和告警。关键点:所有组件零 vLLM 源码修改。

**Q5 request_id 为什么放代理层?**
实证:vLLM 0.32 默认日志里没有 request id(access log 只有 IP/路径/状态码),但 `X-Request-Id` 会透传到响应 id——我验证过发 spike-001 收到 chatcmpl-spike-001。所以代理层是"既拿到 id、又不碰引擎"的最薄注入点。代价是代理层只能看到客户端视角的 phase(排队/prefill/decode 的内部细节要靠指标窗口关联补偿)。

**Q6 停滞检测为什么用"计数器冻结"而不是日志或心跳?**
`generation_tokens_total` 是单调计数器,"在飞请求存在 + 计数器 15 秒不动"就是 hang 的操作化定义——语义精确、零侵入、每 3 秒一次刮取成本可忽略。日志关键字太脆,心跳文件需要改引擎。误报控制两点:只在 `running>0` 时判定(空闲不告警);恢复时报 RECOVERED 并附带停滞时长。阈值 15s 的依据:TPOT SLO 是 50ms,正常 15 秒该有数百 token 产出,是正常最坏情况的数百倍。

**Q7 hang 了之后,你的黑匣子抓什么?怎么定责?**
两层。第一层 py-spy:通过 process_vm_readv 远程读内存、遍历 CPython 线程帧链,拿所有线程的 Python 栈——生产 Linux 上的主力。第二层 /proc 快照:免 root,抓进程状态、每线程 wchan(内核等待通道)、IO。定责靠 wchan 分类:`nvidia_ioctl`+D 状态 → GPU 驱动 hang;`futex_wait` → 锁/同步;`poll_schedule_timeout` → 网络/IPC;`do_signal_stop` → 被人为冻结。原则是先落盘再告警——进程随后真死了,证据也在。飞行记录仪原则。

**Q8 py-spy 的原理?为什么你的 WSL2 上不能用?**
py-spy 用 process_vm_readv 远程读目标进程内存,然后解析 CPython 解释器内部结构(PyThreadState 链表、帧链、code object),不注入代码、不需要调试符号。WSL2 上失败的原因是 Yama ptrace_scope=1:只允许附着自己的子进程,而 watchdog 和 vLLM 是兄弟进程,又没有 CAP_SYS_PTRACE。两个解法:生产 Linux 上 sudo 或 ptrace_scope=0;受限环境下我用 /proc 快照兜底——损失 Python 行号,但 wchan 仍能定责到"哪类等待"。

**Q9 为什么分诊用规则,不用 LLM 判断?**
证据链必须确定性和可测试:每条规则对应一类可证伪的故障模式,能单测、能回归。LLM 的正确位置是把确定性证据转成自然语言摘要——GLM 那篇文章的作者也是这个观点:AI 抓堆栈、做单点分析可行,系统级判断还靠人。我的架构里 LLM 是可选的表述层,不是诊断层。

**Q10 数据为什么用 JSONL?会不会爆盘?**
MVP 取 JSONL:零依赖、追加写、grep 友好。体量可控:metrics/gpu 各 1s 一条(~200B),一天约 35MB;requests 按请求量。已知风险和升级路径都写了:轮转/压缩未做(待办),长期要换 SQLite 或直接吃 Prometheus 远程数据。

### C. 实现细节

**Q11 Prometheus 的 P95/P50 gap 查询为什么要 clamp_min?**
分母是 P50 的 histogram_quantile,低流量时 P50 可能接近 0,直接除会除零或出天文数字。clamp_min(…, 0.001) 把分母钉住,gap 才是稳定可告警的比值。这个细节是我在写规则时踩出来的——没有它,空闲期的告警全是假的。

**Q12 告警规则里的 for: 2m 是干什么用的?**
区分"抖动"和"状态"。指标瞬间毛刺不应告警,持续 2 分钟的超标才代表真实状态劣化。8 条规则里 critical(EngineDown、PreemptionStorm)for 很短甚至没有,warning 类都带 for——严重性与响应速度的权衡是显式设计。

**Q13 vLLM 的指标是谁在打点?histogram 怎么变成 P95 的?**
API server 用 prometheus-fastapi-instrumentator 暴露,引擎侧用 prometheus_client 注册;TTFT/TPOT 是 Histogram 类型(预分桶计数),P95 = histogram_quantile 在桶边界上插值。这也是为什么所有延迟指标都带 `_bucket/_sum/_count` 三个后缀。

**Q14 记录代理的并发模型?够用吗?**
ThreadingHTTPServer(每请求一线程)+ 写文件加锁。单机取证场景 QPS 很低,这个模型足够;已知边界是不支持流式响应(SSE),且转发是阻塞的——都写在 README 的限制清单里。生产化升级路径是 aiohttp 异步代理。

**Q15 WSL2 的 5 类地雷是什么?(必问)**
①Qwen3 原生 40960 上下文默认全预留,KV 要 4.38GB 超可用 → --max-model-len;②绝对路径调 vllm serve 导致子进程找不到 ninja → PATH 加 venv/bin;③系统 nvcc 是 CUDA 12.0,与 vLLM 0.32 的 CUDA 13 栈不兼容,flashinfer JIT 编译失败 → VLLM_USE_FLASHINFER_SAMPLER=0 绕过;④127.0.0.1 拒连、eth0 IP 可用 → 所有 target 用 hostname -I;⑤首次启动 18 分钟是 torch.compile,不是卡死。每条都写进了 SPIKE.md,可复现。

**Q16 GPU exporter 为什么自写?dcgm 不行吗?**
dcgm-exporter 在 WSL2 不可用(容器运行时依赖不满足)。NVML 库本身在 WSL2 可用(nvidia-ml-py),所以我写了 ~70 行的 HTTP exporter 暴露利用率/显存/温度/功率。这也是一套通用方法:驱动层给什么就暴露什么,不硬上不兼容的组件。

### D. 运维与排障

**Q17 一个请求"慢",你怎么定位?走一遍流程。**
四步:①forensics.cli 拉 request_id 的时间轴,确认 client 视角 latency 与 token 数;②切窗口看引擎指标:waiting 高→调度积压;③TTFT 占比高+prompt 大→prefill bound;TPOT 高→再看 GPU 利用率:平均低→host/同步瓶颈(结合 D2H 信号),高→算力饱和(本机实证 batch 32 饱和);④查窗口内 preemption/KV 水位和 watchdog incidents——有事件就说明发生过停滞。每一步都有数据源,不是拍脑袋。

**Q18 检测到 hang 后,你的工具会自动重启吗?**
故意不自动重启。设计原则是"先证据后动作":自动重启会销毁现场,而且对生产服务贸然重启的决策不该由监控工具单方面做。当前版本只做取证(黑匣子先落盘),处置动作留给人;未来如果要加,也应该做成需显式配置的 playbook,并且只在黑匣子成功落盘之后。

**Q19 误报怎么处理?**
三层:①判定条件带 `running>0`,空闲期结构性不误报;②阈值可调(15s 默认 = 正常最坏情况的数百倍);③恢复事件(RECOVERED)和告警写进同一条 JSONL,事后能统计误报率。实测:错误注入、正常负载均未误报,只有真实 SIGSTOP 才触发。

**Q20 数据可靠性?挂了证据还在吗?**
黑匣子先落盘再告警——这是飞行记录仪原则。watchdog 本身挂了会被发现:metrics 长期不可达时它输出 WARN,且 VLLMEngineDown 告警也会触发(up==0)。已知短板:watchdog 单点,自己没有 watchdog——文档里如实写了。

### E. 对照与扩展

**Q21 和文章(GLM5.2 生产实践)的可观测差距?**
三点:多节点聚合(跨节点按 request id 串联)、RDMA 网络指标(ECN/CNP、TE 带宽)、精度维度监控(精度 gap≤5%、SD accept rate≥20%——这两个我还没覆盖,accept rate 需要投机解码落地后才有数据)。方法论(SLO 化、事件链、分诊)是对齐的。

**Q22 如果部署到真实生产集群,你要改什么?**
① recorder 换成网关层中间件(所有节点统一发 id);② metrics 加 instance/rack label,告警按分组;③ 停滞检测覆盖 PD 分离的 prefetch 环节(文章里 prefetch d2h 死锁是真实案例);④ OTel collector 集中收全节点 span;⑤ 把 /proc 黑匣子换成 py-spy(生产 Linux 有 ptrace 权限)或 eBPF。

**Q23 这个项目的测试是怎么做的?**
实机验证链 + 故障注入,即"用自己的工具测自己的工具":错误注入 → 取证分类正确;SIGSTOP 注入 → watchdog 告警、黑匣子落盘、恢复事件齐全;正常负载 → 零误报。故障注入器本身就是测试框架的一部分——这也是它的设计动机之一。

**Q24 项目的局限性?**(主动说,比被问出来好)
四点:①不支持流式请求(SSE)的记录;②单进程单卡验证,集群场景未实证;③引擎内部粒度缺失(靠聚合指标关联补偿);④watchdog 自身是单点。①③④都有明确升级路径,②是定位使然。

**Q25 后续路线?**
①Prometheus/Grafana 持久化部署(promtool 已验证);②OTel collector 接入验证 span 粒度;③LLM 摘要层;④精度监控(接投机解码的 accept rate 和 logdiff);⑤发现的上游缺口(request 日志无 id、无停滞检测)整理成 vLLM upstream issue/PR。

---

## 第四部分:待完善清单(按优先级)

1. **Prometheus/Grafana 持久化**:二进制移出 /tmp;Grafana 导入 dashboard + 数据源配置;prometheus.yml 中 IP 改成变量或 DNS
2. **OTel 集成**:接 OTLP collector,验证 span 是否覆盖调度/执行 phase;若粒度足够,取证报告升级为 span 级时间轴
3. **流式请求支持**:recorder 目前仅非流式(SSE 转发与 token 计量是明确缺口)
4. **请求级服务端事件**:当前分诊靠"客户端时间线 + 聚合指标关联";要引擎内粒度需 vLLM core 改造(→ upstream PR 机会)
5. **LLM 摘要层**:结构化报告 → 自然语言诊断(定位为表述层)
6. **精度监控**:文章 SLO 的"精度 gap≤5%、SD accept rate≥20%",依赖投机解码落地后接数据
7. **数据管理**:JSONL 无轮转/压缩;长期换 SQLite 或直接消费 Prometheus TSDB
8. **单元测试**:当前以实机验证链代替;规则库(dignose 函数)应补纯单测
9. **多卡/多实例**:per-rank 指标暴露与 label 聚合(TP>1、多 worker 场景)
10. **README 英文版 + 演示 GIF**:开源可见度

## 附:一句话总结(面试收尾用)

> 这个项目是我把"排障时该抓什么"固化成了自动化:给 vLLM 补上 request 级取证、停滞自动取证和 SLO 告警,零源码修改,配故障注入器做端到端演练。它和我的引擎侧工作(nano-vllm 的 greedy/对数/量化)是同一个方法论的两面——监控回答"哪里不对",取证回答"为什么",优化回答"怎么改"。
