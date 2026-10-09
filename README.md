# EWS MCP integration snapshot

本仓库保存当前可用源码及附件下载、工具合同、回执与日历限制等修复，用于合入内网完整项目。
首次提交是当前工作区快照，不是完整原项目，也不是可直接独立部署的镜像。

**仅能使用网页爬虫获取代码：** 从 [GitHub 爬虫交付入口](docs/github-crawler-handoff.md) 开始，按固定版本的 GitHub／Raw 文本链接逐文件获取，无需 Git 或 ZIP 下载能力。

开始接入请阅读 [内网 AI 接入说明](docs/intranet-integration.md)，其中包含可直接转交的提示词、文件清单、部署配置、验收及回滚要求。

- 当前注册 28 个工具；删除未注册的本地镜像跟进工具后，保留其他禁用工具及会议通知副本行为。
- 附件通过 `prepare_attachment_download` 生成内网 HTTP 签名链接，AI 执行器 GET 下载文件；字节不进入工具响应。
- 配置统一使用 `lanid`／`OUTLOOK_ADMIN_LANID`。
- 当前员工版以 2026-09-30 提交 `c4a2bd7` 为基线，已按用户提供的内网源码照片恢复忙闲、自动回复、任务等模块；OA、审计和联系人模块也已在仓库内。照片无法证明恢复内容与该提交逐字一致，来源边界及当时验证结果见 [源码恢复记录](docs/source-restoration.md)。
- 本地邮件镜像与 `waiting_on` 已删除，不再使用 `EWS_MCP_CACHE_ENABLED`，服务状态也不再返回镜像覆盖或该开关。确认回执库、附件下载索引及附件缓存保留，`EWS_MCP_DATA_DIR` 仍需配置。
- 源码恢复时的离线测试仍有既存失败，历史通过数量不能视为当前版本全部通过或生产联调完成；每次接入须执行下方验证命令并记录实际结果。
- 照片、凭据、运行数据库及导出附件不入库。

验证命令（使用目标环境 Python）：

```sh
python -B -m unittest discover -s tests -v
```

更多依据见 [附件下载](docs/attachment-downloads.md)、[工具合同验证](docs/tool-contract-validation.md)、[描述核对](docs/tool-description-review.md)。
