#!/usr/bin/env python3
"""停滞检测 watchdog(差异化核心):监控 token 产出,N 秒无进展判定疑似 hang,
自动对 vLLM 进程做 py-spy 全线程栈抓取存档(黑匣子),并持续告警直到恢复。

对应生产痛点:集合通信 divergence / d2h 死锁导致的 hang —— 文章作者"修半个月没修好"
的第一现场,靠的就是这类栈抓取,这里把它自动化。

运行:python watchdog/stall_watchdog.py \
        --metrics-url http://192.168.1.4:8000/metrics \
        --pid 41731 --threshold 30 --outdir incidents/
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request


def scrape_tokens(metrics_url: str) -> tuple[float, float]:
    """返回 (generation_tokens_total, num_requests_running)"""
    with urllib.request.urlopen(metrics_url, timeout=5) as r:
        body = r.read().decode()
    tokens, running = None, 0.0
    for line in body.splitlines():
        if line.startswith("vllm:generation_tokens_total"):
            tokens = float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:num_requests_running"):
            running = max(running, float(line.rsplit(" ", 1)[1]))
    return (tokens if tokens is not None else 0.0), running


def capture_stack(pid: int, outdir: str) -> str | None:
    """py-spy 抓全线程栈;抓不到(权限/未装)返回 None。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(outdir, f"pyspy_{ts}_{pid}.txt")
    pyspy = os.path.join(os.path.dirname(sys.executable), "py-spy")
    try:
        with open(path, "w") as f:
            subprocess.run([pyspy, "dump", "--pid", str(pid)],
                           stdout=f, stderr=subprocess.DEVNULL, timeout=30)
        if os.path.getsize(path) > 0:
            return path
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        pass
    return capture_proc_snapshot(pid, path)   # 免 root 兜底:py-spy 失败时抓 /proc 快照


def capture_proc_snapshot(pid: int, path: str) -> str | None:
    """py-spy 不可用时的兜底(ptrace_scope=1 的 WSL2):/proc 快照。
    包含进程状态、每线程内核等待通道 wchan、IO 统计——对 hang 定位仍有价值。"""
    lines = [f"# /proc snapshot of pid={pid} at {time.strftime('%F %T')}"]
    try:
        with open(f"/proc/{pid}/status") as f:
            lines += [l.strip() for l in f if l.startswith(("State", "Threads", "VmRSS", "voluntary", "nonvoluntary"))]
        tasks = os.listdir(f"/proc/{pid}/task")
        lines.append(f"# threads: {len(tasks)}")
        for t in tasks:
            try:
                with open(f"/proc/{pid}/task/{t}/wchan") as f:
                    wchan = f.read().strip()
                with open(f"/proc/{pid}/task/{t}/stat") as f:
                    state = f.read().split(") ", 1)[1][0]
                lines.append(f"task {t}: state={state} wchan={wchan}")
            except OSError:
                continue
        with open(f"/proc/{pid}/io") as f:
            lines += [l.strip() for l in f if l.startswith(("read_bytes", "write_bytes"))]
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path if os.path.getsize(path) > 0 else None
    except OSError as e:
        print(f"[watchdog] /proc 快照失败: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics-url", required=True)
    ap.add_argument("--pid", type=int, default=0, help="vLLM API server 进程号,用于自动抓栈")
    ap.add_argument("--threshold", type=float, default=30.0, help="无 token 产出判定阈值(秒)")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--outdir", default="incidents")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    last_tokens, last_progress_ts = 0.0, time.time()
    alerting = False
    print(f"[watchdog] 监控 {args.metrics_url} | 停滞阈值 {args.threshold}s")
    while True:
        time.sleep(args.interval)
        try:
            tokens, running = scrape_tokens(args.metrics_url)
        except Exception as e:
            print(f"[watchdog][WARN] metrics 不可达: {e}")
            continue
        now = time.time()
        if tokens != last_tokens:
            if alerting:
                print(f"[watchdog][RECOVERED] 产出恢复 tokens={tokens:.0f},停滞持续 {now-last_progress_ts:.0f}s")
                alerting = False
            last_tokens, last_progress_ts = tokens, now
            continue
        stalled_for = now - last_progress_ts
        if running > 0 and stalled_for > args.threshold and not alerting:
            alerting = True
            stack = capture_stack(args.pid, args.outdir) if args.pid else None
            incident = {
                "ts": now, "kind": "stall", "stalled_for_s": round(stalled_for, 1),
                "running_requests": running, "generation_tokens": tokens,
                "pyspy_dump": stack,
            }
            with open(os.path.join(args.outdir, "incidents.jsonl"), "a") as f:
                f.write(json.dumps(incident, ensure_ascii=False) + "\n")
            print(f"[watchdog][ALERT] 疑似 hang:running={running:.0f} 无产出 {stalled_for:.0f}s "
                  f"| 栈抓取: {stack or '失败(检查 py-spy/权限)'}")
        elif alerting:
            print(f"[watchdog][ALERT-持续] 已停滞 {stalled_for:.0f}s")


if __name__ == "__main__":
    main()
