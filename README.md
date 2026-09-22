# EWS MCP integration snapshot

本仓库保存当前可用源码及附件下载、工具合同、回执与日历限制等修复，用于合入内网完整项目。
首次提交是当前工作区快照，不是完整原项目，也不是可直接独立部署的镜像。

**仅能使用网页爬虫获取代码：** 从 [GitHub 爬虫交付入口](docs/github-crawler-handoff.md) 开始，按固定版本的 GitHub／Raw 文本链接逐文件获取，无需 Git 或 ZIP 下载能力。

开始接入请阅读 [内网 AI 接入说明](docs/intranet-integration.md)，其中包含可直接转交的提示词、文件清单、部署配置、验收及回滚要求。

- 当前注册 28 个工具，保留既有禁用集合及会议通知副本行为。
- 附件通过 `prepare_attachment_download` 生成内网 HTTP 签名链接，AI 执行器 GET 下载文件；字节不进入工具响应。
- 配置统一使用 `lanid`／`OUTLOOK_ADMIN_LANID`。
- 本地最新验证：69 项离线测试通过。测试替代了缺失的 OA、审计和 Exchange 连接依赖，不等同于生产联调。
- 当前副本缺少 `utils/audit.py`、`utils/lanid_email.py`、`availability_operations.py`、`mirror.py`、`oof_operations.py`、`people_operations.py`、`task_operations.py`；这些模块须从完整项目保留或恢复，不得用测试桩补入生产。
- 照片、凭据、运行数据库及导出附件不入库。

验证命令（使用目标环境 Python）：

```sh
python -B -m unittest discover -s tests -v
```

更多依据见 [附件下载](docs/attachment-downloads.md)、[工具合同验证](docs/tool-contract-validation.md)、[描述核对](docs/tool-description-review.md)。
