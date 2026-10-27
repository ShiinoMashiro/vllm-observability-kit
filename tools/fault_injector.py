"""故障注入器:取证工具的演示灵魂——没有故障就无法演示"检测到了什么"。

子命令:
  hang     SIGSTOP 冻结 vLLM 进程 N 秒(模拟集合通信 divergence hang),watchdog 应产生事件
  slow     提示用 recorder 的 --delay-ms 注入慢请求(慢故障走代理层)
  error    发一个必 400 的请求(参数越界),取证报告应给出错误分类
  longinput 发一条贴近 max_model_len 的长输入(合法慢请求,压 prefill)

用法:
  python tools/fault_injector.py hang --pid <vllm_pid> --duration 30
  python tools/fault_injector.py error --base http://127.0.0.1:9000
  python tools/fault_injector.py longinput --base http://127.0.0.1:9000 --model <path>
"""
import argparse
import json
import os
import signal
import time
import urllib.request


def hang(args):
    print(f"[inject] SIGSTOP {args.pid} {args.duration}s —— 模拟 hang,watchdog 应在阈值后告警并抓栈")
    try:
        os.kill(args.pid, signal.SIGSTOP)
    except ProcessLookupError:
        print("[inject] 进程已不存在"); return
    time.sleep(args.duration)
    try:
        os.kill(args.pid, signal.SIGCONT)
        print("[inject] SIGCONT 已恢复(注意:vLLM 恢复后 token 产出应继续,watchdog 报 RECOVERED)")
    except ProcessLookupError:
        print("[inject] SIGCONT 失败:进程已消失")


def error(args):
    body = json.dumps({"model": args.model, "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 999999999})  # 远超 max_model_len → 必 400
    req = urllib.request.Request(f"{args.base}/v1/chat/completions", data=body.encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Request-Id": "inject-error-001"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"[inject] 意外:状态 {r.status}")
    except urllib.error.HTTPError as e:
        print(f"[inject] 注入错误请求 → HTTP {e.code}(预期),该 id 已进 requests.jsonl,可用 forensics 查询")


def longinput(args):
    n = int(args.tokens)
    body = json.dumps({"model": args.model, "messages": [{"role": "user", "content": "词 " * n}],
                       "max_tokens": 8})
    req = urllib.request.Request(f"{args.base}/v1/chat/completions", data=body.encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Request-Id": "inject-longinput-001"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        r.read()
    print(f"[inject] 长输入 {n} token → {time.time()-t:.1f}s(prefill bound 场景,验证 TTFT 分诊)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hang"); h.add_argument("--pid", type=int, required=True); h.add_argument("--duration", type=int, default=30)
    e = sub.add_parser("error"); e.add_argument("--base", required=True); e.add_argument("--model", required=True)
    l = sub.add_parser("longinput"); l.add_argument("--base", required=True); l.add_argument("--model", required=True); l.add_argument("--tokens", type=int, default=3800)
    args = ap.parse_args()
    {"hang": hang, "error": error, "longinput": longinput}[args.cmd](args)
