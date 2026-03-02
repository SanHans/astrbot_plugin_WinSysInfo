# WinSysInfo（AstrBot 插件）

一个用于 `/info` 回复本机或远程主机系统状态的 AstrBot Star 插件（处理器/内存/显卡占用与温度）。

## 指令

- `/info`：按配置选择本机/远程
- `/info <别名>`：查看指定远程主机

## 配置

本插件通过 `_conf_schema.json` 在 AstrBot WebUI 中提供开关：

- `title_text`：标题文字
- `data_source`：数据来源（本机/远程）
- `remote_hosts`：远程主机列表
- `remote_default_alias`：默认远程别名
- `show_host`：显示主机名
- `show_os`：显示系统信息
- `show_cpu_name`：显示处理器名称
- `show_cpu_usage`：显示处理器占用
- `show_cpu_temp`：显示处理器温度
- `show_memory`：显示内存占用
- `show_gpu_name`：显示显卡名称
- `show_gpu_usage`：显示显卡占用
- `show_gpu_temp`：显示显卡温度
- `show_gpu_memory`：显示显存占用
- `show_timestamp`：显示时间

注意：已取消图片输出，仅发送文字。

## 远程主机

远程模式需要目标主机运行一个 HTTP Agent，并提供 `GET /status`。

仓库已内置远程 Agent 示例代码：`winsysinfo/remote_agent/`。

- 建议仅监听局域网 IP（例如 `--host 192.168.1.50 --port 8765`），并在防火墙中仅放行内网网段。
- 如果启用 Token，请求头使用：`Authorization: Bearer <token>`。

## 本地开发

将本目录放入 `AstrBot/data/plugins/<任意目录名>`，然后在 AstrBot WebUI 中启用/重载插件。

## 说明

- 处理器/显卡温度与显卡占用在 Windows 下为“尽力而为”，可能显示为“暂无”。
- NVIDIA 显卡会优先通过 `nvidia-smi` 获取占用/温度/显存。
- 可选：如果你运行了 LibreHardwareMonitor 或 OpenHardwareMonitor 并启用了 WMI，本插件也会尝试通过 PowerShell 从 `root\LibreHardwareMonitor` / `root\OpenHardwareMonitor` 读取传感器数据。
