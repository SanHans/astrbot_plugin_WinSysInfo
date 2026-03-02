from __future__ import annotations

import os
import platform
import socket
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException

import csv
from pathlib import Path

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


app = FastAPI(title="WinSysInfo Agent")


def _get_hwinfo_log_path() -> str:
    return os.environ.get("WINSYSINFO_HWINFO_LOG", "").strip()


def _split_keys(value: str) -> list[str]:
    keys: list[str] = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part:
            keys.append(part)
    return keys


def _default_cpu_temp_keys() -> list[str]:
    return [
        "CPU Package",
        "CPU (Tctl/Tdie)",
        "CPU Tctl/Tdie",
        "CPU Die",
        "CPU",
    ]


def _default_gpu_temp_keys() -> list[str]:
    return [
        "GPU Temperature",
        "GPU Hot Spot Temperature",
        "GPU Hot Spot",
        "Hot Spot",
    ]


def _default_gpu_util_keys() -> list[str]:
    return [
        "GPU Core Load",
        "GPU Utilization",
        "GPU Load",
    ]


def _to_float(text: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    text = text.replace("%", "").replace("C", "").replace("°", "").strip()
    try:
        return float(text)
    except Exception:
        return None


def _read_hwinfo_csv_latest_row(path: str, max_tail_lines: int = 200) -> tuple[list[str], list[str]]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return [], []

    try:
        # HWiNFO CSV 可能是 ANSI/UTF-8，先用 utf-8 容错读取。
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return [], []

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], []

    tail = lines[-max_tail_lines:]
    header: list[str] = []
    last_row: list[str] = []

    # 在尾部范围里找最后一个可解析的 header+row
    # HWiNFO 日志一般首行就是 header，但这里做稳妥处理。
    for i in range(max(0, len(tail) - 50), len(tail) - 1):
        candidate = tail[i]
        if "," not in candidate:
            continue
        try:
            header = next(csv.reader([candidate]))
        except Exception:
            continue
        if len(header) < 3:
            continue
        # 从末尾往回找一行字段数相同的记录
        for j in range(len(tail) - 1, i, -1):
            try:
                row = next(csv.reader([tail[j]]))
            except Exception:
                continue
            if len(row) == len(header):
                last_row = row
                return header, last_row

    return [], []


def _extract_hwinfo_metrics() -> tuple[Optional[float], Optional[float], Optional[float], str]:
    """从 HWiNFO CSV 日志提取：CPU 温度、GPU 温度、GPU 占用。

    返回 (cpu_temp_c, gpu_temp_c, gpu_util_percent, gpu_name)
    """
    log_path = _get_hwinfo_log_path()
    if not log_path:
        return None, None, None, ""

    header, row = _read_hwinfo_csv_latest_row(log_path)
    if not header or not row:
        return None, None, None, ""

    cpu_keys = _split_keys(os.environ.get("WINSYSINFO_HWINFO_CPU_TEMP_KEYS", ""))
    gpu_temp_keys = _split_keys(os.environ.get("WINSYSINFO_HWINFO_GPU_TEMP_KEYS", ""))
    gpu_util_keys = _split_keys(os.environ.get("WINSYSINFO_HWINFO_GPU_UTIL_KEYS", ""))

    cpu_keys = cpu_keys or _default_cpu_temp_keys()
    gpu_temp_keys = gpu_temp_keys or _default_gpu_temp_keys()
    gpu_util_keys = gpu_util_keys or _default_gpu_util_keys()

    cpu_temp: Optional[float] = None
    gpu_temp: Optional[float] = None
    gpu_util: Optional[float] = None
    gpu_name = ""

    lowered = [h.lower() for h in header]

    def find_value_by_keys(keys: list[str]) -> tuple[Optional[float], str]:
        for key in keys:
            key_l = key.lower()
            for idx, col in enumerate(lowered):
                if key_l in col:
                    val = _to_float(row[idx])
                    if val is None:
                        continue
                    return val, header[idx]
        return None, ""

    cpu_temp, _ = find_value_by_keys(cpu_keys)
    gpu_temp, gpu_temp_col = find_value_by_keys(gpu_temp_keys)
    gpu_util, gpu_util_col = find_value_by_keys(gpu_util_keys)

    # 试着从列名里提取显卡名称（HWiNFO 常见格式："GPU [#0]: NVIDIA GeForce ..."）
    for col in (gpu_temp_col, gpu_util_col):
        if not col:
            continue
        m = col.split(":", 1)
        if len(m) == 2 and "gpu" in m[0].lower():
            gpu_name = m[0].strip()
            break

    return cpu_temp, gpu_temp, gpu_util, gpu_name


def _get_token() -> str:
    return os.environ.get("WINSYSINFO_TOKEN", "").strip()


def _auth(authorization: Optional[str]) -> None:
    token = _get_token()
    if not token:
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    got = authorization.removeprefix("Bearer ").strip()
    if got != token:
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/status")
def status(authorization: Optional[str] = Header(default=None)) -> dict:
    _auth(authorization)

    host = socket.gethostname()
    os_line = f"{platform.system()} {platform.release()} ({platform.machine()})"

    cpu_percent = None
    mem_percent = None
    mem_used = None
    mem_total = None
    cpu_temp_c = None
    gpu_temp_c = None
    gpu_util_percent = None
    gpu_name = ""

    hw_cpu_temp, hw_gpu_temp, hw_gpu_util, hw_gpu_name = _extract_hwinfo_metrics()
    cpu_temp_c = hw_cpu_temp
    gpu_temp_c = hw_gpu_temp
    gpu_util_percent = hw_gpu_util
    gpu_name = hw_gpu_name

    if psutil:
        try:
            cpu_percent = float(psutil.cpu_percent(interval=0.2))
        except Exception:
            cpu_percent = None

        try:
            vm = psutil.virtual_memory()
            mem_percent = float(vm.percent)
            mem_used = int(vm.used)
            mem_total = int(vm.total)
        except Exception:
            pass

    return {
        "host": host,
        "os": os_line,
        "timestamp": int(time.time()),
        "cpu_percent": cpu_percent,
        "cpu_temp": cpu_temp_c,
        "mem_percent": mem_percent,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "gpus": (
            [
                {
                    "name": gpu_name or "GPU",
                    "utilization_percent": gpu_util_percent,
                    "temperature_c": gpu_temp_c,
                }
            ]
            if (gpu_temp_c is not None or gpu_util_percent is not None or gpu_name)
            else []
        ),
        "note": "如需温度/显卡信息，建议启用 HWiNFO 传感器日志并配置 WINSYSINFO_HWINFO_LOG",
    }
