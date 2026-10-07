# 原附件临时下载

`get_attachment` 提供清单、元数据和受限文本。`prepare_attachment_download` 检查 mailbox、邮件与附件归属，将原文件流式缓存，返回临时签名 URL、文件名、类型、大小、SHA-256、失效时间。HTTP GET 读取缓存，不再调用 Exchange。

参数采用顶层形式，个人凭据可选：

```json
{"mailbox":"user@example.com","message_id":"邮件ID","attachment_id":"附件ID"}
```

配置：

```env
EWS_MCP_DOWNLOAD_ENABLED=true
EWS_MCP_DATA_DIR=/data/aias_mcp
EWS_MCP_PUBLIC_BASE_URL=http://实际服务地址:7712
EWS_MCP_DOWNLOAD_SECRET=至少32字节的独立随机密钥
EWS_MCP_DOWNLOAD_TTL_SECONDS=600
EWS_MCP_DOWNLOAD_RETENTION_SECONDS=86400
EWS_MCP_DOWNLOAD_MAX_BYTES=5242880
EWS_MCP_DOWNLOAD_CACHE_MAX_BYTES=1073741824
EWS_MCP_DOWNLOAD_CACHE_MAX_FILES=1000
EWS_MCP_DOWNLOAD_CLEANUP_SECONDS=300
```

数据目录须为绝对路径并可写。文件存于 `exports`，`attachment_downloads.db` 仅保存下载元数据和配额；保留生命周期清理，不包含执行回执或密码。即使关闭新下载，存在缓存时仍运行定期清理。默认禁用时工具仍可发现，调用返回 `DOWNLOAD_DISABLED`。

`/downloads/{id}?expires=...&token=...` 是短期 bearer 链接，持有者可下载，GET 不再要求 MCP API key。有效期内可重新 GET，过期后重新准备；不支持 Range。AI 平台将文件下载到任务目录并核对 SHA-256。访问日志过滤签名令牌，响应为 no-store；只支持 FileAttachment，零字节文件也可下载。

容器启用持久卷 `/data/aias_mcp`；有反向代理前缀时将其包含在 PUBLIC_BASE_URL，转发下载路径。已有未索引导出文件不自动迁移或删除，配额仅计算新索引文件。
