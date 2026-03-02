from __future__ import annotations

import os
import platform
import socket
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


app = FastAPI(title="WinSysInfo Agent")


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
        "mem_percent": mem_percent,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "note": "可按需扩展为返回温度/显卡等信息",
    }
