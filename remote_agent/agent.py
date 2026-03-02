from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException

import csv
from pathlib import Path
import re
import shutil
import json

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


def _is_windows() -> bool:
    return os.name == "nt"


def _get_creationflags() -> int:
    if not _is_windows():
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_powershell(script: str, timeout: float = 3.0) -> tuple[int, str, str]:
    if not _is_windows():
        return 127, "", "not windows"

    exe = None
    for name in ("pwsh", "powershell", "powershell.exe"):
        path = shutil.which(name)
        if path:
            exe = path
            break
    if not exe:
        exe = "powershell.exe"

    try:
        proc = subprocess.run(
            [
                exe,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_get_creationflags(),
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def _provider() -> str:
    return os.environ.get("WINSYSINFO_PROVIDER", "auto").strip().lower()


def _to_float_any(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("%", "").replace("C", "").replace("°", "").strip()
    try:
        return float(text)
    except Exception:
        return None


def _to_int_any(value: object) -> Optional[int]:
    f = _to_float_any(value)
    if f is None:
        return None
    try:
        return int(f)
    except Exception:
        return None


def _env_csv(value: str) -> list[str]:
    items: list[str] = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part:
            items.append(part)
    return items


def _aida_cpu_temp_ids() -> list[str]:
    return _env_csv(os.environ.get("WINSYSINFO_AIDA64_CPU_TEMP_IDS", "")) or [
        "TCPUPKG",
        "TCPU",
        "TCPUTCTL",
    ]


def _aida_gpu_temp_ids(index: int) -> list[str]:
    suffix = str(index)
    # 默认优先热点温度：TGPU1HOT
    return _env_csv(os.environ.get("WINSYSINFO_AIDA64_GPU_TEMP_IDS", "")) or [
        f"TGPU{suffix}HOT",
        f"TGPU{suffix}",
        f"TGPU{suffix}DIO",
    ]


def _aida_gpu_util_id(index: int) -> str:
    return f"SGPU{index}UTI"


def _aida_gpu_bus_type_id(index: int) -> str:
    return f"SGPU{index}BUSTYP"


def _aida_gpu_used_ded_mem_id(index: int) -> str:
    return f"SGPU{index}USEDDEMEM"


def _aida_gpu_used_dyn_mem_id(index: int) -> str:
    return f"SGPU{index}USEDDYMEM"


def _aida_cpu_util_id() -> str:
    return "SCPUUTI"


def _aida_registry_values() -> dict[str, str]:
    if not _is_windows():
        return {}
    try:
        import winreg  # type: ignore

        key_path = r"Software\FinalWire\AIDA64\SensorValues"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            values: dict[str, str] = {}
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not name:
                    continue
                raw_name = str(name)
                if raw_name.startswith("Label."):
                    continue

                # AIDA64 Registry 输出常见格式：Value.<ID> / Label.<ID>
                if raw_name.startswith("Value."):
                    raw_name = raw_name[len("Value.") :]

                values[raw_name] = str(data)
            return values
    except Exception:
        return {}


def _aida_wmi_values(ids: list[str]) -> dict[str, str]:
    if not _is_windows() or not ids:
        return {}

    # 用 PowerShell 从 Root\WMI\AIDA64_SensorValues 读取指定属性。
    # 输出格式：ID=value（每行一个）。
    joined = ",".join(ids)
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$o = Get-CimInstance -Namespace root/wmi -ClassName AIDA64_SensorValues | Select-Object -First 1;"
        f"$ids = '{joined}'.Split(',');"
        "foreach ($id in $ids) {"
        "  $v = $null;"
        "  try { $v = $o.$id } catch { $v = $null };"
        "  if ($null -eq $v) { try { $v = $o.('Value.' + $id) } catch { $v = $null } };"
        "  if ($null -ne $v) { Write-Output ($id + '=' + $v) }"
        "}"
    )
    code, out, _ = _run_powershell(script, timeout=3.0)
    if code != 0 or not out:
        return {}

    values: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not k:
            continue
        values[k] = v.strip()
    return values


def _aida_shared_memory_text() -> str:
    if not _is_windows():
        return ""

    try:
        import ctypes
        from ctypes import wintypes

        OpenFileMappingW = ctypes.windll.kernel32.OpenFileMappingW
        OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        OpenFileMappingW.restype = wintypes.HANDLE

        MapViewOfFile = ctypes.windll.kernel32.MapViewOfFile
        MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        MapViewOfFile.restype = ctypes.c_void_p

        UnmapViewOfFile = ctypes.windll.kernel32.UnmapViewOfFile
        UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        UnmapViewOfFile.restype = wintypes.BOOL

        CloseHandle = ctypes.windll.kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        VirtualQuery = ctypes.windll.kernel32.VirtualQuery
        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD),
            ]

        VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
        VirtualQuery.restype = ctypes.c_size_t

        FILE_MAP_READ = 0x0004
        handle = OpenFileMappingW(FILE_MAP_READ, False, "AIDA64_SensorValues")
        if not handle:
            return ""

        try:
            view = MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
            if not view:
                return ""

            try:
                mbi = MEMORY_BASIC_INFORMATION()
                size = VirtualQuery(ctypes.c_void_p(view), ctypes.byref(mbi), ctypes.sizeof(mbi))
                if size == 0:
                    return ""

                max_len = int(min(mbi.RegionSize, 1024 * 1024))
                raw = ctypes.string_at(view, max_len)
                raw = raw.split(b"\x00", 1)[0]

                try:
                    return raw.decode("utf-8", errors="replace")
                except Exception:
                    return raw.decode("latin-1", errors="replace")
            finally:
                UnmapViewOfFile(ctypes.c_void_p(view))
        finally:
            CloseHandle(handle)
    except Exception:
        return ""


