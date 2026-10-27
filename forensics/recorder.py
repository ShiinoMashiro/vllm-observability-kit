"""会话记录器:取证工具二的数据基座。

三个采集器写进同一个 session 目录:
1. 反向代理(:9000 → vLLM):给每个请求分配/透传 request_id,记录 requests.jsonl
   (发送/响应时间戳、prompt_tokens、completion_tokens、状态码、可选注入延迟)
2. metrics 采样器:vLLM /metrics 关键指标 1s 粒度 → metrics.jsonl
3. GPU 采样器:NVML exporter(:9835)1s 粒度 → gpu.jsonl

零 vLLM 源码修改;只支持非流式请求(MVP 边界,见 README)。
运行:python -m forensics.recorder --session records/run1 \
        --target http://192.168.1.4:8000 --port 9000 [--delay-ms 0] [--gpu-exporter :9835]
"""
import argparse
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Session:
    def __init__(self, outdir: str, target: str, gpu_url: str | None, delay_ms: int):
        os.makedirs(outdir, exist_ok=True)
        self.outdir, self.target, self.gpu_url, self.delay_ms = outdir, target, gpu_url, delay_ms
        self.req_counter = 0
        self.stop = threading.Event()
        self._lock = threading.Lock()

    def append(self, filename: str, obj: dict):
        with self._lock, open(os.path.join(self.outdir, filename), "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---- 采集器 2:metrics ----
    def scrape_metrics(self):
        keys = ("vllm:num_requests_running", "vllm:num_requests_waiting",
                "vllm:kv_cache_usage_perc", "vllm:num_preemptions_total",
                "vllm:generation_tokens_total", "vllm:prefix_cache_hits_total",
                "vllm:prefix_cache_queries_total")
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(f"{self.target}/metrics", timeout=5) as r:
                    body = r.read().decode()
                rec: dict = {"ts": time.time()}
                for line in body.splitlines():
                    if any(line.startswith(k) for k in keys) and not line.startswith("#"):
                        name = line.split("{")[0].split(" ")[0]
                        if name.endswith(("_total", "_bucket", "_created")):
                            continue
                        rec[name] = float(line.rsplit(" ", 1)[1])
                self.append("metrics.jsonl", rec)
            except Exception:
                pass
            time.sleep(1)

    # ---- 采集器 3:GPU ----
    def scrape_gpu(self):
        if not self.gpu_url:
            return
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(f"{self.gpu_url}/metrics", timeout=5) as r:
                    body = r.read().decode()
                rec: dict = {"ts": time.time()}
                for line in body.splitlines():
                    if line and not line.startswith("#"):
                        name, val = line.rsplit(" ", 1)
                        rec[name.split("{")[0]] = float(val)
                self.append("gpu.jsonl", rec)
            except Exception:
                pass
            time.sleep(1)


SESSION: Session | None = None


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        assert SESSION is not None
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        request_id = self.headers.get("X-Request-Id") or f"r-{time.time_ns()}"
        prompt_tokens, max_tokens = None, None
        try:
            req = json.loads(body)
            msgs = req.get("messages", [])
            prompt_tokens = sum(len(m.get("content", "")) // 3 for m in msgs)  # 粗估
            max_tokens = req.get("max_tokens")
        except Exception:
            pass
        if SESSION.delay_ms:
            time.sleep(SESSION.delay_ms / 1000)

        t0 = time.time()
        req = urllib.request.Request(f"{SESSION.target}{self.path}", data=body, method="POST")
        for h in ("Content-Type", "Authorization", "X-Request-Id"):
            if h in self.headers:
                req.add_header(h, self.headers[h])
        req.add_unredirected_header("X-Request-Id", request_id)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp_body, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            resp_body, status = e.read(), e.code
        latency = time.time() - t0

        completion_tokens = None
        try:
            completion_tokens = json.loads(resp_body).get("usage", {}).get("completion_tokens")
        except Exception:
            pass
        SESSION.append("requests.jsonl", {
            "ts_start": t0, "ts_end": time.time(), "latency_s": round(latency, 3),
            "request_id": request_id, "path": self.path, "status": status,
            "prompt_tokens_est": prompt_tokens, "max_tokens": max_tokens,
            "completion_tokens": completion_tokens,
        })
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-Id", request_id)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self):
        assert SESSION is not None
        with urllib.request.urlopen(f"{SESSION.target}{self.path}", timeout=30) as r:
            body = r.read()
        self.send_response(r.status)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    global SESSION
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--target", required=True, help="vLLM 地址(WSL2 用 eth0 IP)")
    ap.add_argument("--port", type=int, default=9000, help="记录代理监听端口")
    ap.add_argument("--gpu-exporter", default=None, help="NVML exporter 地址,如 http://127.0.0.1:9835")
    ap.add_argument("--delay-ms", type=int, default=0, help="每请求注入延迟(慢故障)")
    args = ap.parse_args()
    SESSION = Session(args.session, args.target.rstrip("/"), args.gpu_exporter, args.delay_ms)
    threading.Thread(target=SESSION.scrape_metrics, daemon=True).start()
    threading.Thread(target=SESSION.scrape_gpu, daemon=True).start()
    print(f"[recorder] 代理 :{args.port} → {args.target} | session={args.session} | delay={args.delay_ms}ms")
    print(f"[recorder] 发请求请走代理: http://127.0.0.1:{args.port}/v1/chat/completions")
    ThreadingHTTPServer(("0.0.0.0", args.port), ProxyHandler).serve_forever()


if __name__ == "__main__":
    main()
