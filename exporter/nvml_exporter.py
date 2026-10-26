#!/usr/bin/env python3
"""WSL2 GPU exporter:dcgm-exporter 在 WSL2 不可用,基于 NVML 的轻量替代。
以 Prometheus 文本格式在 :9835/metrics 暴露 GPU 利用率/显存/温度/功率。
运行:python exporter/nvml_exporter.py [--port 9835] [--interval 1]
"""
import argparse
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pynvml


class GPUState:
    def __init__(self):
        pynvml.nvmlInit()
        self.handles = [pynvml.nvmlDeviceGetHandleByIndex(i)
                        for i in range(pynvml.nvmlDeviceGetCount())]

    def sample(self) -> list[tuple[str, str]]:
        rows = []
        for i, h in enumerate(self.handles):
            name = pynvml.nvmlDeviceGetName(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            try:
                power_mw = pynvml.nvmlDeviceGetPowerUsage(h)
            except pynvml.NVMLError:
                power_mw = -1
            rows.append((f'gpu_utilization{{gpu="{i}",name="{name}"}}', str(util.gpu)))
            rows.append((f'gpu_memory_bandwidth_utilization{{gpu="{i}"}}', str(util.memory)))
            rows.append((f'gpu_memory_used_bytes{{gpu="{i}"}}', str(mem.used)))
            rows.append((f'gpu_memory_total_bytes{{gpu="{i}"}}', str(mem.total)))
            rows.append((f'gpu_temperature_celsius{{gpu="{i}"}}', str(temp)))
            if power_mw >= 0:
                rows.append((f'gpu_power_watts{{gpu="{i}"}}', str(power_mw / 1000)))
        return rows


class Handler(BaseHTTPRequestHandler):
    state: GPUState

    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        lines = ["# HELP gpu_metrics WSL2 NVML exporter", "# TYPE gpu_metrics gauge"]
        for k, v in self.state.sample():
            lines.append(f"{k} {v}")
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默访问日志
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9835)
    args = ap.parse_args()
    Handler.state = GPUState()
    print(f"NVML exporter on :{args.port}/metrics, GPUs={len(Handler.state.handles)}")
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
