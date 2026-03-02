# WinSysInfo 远程 Agent

该 Agent 提供一个简单的 HTTP 接口（`/status`），供 WinSysInfo 插件在「远程模式」下拉取另一台主机的状态。

## 特性

- 支持绑定指定 IP 与端口（例如仅监听局域网网卡）
- 可选 Token 鉴权（请求头 `Authorization: Bearer <token>`）
- 可选：从 HWiNFO 传感器日志（CSV）读取 CPU/GPU 温度与显卡占用

## 安装

进入本目录后安装依赖：

```bash
pip install -r requirements.txt
```

如果你的 Windows 没有 `python` 命令，通常可以用：

```bash
py -m pip install -r requirements.txt
```

## 启动（绑定 IP/端口）

方式一：bat 脚本启动（推荐）

```bat
run.bat 192.168.1.50 8765 你的token
```

带 HWiNFO 日志路径（可选）：

```bat
run.bat 192.168.1.50 8765 你的token "C:\path\hwinfo.csv"
```

方式二：手动启动

```bash
set WINSYSINFO_TOKEN=你的token
python -m uvicorn agent:app --host 192.168.1.50 --port 8765
```

## 接口

- `GET /health`：探活
- `GET /status`：返回状态 JSON

## HWiNFO 传感器日志（推荐）

如果你希望拿到更可靠的 CPU/GPU 温度与显卡占用，建议用 HWiNFO 的「传感器日志」功能输出 CSV，然后让 Agent 读取。

大致步骤：

1) 打开 HWiNFO（传感器窗口）
2) 打开日志输出（CSV），设置一个固定的日志文件路径
3) 将该日志文件路径传给 Agent：
   - 通过 `run.bat` 的第 4 个参数，或
   - 设置环境变量 `WINSYSINFO_HWINFO_LOG`

Agent 会从 CSV 最后一行尝试解析：

- 处理器温度：优先匹配 `CPU Package`、`CPU (Tctl/Tdie)` 等列
- 显卡温度：优先匹配 `GPU Temperature`、`GPU Hot Spot` 等列
- 显卡占用：优先匹配 `GPU Core Load`、`GPU Utilization` 等列

如果你的列名不同，可以用环境变量自定义关键词（逗号分隔）：

- `WINSYSINFO_HWINFO_CPU_TEMP_KEYS`
- `WINSYSINFO_HWINFO_GPU_TEMP_KEYS`
- `WINSYSINFO_HWINFO_GPU_UTIL_KEYS`

示例：

```bash
curl http://192.168.1.50:8765/status
curl -H "Authorization: Bearer 你的token" http://192.168.1.50:8765/status
```

## 安全建议

- 尽量只监听局域网 IP（不要用 `0.0.0.0` 直接暴露到公网）
- 在系统防火墙中仅放行内网网段
- 建议启用 Token