def _aida_parse_values(text: str) -> dict[str, float]:
    """解析 AIDA64 共享内存 XML 片段，提取传感器 ID->数值。

    共享内存内容是 XML 标签片段（非完整 XML）。这里做宽松解析：
    - 优先匹配 id="XXX" value="YYY" 的属性
    - 其次匹配 <id>XXX</id> ... <value>YYY</value> 的结构
    """
    if not text:
        return {}

    values: dict[str, float] = {}

    for m in re.finditer(r"\bid=\"(?P<id>[^\"]+)\"[^>]*\bvalue=\"(?P<val>[^\"]*)\"", text, re.IGNORECASE):
        sid = m.group("id").strip()
        val = _to_float_any(m.group("val"))
        if sid and val is not None:
            values[sid] = val

    if values:
        return values

    for m in re.finditer(
        r"<id>(?P<id>[^<]+)</id>.*?<value>(?P<val>[^<]*)</value>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        sid = m.group("id").strip()
        val = _to_float_any(m.group("val"))
        if sid and val is not None:
            values[sid] = val

    return values


def _get_video_controller_names() -> list[str]:
    return [c["name"] for c in _get_video_controllers()]


def _get_video_controllers() -> list[dict]:
    """返回 Win32_VideoController 列表（尽量只取有意义字段）。

    每项：{name, pnp}
    """
    if not _is_windows():
        return []

    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$x = Get-CimInstance Win32_VideoController | "
        "Select-Object Name,PNPDeviceID,AdapterCompatibility,VideoProcessor | ConvertTo-Json -Compress;"
        "if ($null -eq $x) { '' } else { $x }"
    )
    code, out, _ = _run_powershell(script, timeout=2.5)
    if code != 0 or not out:
        return []

    try:
        parsed = json.loads(out)
    except Exception:
        return []

    items = parsed if isinstance(parsed, list) else [parsed]
    controllers: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        pnp = str(item.get("PNPDeviceID") or "").strip()
        comp = str(item.get("AdapterCompatibility") or "").strip()
        proc = str(item.get("VideoProcessor") or "").strip()
        if not name:
            continue
        low = name.lower()
        if "microsoft basic display" in low:
            continue
        key = (name + "|" + pnp).lower()
        if key in seen:
            continue
        seen.add(key)
        controllers.append({"name": name, "pnp": pnp, "comp": comp, "proc": proc})
    return controllers


