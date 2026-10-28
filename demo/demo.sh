#!/usr/bin/env bash
# 一条命令看懂本套件:起监控 → 打标请求 → 注入 hang → 自动告警抓栈 → 输出取证报告
# 用法:bash demo/demo.sh   (可选环境变量:MODEL=路径 PORT=8000)
set -uo pipefail
IP=$(hostname -I | awk '{print $1}')          # WSL2:127.0.0.1 会拒连,统一用 eth0 IP
VENV=${VENV:-~/workspace/venv-vllm}
MODEL=${MODEL:-~/huggingface/Qwen3-1.7B}
PORT=${PORT:-8000}
SESSION=records/demo-$(date +%H%M%S)
PIDS=()
cleanup() { echo; echo "[demo] 清理演示进程(不影响外部服务)…"; for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT
step() { echo; echo "━━━ $1 ━━━"; }

# 0. 清理孤儿 EngineCore(异常退出可能残留)
for p in $(pgrep -f "VLLM::EngineCore"); do kill "$p" 2>/dev/null; done

step "1/6 启动 vLLM server(已在运行则跳过;冷启动约 1~3 分钟)"
if ! curl -s -m 2 -o /dev/null "http://$IP:$PORT/v1/models"; then
  bash scripts/serve.sh > /tmp/kit_serve.log 2>&1 &
  PIDS+=($!)
  for i in $(seq 1 60); do curl -s -m 2 -o /dev/null "http://$IP:$PORT/v1/models" && break; sleep 5; done
fi
curl -s -m 2 -o /dev/null "http://$IP:$PORT/v1/models" \
  && echo "  ✅ server 就绪: http://$IP:$PORT" \
  || { echo "  ❌ server 未就绪,查看 /tmp/kit_serve.log"; exit 1; }

step "2/6 启动 GPU exporter + 记录代理(行车记录仪上线)"
$VENV/bin/python -u exporter/nvml_exporter.py --port 9835 > /tmp/kit_nvml.log 2>&1 &
PIDS+=($!); sleep 1
$VENV/bin/python -u -m forensics.recorder --session "$SESSION" \
  --target "http://$IP:$PORT" --port 9000 --gpu-exporter http://127.0.0.1:9835 \
  > /tmp/kit_recorder.log 2>&1 &
PIDS+=($!); sleep 2
echo "  ✅ 数据目录: $SESSION/{requests,metrics,gpu}.jsonl"

step "3/6 正常请求 ×3(记录仪开始工作,每条带 request_id)"
for i in 1 2 3; do
  curl -s -m 60 -o /dev/null -w "  demo-0$i → HTTP %{http_code}  %{time_total}s\n" \
    "http://$IP:9000/v1/chat/completions" \
    -H "Content-Type: application/json" -H "X-Request-Id: demo-0$i" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"一句话解释 KV cache\"}],\"max_tokens\":48}"
done

step "4/6 在飞长请求 + 注入 hang(冻结引擎 20s,模拟服务卡死)"
API_PID=$(pgrep -f "bin/vllm serve" | head -1)
ENGINE_PID=$(pgrep -f "VLLM::EngineCore" | head -1)
echo "  API=$API_PID EngineCore=$ENGINE_PID"
$VENV/bin/python -u watchdog/stall_watchdog.py \
  --metrics-url "http://$IP:$PORT/metrics" --pid "$ENGINE_PID" \
  --threshold 12 --interval 3 --outdir incidents/ > /tmp/kit_watchdog.log 2>&1 &
PIDS+=($!)
curl -s -m 600 -o /dev/null "http://$IP:9000/v1/chat/completions" \
  -H "Content-Type: application/json" -H "X-Request-Id: demo-hang-001" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"写一篇长文\"}],\"max_tokens\":2000,\"ignore_eos\":true}" &
CURL_PID=$!
sleep 6
$VENV/bin/python tools/fault_injector.py hang --pid "$ENGINE_PID" --duration 20
wait $CURL_PID; sleep 2

step "5/6 输出取证报告(本工具的核心价值就在这一屏)"
$VENV/bin/python -m forensics.cli --session "$SESSION" --request-id demo-hang-001 --incidents incidents/
echo; echo "  watchdog 事件流:"; grep -E "ALERT|RECOVERED" /tmp/kit_watchdog.log | head -6

step "6/6 完成——接下来你可以"
echo "  · 查任意请求:  python -m forensics.cli --session $SESSION --request-id <id>"
echo "  · 看黑匣子:    incidents/pyspy_*.txt(引擎卡死时的线程现场)"
echo "  · 原始三路数据: $SESSION/{requests,metrics,gpu}.jsonl"
echo "  · 接监控栈:    prometheus --config.file=prometheus.yml + Grafana 导入 dashboards/"
echo "  · 完整设计:    docs/DESIGN_AND_INTERVIEW.md"
