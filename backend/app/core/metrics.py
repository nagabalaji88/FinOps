"""Prometheus metric definitions and process/system gauges."""

from __future__ import annotations

import os
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "finops_http_requests_total", "HTTP requests", ["method", "path", "status"], registry=REGISTRY
)
http_request_duration = Histogram(
    "finops_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
agent_executions_total = Counter(
    "finops_agent_executions_total", "Agent executions", ["agent", "status"], registry=REGISTRY
)
agent_execution_duration = Histogram(
    "finops_agent_execution_duration_seconds",
    "Agent execution latency",
    ["agent"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
agent_retries_total = Counter(
    "finops_agent_retries_total", "Node retries", ["agent", "node"], registry=REGISTRY
)
llm_tokens_total = Counter(
    "finops_llm_tokens_total", "LLM tokens", ["provider", "model", "kind"], registry=REGISTRY
)
llm_cost_usd_total = Counter(
    "finops_llm_cost_usd_total", "LLM spend in USD", ["provider", "model"], registry=REGISTRY
)
llm_latency = Histogram(
    "finops_llm_latency_seconds",
    "LLM call latency",
    ["provider", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 90),
    registry=REGISTRY,
)
llm_errors_total = Counter(
    "finops_llm_errors_total", "LLM call errors", ["provider", "model", "kind"], registry=REGISTRY
)
tool_calls_total = Counter(
    "finops_tool_calls_total", "Tool invocations", ["tool", "status"], registry=REGISTRY
)
tool_latency = Histogram(
    "finops_tool_latency_seconds",
    "Tool latency",
    ["tool"],
    buckets=(0.005, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)
cache_events_total = Counter(
    "finops_cache_events_total", "Cache hits and misses", ["scope", "event"], registry=REGISTRY
)
vector_search_latency = Histogram(
    "finops_vector_search_seconds",
    "Vector search latency",
    ["collection"],
    buckets=(0.005, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 3),
    registry=REGISTRY,
)
approvals_total = Counter(
    "finops_approvals_total", "Approval decisions", ["agent", "decision"], registry=REGISTRY
)
circuit_state = Gauge("finops_circuit_state", "0 closed 1 half-open 2 open", ["name"], registry=REGISTRY)
queue_depth = Gauge("finops_queue_depth", "Pending executions", ["queue"], registry=REGISTRY)
active_executions = Gauge("finops_active_executions", "In-flight executions", registry=REGISTRY)
process_cpu_percent = Gauge("finops_process_cpu_percent", "Process CPU percent", registry=REGISTRY)
process_memory_bytes = Gauge("finops_process_memory_bytes", "Process RSS bytes", registry=REGISTRY)
system_load1 = Gauge("finops_system_load1", "1 minute load average", registry=REGISTRY)
gpu_utilisation = Gauge("finops_gpu_utilisation_percent", "GPU utilisation", ["index"], registry=REGISTRY)

_last_cpu = (time.monotonic(), time.process_time())


def _read_rss_bytes() -> float:
    try:
        with open(f"/proc/{os.getpid()}/statm") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0.0


def collect_runtime_gauges() -> None:
    """Refresh host-level gauges. Called before scraping /metrics."""
    global _last_cpu
    now_wall, now_cpu = time.monotonic(), time.process_time()
    prev_wall, prev_cpu = _last_cpu
    elapsed = max(now_wall - prev_wall, 1e-6)
    process_cpu_percent.set(min(100.0, (now_cpu - prev_cpu) / elapsed * 100.0))
    _last_cpu = (now_wall, now_cpu)
    process_memory_bytes.set(_read_rss_bytes())
    try:
        system_load1.set(os.getloadavg()[0])
    except OSError:
        pass
    _collect_gpu()


def _collect_gpu() -> None:
    try:
        import shutil
        import subprocess

        if not shutil.which("nvidia-smi"):
            return
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in out.stdout.strip().splitlines():
            idx, util = (p.strip() for p in line.split(","))
            gpu_utilisation.labels(index=idx).set(float(util))
    except Exception:
        return


def render_metrics() -> bytes:
    collect_runtime_gauges()
    return generate_latest(REGISTRY)