def _is_virtual_controller(controller: dict) -> bool:
    name = str(controller.get("name") or "").strip().lower()
    pnp = str(controller.get("pnp") or "").strip().upper()
    comp = str(controller.get("comp") or "").strip().lower()
    proc = str(controller.get("proc") or "").strip().lower()

    # 明显的虚拟/远程/镜像/USB 显示适配器关键词
    bad_words = [
        "virtual",
        "usb",
        "mobile monitor",
        "displaylink",
        "mirror",
        "mirage",
        "remote",
        "rdp",
        "citrix",
        "xen",
        "hyper-v",
        "vmware",
        "virtualbox",
        "parallels",
    ]
    if any(w in name for w in bad_words):
        return True
    if any(w in comp for w in ("displaylink", "citrix", "vmware", "virtualbox", "parallels")):
        return True
    if "displaylink" in proc:
        return True

    # PNPDeviceID 明显不是物理显卡
    if pnp.startswith("USB\\") or pnp.startswith("ROOT\\") or pnp.startswith("SWD\\"):
        return True
    if "DISPLAYLINK" in pnp:
        return True

    return False


def _pci_gpu_names() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for c in _get_video_controllers():
        if _is_virtual_controller(c):
            continue
        pnp = str(c.get("pnp") or "")
        if not pnp.upper().startswith("PCI\\"):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        names.append(name)
    return names


def _non_virtual_controller_names() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for c in _non_virtual_controllers():
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        names.append(name)
    return names


def _looks_like_virtual_gpu_name(name: str) -> bool:
    low = (name or "").strip().lower()
    if not low:
        return True
    bad = [
        "usb mobile monitor",
        "virtual display",
        "displaylink",
        "virtual",
        "remote",
        "mirror",
    ]
    return any(w in low for w in bad)


def _controller_vendor(pnp: str, name: str) -> str:
    pnp_u = (pnp or "").upper()
    name_l = (name or "").lower()
    if "VEN_10DE" in pnp_u or "nvidia" in name_l:
        return "nvidia"
    if "VEN_1002" in pnp_u or "advanced micro devices" in name_l or "amd" in name_l or "ati" in name_l:
        return "amd"
    if "VEN_8086" in pnp_u or "intel" in name_l:
        return "intel"
    return "unknown"


def _non_virtual_controllers() -> list[dict]:
    return [c for c in _get_video_controllers() if not _is_virtual_controller(c)]


def _non_nvidia_controller_names() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for c in _non_virtual_controllers():
        name = str(c.get("name") or "").strip()
        pnp = str(c.get("pnp") or "")
        if not name:
            continue
        if _controller_vendor(pnp, name) == "nvidia":
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        names.append(name)
    return names


def _merge_gpus_by_name(gpus: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for g in gpus:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "GPU").strip() or "GPU"
        key = name.lower()
        if key not in merged:
            merged[key] = {
                "name": name,
                "utilization_percent": None,
                "temperature_c": None,
                "memory_used_mib": None,
                "memory_total_mib": None,
            }
            order.append(key)

        u = _to_float_any(g.get("utilization_percent"))
        t = _to_float_any(g.get("temperature_c"))
        mu = _to_float_any(g.get("memory_used_mib"))
        mt = _to_float_any(g.get("memory_total_mib"))

        cur_u = _to_float_any(merged[key].get("utilization_percent"))
        cur_t = _to_float_any(merged[key].get("temperature_c"))
        cur_mu = _to_float_any(merged[key].get("memory_used_mib"))
        cur_mt = _to_float_any(merged[key].get("memory_total_mib"))

        if u is not None:
            merged[key]["utilization_percent"] = u if cur_u is None else max(cur_u, u)
        if t is not None:
            merged[key]["temperature_c"] = t if cur_t is None else max(cur_t, t)
        if mu is not None:
            merged[key]["memory_used_mib"] = int(mu) if cur_mu is None else int(max(cur_mu, mu))
        if mt is not None:
            merged[key]["memory_total_mib"] = int(mt) if cur_mt is None else int(max(cur_mt, mt))

    return [merged[k] for k in order]


