# WinSysInfo（AstrBot 插件）

一个用于 `/info` 回复当前系统状态的 AstrBot Star 插件（处理器/内存/显卡占用与温度）。

## 指令

- `/info`：查看系统状态（温度/显卡数据为“尽力而为”）

## 配置

本插件通过 `_conf_schema.json` 在 AstrBot WebUI 中提供开关：

- `output_mode`：`文字` / `图片`
- `title_text`：标题文字
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

## 本地开发

将本目录放入 `AstrBot/data/plugins/<任意目录名>`，然后在 AstrBot WebUI 中启用/重载插件。

## 说明

- 处理器/显卡温度与显卡占用在 Windows 下为“尽力而为”，可能显示为“暂无”。
- NVIDIA 显卡会优先通过 `nvidia-smi` 获取占用/温度/显存。
- 可选：如果你运行了 LibreHardwareMonitor 或 OpenHardwareMonitor 并启用了 WMI，本插件也会尝试通过 PowerShell 从 `root\LibreHardwareMonitor` / `root\OpenHardwareMonitor` 读取传感器数据。
