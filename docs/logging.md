# 日志保存与排障

服务启动时配置文件日志，分别保存本地时区下的 `errors-YYYY-MM-DD.log` 和 `audit-YYYY-MM-DD.log`。Dockerfile 默认时区为 Asia/Shanghai；审计 JSON 内的 timestamp 延续使用 UTC。写入采用追加模式，重启不会覆盖当天已有记录。

- 错误文件：应用/依赖实际记录的 ERROR 及以上日志、程序异常堆栈，以及审计状态为 failure/rejected/error 的完整脱敏记录。普通业务错误补充底层 Exchange 错误码和脱敏摘要，不启用完整 SOAP/XML 调试。
- 审计文件：保留全部脱敏审计。未完成操作按实际阶段记录 pending、confirmed、executing 或 processing；来源覆盖不完整的读取记录为 incomplete，不将它们误记为成功或失败。审计失败同时进入错误文件，方便单文件排查；控制台只显示简短失败摘要和关联 event_id。
- 控制台：保留启动、错误和有意义的警告；隐藏 MCP 请求类型、Exchange 连接成功、已推断时区等重复 INFO。未验证 HTTPS 的警告按目标主机每进程保留首次；重启后会再提示，不改变证书校验行为。其他警告不一并关闭。

保留今天及前29个自然日的文件。启动及新日期首次写日志时，按文件名日期删除更早的 errors/audit 文件；不是按文件数量或mtime判断。服务停止期间不会执行清理，下次启动会补清理。其他名称、非法日期、目录和符号链接不参与删除。无按大小截断，因此单日大量错误仍可能生成较大的文件。

## 宿主机目录与权限

Compose 将宿主机 `${EWS_MCP_LOG_HOST_DIR:-./logs}` 绑定到容器 `/logs`，设置 `EWS_MCP_LOG_DIR=/logs`。相对路径以 Compose 项目目录为准；也可以在部署机 `.env` 中指定宿主机绝对目录。不要覆盖已有 `.env`。

镜像中的服务以 appuser 运行。绑定挂载会遮住镜像内的属主设置，部署前需要创建宿主机目录并赋予镜像 appuser 的实际 UID/GID；不要用 `chmod 777`。

在部署机、构建好待运行镜像后执行（将镜像名和目录改成实际值）：

```sh
docker run --rm --entrypoint id ews-mcp:dev appuser
sudo mkdir -p ./logs
# 根据上条输出替换下列 UID:GID，不假设必然是1000:1000。
sudo chown UID:GID ./logs
sudo chmod 750 ./logs
```

Compose 使用 `create_host_path: false`，目录未准备好时直接失败，不自动创建成 root 属主目录。应用初始化会实际打开两类日志文件；无写入/清理权限时启动失败，不悄悄退回控制台。新文件默认权限0640；需要确保部署账号有相应读取权限。

若不用仓库 Compose，须自行挂载宿主机目录并设置 `EWS_MCP_LOG_DIR`；未设置时写到 `EWS_MCP_DATA_DIR/logs`。仅有容器内目录不能代替宿主机绑定保存。单实例单进程写入，多个进程不要共用同一日志目录。

## 查看与验证

```sh
tail -n 200 ./logs/errors-$(date +%F).log
grep 'ErrorAccessDenied' ./logs/errors-*.log
docker logs --tail 100 ews-mcp-dev
```

这些命令只读取日志。依照错误摘要中的 event_id 可以定位审计记录；审计中保留脱敏后的原始参数结构和操作号。完整堆栈仅进错误文件，控制台保留摘要。不要把整份日志直接发布到公共仓库或工单。

Compose 另限制 Docker 自身控制台日志为10MB、最多3份，避免无限增长；它与上述按日期保留30天的业务文件相互独立。改变挂载/环境变量需要按部署流程重建容器，宿主机目录中的已有日期文件继续保留。

本地测试覆盖跨日、30天边界、重启追加、并发与重复初始化、实际MCP失败审计及降噪。真实宿主机目录权限、Compose版本兼容和容器重建后的保留，仍需在部署机实际验证。
