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

## v0.1.1

- 新增：`CHANGELOG.md` 更新日志
- 新增：标题文字、主机名、系统信息、处理器名称、显卡名称开关
- 变更：取消图片发送功能，仅发送文字
- 修复：标题文字/处理器名称/显卡名称不显示的问题

## v0.1.0

- 初版：提供 `/info` 查看系统状态