def _aida_collect_values() -> dict[str, float]:
    # 共享内存（温度优先）
    shared_text = _aida_shared_memory_text()
    values = _aida_parse_values(shared_text)

    # Registry（占用等，需在 AIDA64 External Applications 勾选 Registry）
    reg = _aida_registry_values()
    for k, v in reg.items():
        fv = _to_float_any(v)
        if fv is not None:
            values.setdefault(k, fv)

    # WMI（占用等，需在 AIDA64 External Applications 勾选 WMI）
    need_ids = [_aida_cpu_util_id()]
    for i in range(1, 13):
        need_ids.append(_aida_gpu_util_id(i))
        for tid in _aida_gpu_temp_ids(i):
            need_ids.append(tid)
    for tid in _aida_cpu_temp_ids():
        need_ids.append(tid)

    missing = [sid for sid in need_ids if sid not in values]
    wmi = _aida_wmi_values(missing)
    for k, v in wmi.items():
        fv = _to_float_any(v)
        if fv is not None:
            values.setdefault(k, fv)

    return values


def _aida_collect_strings(ids: list[str]) -> dict[str, str]:
    """从 AIDA64 Registry/WMI 收集字符串类传感器值（例如 BUSTYP）。"""
    out: dict[str, str] = {}
    reg = _aida_registry_values()
    for sid in ids:
        if sid in reg and reg[sid] is not None:
            out[sid] = str(reg[sid])

    missing = [sid for sid in ids if sid not in out]
    if missing:
        wmi = _aida_wmi_values(missing)
        for sid, val in wmi.items():
            if val is None:
                continue
            out[sid] = str(val)

    return out


def _pick_first(values: dict[str, float], ids: list[str]) -> Optional[float]:
    for sid in ids:
        if sid in values:
            return values[sid]
    return None


def _parse_aida_bustyp(text: str) -> tuple[Optional[float], Optional[int]]:
    # 示例："PCI-E 3.0 x16 @ 3.0 x16"
    text = (text or "").strip().lower()
    if not text:
        return None, None
    m = re.search(r"pci-?e\s+(\d+(?:\.\d+)?)\s+x(\d+)", text)
    if not m:
        return None, None
    gen = _to_float_any(m.group(1))
    width = _to_int_any(m.group(2))
    return gen, width


