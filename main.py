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

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None

HTTPXError: type[Exception] = Exception
if httpx:
    HTTPXError = httpx.HTTPError


@dataclass
class CpuStats:
    name: str = ""
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


@dataclass
class RemoteHost:
    alias: str
    url: str
    token: str = ""
    enabled: bool = True


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


def _get_cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                return str(value).strip()
        except Exception:
            pass

    try:
        name = platform.processor()
        return str(name).strip()
    except Exception:
        return ""


def _as_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value))
    except Exception:
        return None


def _as_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return int(float(value))
        return int(float(str(value)))
    except Exception:
        return None


def _normalize_status_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.endswith("/"):
        url = url[:-1]
    if url.lower().endswith("/status"):
        return url
    return url + "/status"


def _parse_remote_hosts(value: object) -> list[RemoteHost]:
    if not isinstance(value, list):
        return []
    hosts: list[RemoteHost] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("alias", "")).strip()
        url = str(item.get("url", "")).strip()
        if not alias or not url:
            continue
        hosts.append(
            RemoteHost(
                alias=alias,
                url=url,
                token=str(item.get("token", "")).strip(),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return hosts


async def _fetch_remote_status(url: str, token: str) -> dict:
    if not httpx:
        raise RuntimeError("缺少依赖 httpx，请检查 requirements.txt")

    status_url = _normalize_status_url(url)
    if not status_url:
        raise RuntimeError("远程 URL 为空")

    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    timeout = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=3.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(status_url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("远程返回不是 JSON 对象")
        return data


def _remote_payload_to_stats(payload: dict) -> tuple[str, str, Optional[str], Optional[CpuStats], Optional[MemoryStats], list[GpuStats]]:
    host = str(payload.get("host") or payload.get("hostname") or "").strip()
    os_line = str(payload.get("os") or payload.get("os_line") or "").strip()

    timestamp = payload.get("timestamp") or payload.get("time")
    timestamp_str: Optional[str] = None
    if isinstance(timestamp, str):
        timestamp_str = timestamp.strip() or None
    elif isinstance(timestamp, (int, float)):
        try:
            timestamp_str = dt.datetime.fromtimestamp(float(timestamp)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except Exception:
            timestamp_str = None

    cpu_obj = payload.get("cpu")
    cpu_name = str(payload.get("cpu_name") or "").strip()
    cpu_usage = _as_float(payload.get("cpu_percent") or payload.get("cpu_usage"))
    cpu_temp = _as_float(payload.get("cpu_temp") or payload.get("cpu_temperature"))
    if isinstance(cpu_obj, dict):
        cpu_name = str(cpu_obj.get("name") or cpu_name).strip()
        cpu_usage = _as_float(cpu_obj.get("usage_percent") or cpu_obj.get("percent") or cpu_usage)
        cpu_temp = _as_float(cpu_obj.get("temperature_c") or cpu_obj.get("temp_c") or cpu_temp)

    cpu: Optional[CpuStats] = None
    if cpu_name or cpu_usage is not None or cpu_temp is not None:
        cpu = CpuStats(name=cpu_name, usage_percent=cpu_usage, temperature_c=cpu_temp)

    mem_obj = payload.get("memory")
    mem_used = _as_int(payload.get("mem_used") or payload.get("memory_used"))
    mem_total = _as_int(payload.get("mem_total") or payload.get("memory_total"))
    mem_percent = _as_float(payload.get("mem_percent") or payload.get("memory_percent"))
    if isinstance(mem_obj, dict):
        mem_used = _as_int(mem_obj.get("used_bytes") or mem_obj.get("used") or mem_used)
        mem_total = _as_int(mem_obj.get("total_bytes") or mem_obj.get("total") or mem_total)
        mem_percent = _as_float(mem_obj.get("percent") or mem_percent)

    memory: Optional[MemoryStats] = None
    if mem_used is not None or mem_total is not None or mem_percent is not None:
        memory = MemoryStats(used_bytes=mem_used, total_bytes=mem_total, percent=mem_percent)

    gpus: list[GpuStats] = []
    gpus_obj = payload.get("gpus")
    if isinstance(gpus_obj, list):
        for gpu_item in gpus_obj:
            if not isinstance(gpu_item, dict):
                continue
            name = str(gpu_item.get("name") or gpu_item.get("gpu_name") or "GPU").strip() or "GPU"
            util = _as_float(
                gpu_item.get("utilization_percent")
                or gpu_item.get("util")
                or gpu_item.get("percent")
            )
            temp = _as_float(
                gpu_item.get("temperature_c")
                or gpu_item.get("temp_c")
                or gpu_item.get("temperature")
            )
            mem_used_mib = _as_int(
                gpu_item.get("memory_used_mib")
                or gpu_item.get("mem_used_mib")
                or gpu_item.get("vram_used_mib")
            )
            mem_total_mib = _as_int(
                gpu_item.get("memory_total_mib")
                or gpu_item.get("mem_total_mib")
                or gpu_item.get("vram_total_mib")
            )
            gpus.append(
                GpuStats(
                    name=name,
                    utilization_percent=util,
                    temperature_c=temp,
                    memory_used_mib=mem_used_mib,
                    memory_total_mib=mem_total_mib,
                )
            )

    return host, os_line, timestamp_str, cpu, memory, gpus


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


async def _get_windows_video_controller_names() -> list[str]:
    if os.name != "nt":
        return []

    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_VideoController | "
        "Select-Object -ExpandProperty Name"
    )
    code, out, _ = await _run_powershell(script, timeout=2.0)
    if code != 0 or not out:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for line in out.splitlines():
        name = line.strip()
        if not name:
            continue
        lower = name.lower()
        if "microsoft basic display" in lower:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        names.append(name)
    return names


async def _get_windows_gpu_utilization_percent() -> Optional[float]:
    if os.name != "nt":
        return None

    # Windows 性能计数器：\GPU Engine(*)\Utilization Percentage
    # 该计数器通常需要 Win10+，返回多个 engine 实例。
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "(Get-Counter -Counter '\\GPU Engine(*)\\Utilization Percentage')." 
        "CounterSamples | Select-Object -ExpandProperty CookedValue"
    )
    code, out, _ = await _run_powershell(script, timeout=2.5)
    if code != 0 or not out:
        return None

    values = [v for v in _parse_floats(out) if 0.0 <= v <= 100.0]
    if not values:
        return None

    # 取最大值作为整体占用的近似值（避免对多 engine 求和超过 100%）。
    return float(max(values))


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
        # 兜底：至少拿到显卡名称（AMD/Intel 等无 nvidia-smi 的情况）
        names = await _get_windows_video_controller_names()
        if not names:
            return []

        util = await _get_windows_gpu_utilization_percent()
        if len(names) == 1:
            return [
                GpuStats(
                    name=names[0],
                    utilization_percent=util,
                    temperature_c=None,
                    memory_used_mib=None,
                    memory_total_mib=None,
                )
            ]

        # 多显卡时不强行分配占用（性能计数器难以准确拆分到每张卡）
        return [
            GpuStats(
                name=n,
                utilization_percent=None,
                temperature_c=None,
                memory_used_mib=None,
                memory_total_mib=None,
            )
            for n in names
        ]

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
    title_text: str,
    host: str,
    os_line: str,
    timestamp: Optional[str],
    cpu: Optional[CpuStats],
    memory: Optional[MemoryStats],
    gpus: list[GpuStats],
    show_host: bool,
    show_os: bool,
    show_cpu_name: bool,
    show_cpu_usage: bool,
    show_cpu_temp: bool,
    show_memory: bool,
    show_gpu_name: bool,
    show_gpu_usage: bool,
    show_gpu_temp: bool,
    show_gpu_memory: bool,
) -> str:
    title_text = title_text.strip() or "系统状态"
    lines: list[str] = [title_text]
    if show_host:
        lines.append(f"主机：{host}")
    if show_os:
        lines.append(f"系统：{os_line}")
    if timestamp:
        lines.append(f"时间：{timestamp}")

    want_cpu_line = show_cpu_name or show_cpu_usage or show_cpu_temp
    if cpu and want_cpu_line:
        cpu_parts: list[str] = []
        if show_cpu_name and cpu.name:
            cpu_parts.append(cpu.name)
        if show_cpu_usage:
            cpu_parts.append(f"占用 {_format_percent(cpu.usage_percent)}")
        if show_cpu_temp:
            cpu_parts.append(f"温度 {_format_temp(cpu.temperature_c)}")
        lines.append("处理器：" + (" | ".join(cpu_parts) if cpu_parts else "暂无"))

    if show_memory:
        if memory and memory.used_bytes is not None and memory.total_bytes is not None:
            mem_text = f"{_format_bytes(memory.used_bytes)} / {_format_bytes(memory.total_bytes)}"
        else:
            mem_text = "暂无"
        if memory and memory.percent is not None:
            mem_text += f" ({memory.percent:.0f}%)"
        lines.append(f"内存：{mem_text}")

    want_gpu_line = show_gpu_name or show_gpu_usage or show_gpu_temp or show_gpu_memory
    if want_gpu_line:
        if not gpus:
            lines.append("显卡：暂无")
        else:
            for idx, gpu in enumerate(gpus):
                parts: list[str] = []
                if show_gpu_name and gpu.name:
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


@register(
    "winsysinfo",
    "SanHans",
    "使用 /info 查看系统状态",
    "0.3.3",
    "https://github.com/SanHans/astrbot_plugin_WinSysInfo",
)
class WinSysInfo(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}

    @filter.command("info")
    async def info(self, event: AstrMessageEvent, target: str = ""):
        """查看当前系统状态"""
        start = time.time()

        show_cpu_usage = bool(self.config.get("show_cpu_usage", True))
        show_cpu_temp = bool(self.config.get("show_cpu_temp", True))
        show_memory = bool(self.config.get("show_memory", True))
        show_gpu_usage = bool(self.config.get("show_gpu_usage", True))
        show_gpu_name = bool(self.config.get("show_gpu_name", True))
        show_gpu_temp = bool(self.config.get("show_gpu_temp", True))
        show_gpu_memory = bool(self.config.get("show_gpu_memory", True))
        show_timestamp = bool(self.config.get("show_timestamp", True))
        show_host = bool(self.config.get("show_host", True))
        show_os = bool(self.config.get("show_os", True))
        show_cpu_name = bool(self.config.get("show_cpu_name", True))
        title_text = str(self.config.get("title_text", "系统状态"))

        data_source = str(self.config.get("data_source", "本机")).strip()
        remote_default_alias = str(self.config.get("remote_default_alias", "")).strip()
        remote_hosts = _parse_remote_hosts(self.config.get("remote_hosts", []))

        target = (target or "").strip()
        use_remote = bool(target) or data_source == "远程"

        if use_remote:
            try:
                url = ""
                token = ""
                alias = ""

                if "://" in target:
                    url = target
                else:
                    alias = target or remote_default_alias
                    if not alias:
                        yield event.plain_result(
                            "WinSysInfo：未指定远程主机。请在配置中添加远程主机，并设置默认远程别名，或使用 /info <别名>。"
                        )
                        return

                    host_cfg = next(
                        (h for h in remote_hosts if h.alias == alias),
                        None,
                    )
                    if not host_cfg:
                        yield event.plain_result(
                            f"WinSysInfo：找不到远程主机别名「{alias}」。请检查插件配置。"
                        )
                        return
                    if not host_cfg.enabled:
                        yield event.plain_result(
                            f"WinSysInfo：远程主机「{alias}」未启用。"
                        )
                        return
                    url = host_cfg.url
                    token = host_cfg.token

                payload = await _fetch_remote_status(url=url, token=token)
                r_host, r_os, r_ts, r_cpu, r_mem, r_gpus = _remote_payload_to_stats(payload)

                host = r_host or alias or "远程主机"
                os_line = r_os or ""
                timestamp = r_ts if show_timestamp else None
                if show_timestamp and not timestamp:
                    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                want_cpu = show_cpu_name or show_cpu_usage or show_cpu_temp
                want_gpu = show_gpu_name or show_gpu_usage or show_gpu_temp or show_gpu_memory
                cpu = r_cpu if want_cpu else None
                if want_cpu and cpu is None:
                    cpu = CpuStats(name="")
                memory = r_mem
                gpus = r_gpus if want_gpu else []

            except HTTPXError as exc:
                logger.error(f"WinSysInfo 远程请求失败: {exc!r}")
                yield event.plain_result("WinSysInfo：远程请求失败，请检查 URL/端口/网络与 Token。")
                return
            except Exception as exc:
                logger.error(f"WinSysInfo 远程获取失败: {exc!r}")
                yield event.plain_result("WinSysInfo：远程获取失败，请查看日志。")
                return

            text = _build_text_reply(
                title_text=title_text,
                host=host,
                os_line=os_line,
                timestamp=timestamp,
                cpu=cpu,
                memory=memory,
                gpus=gpus,
                show_host=show_host,
                show_os=show_os,
                show_cpu_name=show_cpu_name,
                show_cpu_usage=show_cpu_usage,
                show_cpu_temp=show_cpu_temp,
                show_memory=show_memory,
                show_gpu_name=show_gpu_name,
                show_gpu_usage=show_gpu_usage,
                show_gpu_temp=show_gpu_temp,
                show_gpu_memory=show_gpu_memory,
            )
            yield event.plain_result(text)

            elapsed_ms = int((time.time() - start) * 1000)
            logger.info(f"WinSysInfo /info(远程) 处理完成: {elapsed_ms}ms")
            return

        host = socket.gethostname()
        os_line = f"{platform.system()} {platform.release()} ({platform.machine()})"
        timestamp = (
            dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if show_timestamp else None
        )

        want_cpu = show_cpu_name or show_cpu_usage or show_cpu_temp
        want_gpu = show_gpu_name or show_gpu_usage or show_gpu_temp or show_gpu_memory

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
            cpu_name = _get_cpu_name() if show_cpu_name else ""
            cpu = CpuStats(
                name=cpu_name,
                usage_percent=float(cpu_usage) if cpu_usage is not None else None,
                temperature_c=float(cpu_temp) if cpu_temp is not None else None,
            )

        text = _build_text_reply(
            title_text=title_text,
            host=host,
            os_line=os_line,
            timestamp=timestamp,
            cpu=cpu,
            memory=memory,
            gpus=gpus,
            show_host=show_host,
            show_os=show_os,
            show_cpu_name=show_cpu_name,
            show_cpu_usage=show_cpu_usage,
            show_cpu_temp=show_cpu_temp,
            show_memory=show_memory,
            show_gpu_name=show_gpu_name,
            show_gpu_usage=show_gpu_usage,
            show_gpu_temp=show_gpu_temp,
            show_gpu_memory=show_gpu_memory,
        )
        yield event.plain_result(text)

        elapsed_ms = int((time.time() - start) * 1000)
        logger.info(f"WinSysInfo /info 处理完成: {elapsed_ms}ms")
