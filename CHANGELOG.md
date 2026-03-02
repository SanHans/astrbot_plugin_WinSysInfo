# 更新日志

## v0.2.0

- 新增：远程主机模式（HTTP Agent），支持 `/info <别名>`
- 新增：`data_source`、`remote_hosts`、`remote_default_alias` 配置项
- 变更：移除 `output_mode` 配置项（仅文字输出）

## v0.2.1

- 新增：远程 Agent 示例代码（`winsysinfo/remote_agent/`）

## v0.2.2

- 改进：Windows 下显卡兜底探测（WMI 显卡名称 + GPU Engine 占用）

## v0.2.3

- 新增：远程 Agent 支持读取 HWiNFO 传感器日志（CSV），用于 CPU/GPU 温度与占用

## v0.3.0

- 新增：远程 Agent 支持 AIDA64 共享内存读取温度
- 新增：远程 Agent 支持从 AIDA64 Registry/WMI 读取 CPU/GPU 占用（方案 2）

## v0.3.1

- 修复：过滤 USB/虚拟显示适配器导致的显卡误识别

## v0.3.2

- 修复：远程 AIDA64 模式下更严格过滤虚拟/USB 显示适配器（含 BUSTYP 过滤）

## v0.3.3

- 修复：远程 AIDA64 显卡名称映射优先使用 PCI 设备，并过滤 USB/虚拟显示适配器

## v0.3.4

- 修复：AIDA64 Registry/WMI 的 Value.* 键名解析，恢复 GPU 占用/显存已用读取
- 改进：NVIDIA 显卡用 nvidia-smi 补齐显存总量，并按显存已用匹配显卡名称

## v0.3.5

- 修复：合并显卡条目时保留显存/占用字段，避免 nvidia-smi 补齐信息丢失

## v0.1.1

- 新增：`CHANGELOG.md` 更新日志
- 新增：标题文字、主机名、系统信息、处理器名称、显卡名称开关
- 变更：取消图片发送功能，仅发送文字
- 修复：标题文字/处理器名称/显卡名称不显示的问题

## v0.1.0

- 初版：提供 `/info` 查看系统状态