def _nvidia_smi_query() -> list[dict]:
    """返回 nvidia-smi 查询结果（best effort）。

    每项：{name, utilization_percent, temperature_c, memory_used_mib, memory_total_mib}
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.5,
            creationflags=_get_creationflags(),
        )
    except Exception:
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    gpus: list[dict] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        name = parts[0]
        util = _to_float_any(parts[1])
        temp = _to_float_any(parts[2])
        mem_used = _to_float_any(parts[3])
        mem_total = _to_float_any(parts[4])
        gpus.append(
            {
                "name": name,
                "utilization_percent": util,
                "temperature_c": temp,
                "memory_used_mib": int(mem_used) if mem_used is not None else None,
                "memory_total_mib": int(mem_total) if mem_total is not None else None,
            }
        )
    return gpus


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

    provider = _provider()

    gpus: list[dict] = []

    if provider in {"auto", "aida64"}:
        aida_values = _aida_collect_values()

        bus_type_ids = [_aida_gpu_bus_type_id(i) for i in range(1, 13)]
        aida_str = _aida_collect_strings(bus_type_ids)

        cpu_percent = _pick_first(aida_values, [_aida_cpu_util_id()])
        cpu_temp_c = _pick_first(aida_values, _aida_cpu_temp_ids())

        non_nvidia_names = _non_nvidia_controller_names()
        smi = _nvidia_smi_query()

        # 先收集 AIDA64 的 GPU 条目（不直接绑定具体显卡名）
        aida_gpu_entries: list[dict] = []
        for i in range(1, 13):
            util = _pick_first(aida_values, [_aida_gpu_util_id(i)])
            temp = _pick_first(aida_values, _aida_gpu_temp_ids(i))
            used_ded = _pick_first(aida_values, [_aida_gpu_used_ded_mem_id(i)])
            if util is None and temp is None and used_ded is None:
                continue

            bustyp = str(aida_str.get(_aida_gpu_bus_type_id(i), "")).strip().lower()
            if bustyp and any(w in bustyp for w in ("usb", "virtual", "remote")):
                continue

            aida_gpu_entries.append(
                {
                    "index": i,
                    "util": util,
                    "temp": temp,
                    "used_ded": used_ded,
                }
            )

        matched_aida: set[int] = set()
        used_non_nvidia_name = 0

        # 1) NVIDIA：只要 nvidia-smi 有结果，就以 nvidia-smi 为准（占用/显存/温度）。
        #    温度可以尝试用 AIDA64 的热点温度覆盖（需要能找到对应 AIDA index）。
        if smi:
            for s in smi:
                mapped = None
                s_gen = _to_float_any(s.get("pcie_gen_max"))
                s_wid = _to_int_any(s.get("pcie_width_max"))

                # 优先用 BUSTYP 的 PCI-E gen/width 匹配 AIDA index
                if s_gen is not None and s_wid is not None:
                    for e in aida_gpu_entries:
                        idx = int(e["index"])
                        if idx in matched_aida:
                            continue
                        bustyp = str(aida_str.get(_aida_gpu_bus_type_id(idx), ""))
                        a_gen, a_wid = _parse_aida_bustyp(bustyp)
                        if a_gen == s_gen and a_wid == s_wid:
                            mapped = e
                            break

                # 次选：按显存已用匹配（更严格，避免 AMD/USB 误匹配）
                if mapped is None:
                    s_used = _to_int_any(s.get("memory_used_mib"))
                    if s_used is not None and s_used >= 1024:
                        best = None
                        best_diff = None
                        for e in aida_gpu_entries:
                            idx = int(e["index"])
                            if idx in matched_aida:
                                continue
                            a_used = _to_int_any(e.get("used_ded"))
                            if a_used is None:
                                continue
                            diff = abs(int(a_used) - int(s_used))
                            if best_diff is None or diff < best_diff:
                                best_diff = diff
                                best = e
                        if best is not None and best_diff is not None and best_diff <= 256:
                            mapped = best

                if mapped is not None:
                    matched_aida.add(int(mapped["index"]))

                hot_temp = None
                if mapped is not None:
                    hot_temp = _pick_first(aida_values, _aida_gpu_temp_ids(int(mapped["index"])))

                gpus.append(
                    {
                        "name": str(s.get("name") or "NVIDIA GPU"),
                        "utilization_percent": _to_float_any(s.get("utilization_percent")),
                        "temperature_c": hot_temp if hot_temp is not None else _to_float_any(s.get("temperature_c")),
                        "memory_used_mib": _to_int_any(s.get("memory_used_mib")),
                        "memory_total_mib": _to_int_any(s.get("memory_total_mib")),
                    }
                )

        # 2) 非 NVIDIA：把剩余 AIDA GPU 条目按顺序绑定到非 NVIDIA 控制器名称
        for e in aida_gpu_entries:
            idx = int(e["index"])
            if idx in matched_aida:
                continue
            name = (
                non_nvidia_names[used_non_nvidia_name]
                if used_non_nvidia_name < len(non_nvidia_names)
                else f"GPU{idx}"
            )
            used_non_nvidia_name += 1
            if _looks_like_virtual_gpu_name(name):
                continue
            gpus.append(
                {
                    "name": name,
                    "utilization_percent": e.get("util"),
                    "temperature_c": e.get("temp"),
                    "memory_used_mib": _to_int_any(e.get("used_ded")),
                    "memory_total_mib": None,
                }
            )

    if provider in {"auto", "hwinfo"} and (cpu_temp_c is None and not gpus):
        hw_cpu_temp, hw_gpu_temp, hw_gpu_util, hw_gpu_name = _extract_hwinfo_metrics()
        cpu_temp_c = cpu_temp_c if cpu_temp_c is not None else hw_cpu_temp
        if hw_gpu_temp is not None or hw_gpu_util is not None or hw_gpu_name:
            gpus = [
                {
                    "name": hw_gpu_name or "GPU",
                    "utilization_percent": hw_gpu_util,
                    "temperature_c": hw_gpu_temp,
                }
            ]

    if psutil:
        try:
            # 占用在“方案 2”里希望来自 AIDA64；这里仅在 AIDA64 未提供时回退。
            if cpu_percent is None:
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
        "gpus": gpus,
        "note": "AIDA64 模式：温度来自共享内存；占用需开启 Registry 或 WMI 输出。",
    }
