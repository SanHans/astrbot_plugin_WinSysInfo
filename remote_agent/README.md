# WinSysInfo 远程 Agent

该 Agent 提供一个简单的 HTTP 接口（`/status`），供 WinSysInfo 插件在「远程模式」下拉取另一台主机的状态。

## 特性

- 支持绑定指定 IP 与端口（例如仅监听局域网网卡）
- 可选 Token 鉴权（请求头 `Authorization: Bearer <token>`）

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

方式一：PowerShell 脚本启动

```powershell
./run.ps1 -HostIp 192.168.1.50 -Port 8765 -Token "你的token"
```

方式二：手动启动

```bash
set WINSYSINFO_TOKEN=你的token
python -m uvicorn agent:app --host 192.168.1.50 --port 8765
```

## 接口

- `GET /health`：探活
- `GET /status`：返回状态 JSON

示例：

```bash
curl http://192.168.1.50:8765/status
curl -H "Authorization: Bearer 你的token" http://192.168.1.50:8765/status
```

## 安全建议

- 尽量只监听局域网 IP（不要用 `0.0.0.0` 直接暴露到公网）
- 在系统防火墙中仅放行内网网段
- 建议启用 Token
