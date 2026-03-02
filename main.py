from __future__ import annotations

import asyncio
import datetime as dt
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


@dataclass
class CpuStats:
    usage_percent: Optional[float] = None
    temperature_c: Optional[float] = None


@dataclass
class MemoryStats:
    used_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    percent: Optional[float] = None


@dataclass
class GpuStats:
    name: str
    utilization_percent: Optional[float] = None
    temperature_c: Optional[float] = None
    memory_used_mib: Optional[int] = None
    memory_total_mib: Optional[int] = None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _format_bytes(num_bytes: int) -> str:
    value = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _format_temp(value_c: Optional[float]) -> str:
    if value_c is None:
        return "暂无"
    return f"{value_c:.1f}C"


def _format_percent(value: Optional[float]) -> str:
    if value is None:
        return "暂无"
    return f"{value:.0f}%"


def _percent_int(value: Optional[float]) -> int:
    if value is None:
        return 0
    return int(round(_clamp(float(value), 0.0, 100.0)))


def _get_creationflags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


async def _run_command(args: list[str], timeout: float = 2.5) -> tuple[int, str, str]:
    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_get_creationflags(),
        )

    try:
        proc = await asyncio.to_thread(_run)
    except Exception as exc:
        return 1, "", str(exc)

    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _powershell_exe() -> Optional[str]:
    for name in ("pwsh", "powershell", "powershell.exe"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


async def _run_powershell(script: str, timeout: float = 3.0) -> tuple[int, str, str]:
    exe = _powershell_exe()
    if not exe:
        return 127, "", "powershell not found"
    args = [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    return await _run_command(args, timeout=timeout)


def _parse_floats(text: str) -> list[float]:
    values: list[float] = []
    for match in re.finditer(r"-?\d+(?:\.\d+)?", text):
        try:
            values.append(float(match.group(0)))
        except Exception:
            continue
    return values


async def _get_cpu_usage_percent() -> Optional[float]:
    if not psutil:
        return None
    try:
        return float(await asyncio.to_thread(psutil.cpu_percent, interval=0.2))
    except Exception:
        return None


def _get_memory_fallback_windows() -> Optional[MemoryStats]:
    if os.name != "nt":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        if not ok:
            return None

        total = int(status.ullTotalPhys)
        avail = int(status.ullAvailPhys)
        used = max(0, total - avail)
        percent = float(status.dwMemoryLoad)
        return MemoryStats(used_bytes=used, total_bytes=total, percent=percent)
    except Exception:
        return None


async def _get_memory_stats() -> Optional[MemoryStats]:
    if psutil:
        try:
            vm = await asyncio.to_thread(psutil.virtual_memory)
            return MemoryStats(
                used_bytes=int(vm.used),
                total_bytes=int(vm.total),
                percent=float(vm.percent),
            )
        except Exception:
            pass

    return _get_memory_fallback_windows()


async def _get_hwmon_sensor_max(
    sensor_type: str, name_match: str
) -> Optional[float]:
    if os.name != "nt":
        return None

    namespaces = ["root\\LibreHardwareMonitor", "root\\OpenHardwareMonitor"]
    for ns in namespaces:
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"Get-CimInstance -Namespace '{ns}' -ClassName Sensor | "
            "Where-Object { $_.SensorType -eq '"
            + sensor_type
            + "' -and $_.Name -match '"
            + name_match
            + "' } | Select-Object -ExpandProperty Value"
        )
        code, out, _ = await _run_powershell(script, timeout=2.0)
        if code != 0 or not out:
            continue

        values = _parse_floats(out)
        if sensor_type == "Temperature":
            values = [v for v in values if -20.0 <= v <= 150.0]
        elif sensor_type == "Load":
            values = [v for v in values if 0.0 <= v <= 100.0]

        if values:
            return float(max(values))

    return None


async def _get_acpi_thermalzone_c() -> Optional[float]:
    if os.name != "nt":
        return None

    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | "
        "ForEach-Object { ($_.CurrentTemperature/10) - 273.15 }"
    )
    code, out, _ = await _run_powershell(script, timeout=2.0)
    if code != 0 or not out:
        return None

    values = [v for v in _parse_floats(out) if -20.0 <= v <= 150.0]
    if not values:
        return None
    return float(max(values))


