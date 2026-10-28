"""取证 CLI:给定 request_id,重建该请求的完整时间轴并给出分诊结论。

运行:python -m forensics.cli --session records/run1 --request-id r-xxx [--incidents incidents/]
"""
import argparse
import json
import os


def load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def window(records: list[dict], t0: float, t1: float, pad: float = 5.0):
    lo, hi = t0 - pad, t1 + pad
    return [r for r in records if lo <= r.get("ts", 0) <= hi]


def diagnose(req: dict, metrics_win: list[dict], gpu_win: list[dict], incidents: list[dict]) -> list[str]:
    """分诊规则库:每条对应一种故障模式(GLM5.2 文章踩坑 + 本机实证)"""
    findings = []
    status = req.get("status")
    if status != 200:
        findings.append(f"[错误] HTTP {status}:非 200 结束;若 400→参数越界(检查 max_tokens/max_model_len)")

    comp = req.get("completion_tokens") or 0
    lat = req.get("latency_s") or 0
    if lat > 0 and comp > 0:
        tpot_est = (lat - min(lat * 0.2, 2.0)) / comp * 1000  # 粗估:扣掉 ~20% prefill
        if tpot_est > 50:
            findings.append(f"[SLO] 估算 TPOT≈{tpot_est:.0f}ms > 50ms")
    wait_max = max((m.get("vllm:num_requests_waiting", 0) for m in metrics_win), default=0)
    if wait_max > 0:
        findings.append(f"[调度] 窗口内最大排队 {wait_max:.0f} → 调度积压(并发超容量)")
    kv_max = max((m.get("vllm:kv_cache_usage_perc", 0) for m in metrics_win), default=0)
    if kv_max > 0.9:
        findings.append(f"[容量] KV 水位峰值 {kv_max:.0%} → 抢占风险")
    preem = [m.get("vllm:num_preemptions_total", 0) for m in metrics_win]
    if preem and max(preem) > min(preem):
        findings.append("[容量] 窗口内发生 preemption → KV 不足,抢占-重算循环")
    gpu_utils = [g.get("gpu_utilization", 0) for g in gpu_win]
    if gpu_utils and lat > 5 and sum(gpu_utils) / len(gpu_utils) < 50:
        findings.append(f"[host] GPU 平均利用率 {sum(gpu_utils)/len(gpu_utils):.0f}% 偏低 → host/同步瓶颈嫌疑")
    for inc in incidents:
        if req["ts_start"] - 5 <= inc.get("ts", 0) <= req.get("ts_end", 0) + 5:
            findings.append(f"[停滞] 窗口内出现 hang 事件(停滞 {inc.get('stalled_for_s')}s,栈:{inc.get('pyspy_dump')})")
    if not findings:
        findings.append("[正常] 未命中任何分诊规则")
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--request-id", required=True)
    ap.add_argument("--incidents", default="incidents", help="watchdog 的 incidents 目录")
    args = ap.parse_args()
    sess = args.session

    requests = load_jsonl(os.path.join(sess, "requests.jsonl"))
    req = next((r for r in requests if r.get("request_id") == args.request_id), None)
    if req is None:
        print(f"未找到 request_id={args.request_id};已知 id:")
        for r in requests[-10:]:
            print("  ", r.get("request_id"), r.get("status"), r.get("latency_s"), "s")
        return

    metrics_win = window(load_jsonl(os.path.join(sess, "metrics.jsonl")), req["ts_start"], req["ts_end"])
    gpu_win = window(load_jsonl(os.path.join(sess, "gpu.jsonl")), req["ts_start"], req["ts_end"])
    incidents = load_jsonl(os.path.join(args.incidents, "incidents.jsonl"))

    print("=" * 62)
    print(f"请求取证报告  id={args.request_id}")
    print("=" * 62)
    print(f"路径        : {req.get('path')}")
    print(f"状态        : HTTP {req.get('status')}")
    print(f"时间轴      : send {req['ts_start']:.3f} → done {req['ts_end']:.3f}(latency {req.get('latency_s')}s)")
    print(f"token       : prompt_est={req.get('prompt_tokens_est')} completion={req.get('completion_tokens')} max={req.get('max_tokens')}")
    print(f"窗口指标    : metrics {len(metrics_win)} 条 / gpu {len(gpu_win)} 条")
    gpu_avg = sum(g.get("gpu_utilization", 0) for g in gpu_win) / max(len(gpu_win), 1)
    if gpu_win:
        print(f"  GPU      : 利用率均值 {gpu_avg:.0f}% | 显存峰值 {max(g.get('gpu_memory_used_bytes', 0) for g in gpu_win)/1e9:.2f} GB")
    if metrics_win:
        print(f"  引擎     : running 峰值 {max(m.get('vllm:num_requests_running', 0) for m in metrics_win):.0f} | "
              f"waiting 峰值 {max(m.get('vllm:num_requests_waiting', 0) for m in metrics_win):.0f} | "
              f"KV 水位峰值 {max(m.get('vllm:kv_cache_usage_perc', 0) for m in metrics_win):.0%}")
    print("分诊结论    :")
    for f in diagnose(req, metrics_win, gpu_win, incidents):
        print("  -", f)


if __name__ == "__main__":
    main()