async def _get_cpu_temperature_c() -> Optional[float]:
    temp = await _get_hwmon_sensor_max("Temperature", "CPU")
    if temp is not None:
        return temp
    return await _get_acpi_thermalzone_c()


def _nvidia_smi_path() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    common = r"C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"
    if os.path.exists(common):
        return common
    return None


def _safe_int(text: str) -> Optional[int]:
    text = text.strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return int(float(text))
    except Exception:
        return None


async def _get_nvidia_gpus() -> list[GpuStats]:
    exe = _nvidia_smi_path()
    if not exe:
        return []

    args = [
        exe,
        "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    code, out, _ = await _run_command(args, timeout=2.5)
    if code != 0 or not out:
        return []

    gpus: list[GpuStats] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts:
            continue

        name = parts[0] if len(parts) >= 1 else "GPU"
        util = _safe_int(parts[1]) if len(parts) >= 2 else None
        temp = _safe_int(parts[2]) if len(parts) >= 3 else None
        mem_used = _safe_int(parts[3]) if len(parts) >= 4 else None
        mem_total = _safe_int(parts[4]) if len(parts) >= 5 else None

        gpus.append(
            GpuStats(
                name=name,
                utilization_percent=float(util) if util is not None else None,
                temperature_c=float(temp) if temp is not None else None,
                memory_used_mib=mem_used,
                memory_total_mib=mem_total,
            )
        )
    return gpus


async def _get_gpu_stats() -> list[GpuStats]:
    gpus = await _get_nvidia_gpus()
    if gpus:
        return gpus

    gpu_temp = await _get_hwmon_sensor_max("Temperature", "GPU")
    gpu_load = await _get_hwmon_sensor_max("Load", "GPU")
    if gpu_temp is None and gpu_load is None:
        return []

    return [
        GpuStats(
            name="GPU",
            utilization_percent=gpu_load,
            temperature_c=gpu_temp,
            memory_used_mib=None,
            memory_total_mib=None,
        )
    ]


def _build_text_reply(
    host: str,
    os_line: str,
    timestamp: Optional[str],
    cpu: Optional[CpuStats],
    memory: Optional[MemoryStats],
    gpus: list[GpuStats],
    show_cpu_usage: bool,
    show_cpu_temp: bool,
    show_memory: bool,
    show_gpu_usage: bool,
    show_gpu_temp: bool,
    show_gpu_memory: bool,
) -> str:
    lines: list[str] = ["WinSysInfo 系统状态", f"主机：{host}", f"系统：{os_line}"]
    if timestamp:
        lines.append(f"时间：{timestamp}")

    if cpu and (show_cpu_usage or show_cpu_temp):
        cpu_parts: list[str] = []
        if show_cpu_usage:
            cpu_parts.append(f"占用 {_format_percent(cpu.usage_percent)}")
        if show_cpu_temp:
            cpu_parts.append(f"温度 {_format_temp(cpu.temperature_c)}")
        lines.append("处理器：" + (" | ".join(cpu_parts) if cpu_parts else "暂无"))

    if memory and show_memory:
        if memory.used_bytes is not None and memory.total_bytes is not None:
            mem_text = f"{_format_bytes(memory.used_bytes)} / {_format_bytes(memory.total_bytes)}"
        else:
            mem_text = "暂无"
        if memory.percent is not None:
            mem_text += f" ({memory.percent:.0f}%)"
        lines.append(f"内存：{mem_text}")

    want_gpu = show_gpu_usage or show_gpu_temp or show_gpu_memory
    if want_gpu:
        if not gpus:
            lines.append("显卡：暂无")
        else:
            for idx, gpu in enumerate(gpus):
                parts: list[str] = []
                if not (len(gpus) == 1 and gpu.name == "GPU"):
                    parts.append(gpu.name)
                if show_gpu_usage:
                    parts.append(f"占用 {_format_percent(gpu.utilization_percent)}")
                if show_gpu_temp:
                    parts.append(f"温度 {_format_temp(gpu.temperature_c)}")
                if show_gpu_memory:
                    if gpu.memory_used_mib is not None and gpu.memory_total_mib is not None:
                        pct = (
                            (gpu.memory_used_mib / gpu.memory_total_mib) * 100
                            if gpu.memory_total_mib > 0
                            else 0.0
                        )
                        parts.append(
                            f"显存 {gpu.memory_used_mib}/{gpu.memory_total_mib} MiB ({pct:.0f}%)"
                        )
                    else:
                        parts.append("显存 暂无")
                prefix = f"显卡{idx + 1}：" if len(gpus) > 1 else "显卡："
                lines.append(prefix + " | ".join(parts))

    return "\n".join(lines)


STATUS_TEMPLATE = r"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root {
        --bg1: #0b1220;
        --bg2: #0a2a2f;
        --card: rgba(255, 255, 255, 0.08);
        --card2: rgba(255, 255, 255, 0.10);
        --text: rgba(255, 255, 255, 0.92);
        --muted: rgba(255, 255, 255, 0.65);
        --accent: #43d6b0;
        --accent2: #4aa3ff;
        --warn: #ffb020;
        --danger: #ff5a6a;
        --shadow: rgba(0, 0, 0, 0.35);
      }

      * { box-sizing: border-box; }

      body {
        margin: 0;
        color: var(--text);
        font-family: "Microsoft YaHei UI", "Microsoft YaHei", Bahnschrift, "Segoe UI", sans-serif;
        background:
          radial-gradient(1200px 600px at 10% 10%, rgba(67, 214, 176, 0.25), transparent 60%),
          radial-gradient(900px 500px at 90% 20%, rgba(74, 163, 255, 0.22), transparent 55%),
          radial-gradient(700px 500px at 50% 100%, rgba(255, 176, 32, 0.12), transparent 60%),
          linear-gradient(180deg, var(--bg1), var(--bg2));
      }

      .wrap {
        width: 980px;
        padding: 28px 28px 30px;
        margin: 0 auto;
      }

      .top {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 18px;
      }

      .title {
        font-size: 26px;
        font-weight: 700;
        letter-spacing: 0.4px;
      }

      .meta {
        text-align: right;
        color: var(--muted);
        font-size: 12.5px;
        line-height: 1.35;
      }

      .grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 14px;
      }

      .card {
        background: linear-gradient(180deg, var(--card2), var(--card));
        border: 1px solid rgba(255, 255, 255, 0.10);
        border-radius: 16px;
        padding: 16px 16px 14px;
        box-shadow: 0 14px 40px var(--shadow);
        backdrop-filter: blur(10px);
      }

      .card-head {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        margin-bottom: 10px;
      }

      .card-title {
        font-weight: 700;
        letter-spacing: 0.3px;
      }

      .card-sub {
        color: var(--muted);
        font-size: 12px;
        max-width: 220px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .big {
        font-size: 38px;
        font-weight: 800;
        letter-spacing: 0.3px;
        margin: 2px 0 8px;
      }

      .row {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
        margin-bottom: 10px;
      }

      .pill {
        border: 1px solid rgba(255, 255, 255, 0.14);
        background: rgba(0, 0, 0, 0.18);
        border-radius: 999px;
        padding: 6px 10px;
        font-size: 12px;
        color: rgba(255, 255, 255, 0.82);
      }

      .bar {
        height: 10px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.10);
        overflow: hidden;
      }

      .fill {
        height: 100%;
        border-radius: 999px;
        background: linear-gradient(90deg, var(--accent), var(--accent2));
        width: 0;
      }

      .fill.warm { background: linear-gradient(90deg, var(--warn), #ff6b1a); }
      .fill.hot { background: linear-gradient(90deg, var(--danger), #ff2e49); }

      .foot {
        margin-top: 14px;
        color: rgba(255, 255, 255, 0.50);
        font-size: 11.5px;
      }

      .span-2 { grid-column: span 2; }
      .span-3 { grid-column: span 3; }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="top">
        <div>
          <div class="title">{{ title }}</div>
          <div class="meta">
            {% if show_host %}{{ host }}{% endif %}
            {% if show_host and show_os %}<br />{% endif %}
            {% if show_os %}{{ os }}{% endif %}
          </div>
        </div>
        <div class="meta">
          {% if timestamp %}{{ timestamp }}{% endif %}
        </div>
      </div>

      <div class="grid">
        {% if cpu %}
        <div class="card">
          <div class="card-head">
            <div class="card-title">处理器</div>
            <div class="card-sub">{{ cpu.subtitle }}</div>
          </div>
          <div class="big">{{ cpu.usage_str }}</div>
          <div class="row">
            {% if cpu.temp_str %}<div class="pill">温度 {{ cpu.temp_str }}</div>{% endif %}
          </div>
          <div class="bar"><div class="fill {{ cpu.fill_class }}" style="width: {{ cpu.usage_pct }}%"></div></div>
        </div>
        {% endif %}

        {% if memory %}
        <div class="card">
          <div class="card-head">
            <div class="card-title">内存</div>
            <div class="card-sub">{{ memory.detail }}</div>
          </div>
          <div class="big">{{ memory.usage_str }}</div>
          <div class="row">
            <div class="pill">已用 {{ memory.used_str }}</div>
            <div class="pill">总计 {{ memory.total_str }}</div>
          </div>
          <div class="bar"><div class="fill {{ memory.fill_class }}" style="width: {{ memory.usage_pct }}%"></div></div>
        </div>
        {% endif %}

        {% if gpus and gpus|length == 1 %}
        {% set g = gpus[0] %}
        <div class="card">
          <div class="card-head">
            <div class="card-title">显卡</div>
            <div class="card-sub">{% if show_gpu_name %}{{ g.name }}{% else %}状态{% endif %}</div>
          </div>
          <div class="big">{{ g.util_str }}</div>
          <div class="row">
            {% if g.temp_str %}<div class="pill">温度 {{ g.temp_str }}</div>{% endif %}
            {% if g.vram_str %}<div class="pill">显存 {{ g.vram_str }}</div>{% endif %}
          </div>
          <div class="bar"><div class="fill {{ g.fill_class }}" style="width: {{ g.util_pct }}%"></div></div>
        </div>
        {% endif %}

        {% if gpus and gpus|length > 1 %}
        <div class="card span-3">
          <div class="card-head">
            <div class="card-title">显卡</div>
            <div class="card-sub">{{ gpus|length }} 张</div>
          </div>
          {% for g in gpus %}
          <div style="margin-bottom: 12px;">
            <div style="display:flex; justify-content:space-between; gap: 10px; align-items:baseline; margin-bottom:6px;">
              <div style="font-weight:700;">{% if show_gpu_name %}{{ g.name }}{% else %}显卡{{ loop.index }}{% endif %}</div>
              <div style="color: var(--muted); font-size: 12px;">占用 {{ g.util_str }}{% if g.temp_str %} | 温度 {{ g.temp_str }}{% endif %}{% if g.vram_str %} | 显存 {{ g.vram_str }}{% endif %}</div>
            </div>
            <div class="bar"><div class="fill {{ g.fill_class }}" style="width: {{ g.util_pct }}%"></div></div>
          </div>
          {% endfor %}
        </div>
        {% endif %}

        {% if not cpu and not memory and (not gpus or gpus|length == 0) %}
        <div class="card span-3">
          <div class="card-head">
            <div class="card-title">未启用任何指标</div>
            <div class="card-sub">请在插件配置中启用需要展示的内容</div>
          </div>
          <div class="big">--</div>
          <div class="foot">提示：在 WinSysInfo 配置页中启用处理器/内存/显卡开关。</div>
        </div>
        {% endif %}
      </div>

      <div class="foot">由 WinSysInfo 生成</div>
    </div>
  </body>
</html>
"""


def _fill_class_for_percent(pct: int) -> str:
    if pct >= 90:
        return "hot"
    if pct >= 75:
        return "warm"
    return ""


def _build_image_data(
    host: str,
    os_line: str,
    timestamp: Optional[str],
    cpu: Optional[CpuStats],
    memory: Optional[MemoryStats],
    gpus: list[GpuStats],
    show_cpu_usage: bool,
    show_cpu_temp: bool,
    show_memory: bool,
    show_gpu_usage: bool,
    show_gpu_temp: bool,
    show_gpu_memory: bool,
) -> dict:
    data: dict = {
        "host": host,
        "os": os_line,
        "timestamp": timestamp or "",
        "cpu": None,
        "memory": None,
        "gpus": [],
    }

    if cpu and (show_cpu_usage or show_cpu_temp):
        usage_pct = _percent_int(cpu.usage_percent) if show_cpu_usage else 0
        data["cpu"] = {
            "subtitle": f"{os.cpu_count() or '未知'} 核",
            "usage_str": _format_percent(cpu.usage_percent) if show_cpu_usage else "--",
            "usage_pct": usage_pct,
            "temp_str": _format_temp(cpu.temperature_c) if show_cpu_temp else "",
            "fill_class": _fill_class_for_percent(usage_pct),
        }

    if memory and show_memory:
        usage_pct = _percent_int(memory.percent)
        used_str = _format_bytes(memory.used_bytes) if memory.used_bytes is not None else "暂无"
        total_str = _format_bytes(memory.total_bytes) if memory.total_bytes is not None else "暂无"
        data["memory"] = {
            "detail": "物理内存",
            "usage_str": _format_percent(memory.percent),
            "usage_pct": usage_pct,
            "used_str": used_str,
            "total_str": total_str,
            "fill_class": _fill_class_for_percent(usage_pct),
        }

    want_gpu = show_gpu_usage or show_gpu_temp or show_gpu_memory
    if want_gpu:
        gpu_items: list[dict] = []
        for gpu in gpus:
            util_pct = _percent_int(gpu.utilization_percent) if show_gpu_usage else 0
            vram_str = ""
            if show_gpu_memory:
                if gpu.memory_used_mib is not None and gpu.memory_total_mib is not None:
                    pct = (
                        (gpu.memory_used_mib / gpu.memory_total_mib) * 100
                        if gpu.memory_total_mib > 0
                        else 0.0
                    )
                    vram_str = f"{gpu.memory_used_mib}/{gpu.memory_total_mib} MiB ({pct:.0f}%)"
                else:
                    vram_str = "暂无"

            gpu_items.append(
                {
                    "name": gpu.name,
                    "util_str": _format_percent(gpu.utilization_percent) if show_gpu_usage else "--",
                    "util_pct": util_pct,
                    "temp_str": _format_temp(gpu.temperature_c) if show_gpu_temp else "",
                    "vram_str": vram_str,
                    "fill_class": _fill_class_for_percent(util_pct),
                }
            )

        if not gpu_items:
            gpu_items = [
                {
                    "name": "GPU",
                    "util_str": "暂无" if show_gpu_usage else "--",
                    "util_pct": 0,
                    "temp_str": "暂无" if show_gpu_temp else "",
                    "vram_str": "暂无" if show_gpu_memory else "",
                    "fill_class": "",
                }
            ]

        data["gpus"] = gpu_items

    return data


@register(
    "winsysinfo",
    "SanHans",
    "使用 /info 查看系统状态",
    "0.1.0",
    "https://github.com/SanHans/astrbot_plugin_WinSysInfo",
)
class WinSysInfo(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}

    @filter.command("info")
    async def info(self, event: AstrMessageEvent):
        """查看当前系统状态"""
        start = time.time()

        show_cpu_usage = bool(self.config.get("show_cpu_usage", True))
        show_cpu_temp = bool(self.config.get("show_cpu_temp", True))
        show_memory = bool(self.config.get("show_memory", True))
        show_gpu_usage = bool(self.config.get("show_gpu_usage", True))
        show_gpu_temp = bool(self.config.get("show_gpu_temp", True))
        show_gpu_memory = bool(self.config.get("show_gpu_memory", True))
        show_timestamp = bool(self.config.get("show_timestamp", True))
        output_mode_raw = str(self.config.get("output_mode", "文字")).strip()
        output_mode = output_mode_raw.lower()
        is_image = output_mode in {"image", "img", "pic", "png", "图片", "图", "图像"}

        host = socket.gethostname()
        os_line = f"{platform.system()} {platform.release()} ({platform.machine()})"
        timestamp = (
            dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if show_timestamp else None
        )

        want_cpu = show_cpu_usage or show_cpu_temp
        want_gpu = show_gpu_usage or show_gpu_temp or show_gpu_memory

        cpu_usage_task = _get_cpu_usage_percent() if show_cpu_usage else None
        cpu_temp_task = _get_cpu_temperature_c() if show_cpu_temp else None
        mem_task = _get_memory_stats() if show_memory else None
        gpu_task = _get_gpu_stats() if want_gpu else None

        try:
            cpu_usage, cpu_temp, memory, gpus = await asyncio.gather(
                cpu_usage_task if cpu_usage_task else asyncio.sleep(0, result=None),
                cpu_temp_task if cpu_temp_task else asyncio.sleep(0, result=None),
                mem_task if mem_task else asyncio.sleep(0, result=None),
                gpu_task if gpu_task else asyncio.sleep(0, result=[]),
            )
        except Exception as exc:
            logger.error(f"WinSysInfo 获取指标失败: {exc!r}")
            yield event.plain_result("WinSysInfo 出错：获取系统状态失败，请查看日志。")
            return

        cpu: Optional[CpuStats] = None
        if want_cpu:
            cpu = CpuStats(
                usage_percent=float(cpu_usage) if cpu_usage is not None else None,
                temperature_c=float(cpu_temp) if cpu_temp is not None else None,
            )

        if is_image:
            try:
                image_data = _build_image_data(
                    host=host,
                    os_line=os_line,
                    timestamp=timestamp,
                    cpu=cpu,
                    memory=memory,
                    gpus=gpus,
                    show_cpu_usage=show_cpu_usage,
                    show_cpu_temp=show_cpu_temp,
                    show_memory=show_memory,
                    show_gpu_usage=show_gpu_usage,
                    show_gpu_temp=show_gpu_temp,
                    show_gpu_memory=show_gpu_memory,
                )
                image_path = await self.html_render(
                    STATUS_TEMPLATE,
                    image_data,
                    return_url=False,
                    options={"type": "png", "full_page": True},
                )
                yield event.image_result(image_path)
                return
            except Exception as exc:
                logger.error(f"WinSysInfo 图片渲染失败: {exc!r}")

        text = _build_text_reply(
            host=host,
            os_line=os_line,
            timestamp=timestamp,
            cpu=cpu,
            memory=memory,
            gpus=gpus,
            show_cpu_usage=show_cpu_usage,
            show_cpu_temp=show_cpu_temp,
            show_memory=show_memory,
            show_gpu_usage=show_gpu_usage,
            show_gpu_temp=show_gpu_temp,
            show_gpu_memory=show_gpu_memory,
        )
        yield event.plain_result(text)

        elapsed_ms = int((time.time() - start) * 1000)
        logger.info(f"WinSysInfo /info 处理完成: {elapsed_ms}ms")
