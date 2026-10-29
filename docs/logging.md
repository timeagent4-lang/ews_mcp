# 日志保存、查询与长期归档

日志使用schema_version=2 JSONL，一条逻辑事件一行。文件名按服务本地日期（默认Asia/Shanghai）划分；事件timestamp使用带时区的UTC时间。重启追加当天文件，不覆盖历史。

- audit-YYYY-MM-DD.log：工具开始、真实阶段、返回、后台分发/操作结果，以及启停、目录、归档和写入健康事件。
- errors-YYYY-MM-DD.log：实际失败及安全异常链，使用与审计相同event_id。
- docker logs：简短起止/结果摘要和告警。完整允许输入、结果和异常元数据以文件为准。

参数与结构化结果保留允许的完整字符串、列表和业务字段，不沿用旧512字符/100项/16KiB截断。主题、身份/邮箱、收发件人、文件名/路径、ID、覆盖率及批次结果可记录。正文、正文预览/引用/自动回复、会议邀请/回复正文和认证凭据统一脱敏；附件原始字节/Base64/提取原文只保留元数据和引用。JSON文本镜像、未知外部错误及第三方日志同样经过安全出口，不开启原始HTTP/SOAP DEBUG。

## 调用与业务结果如何判断

每条事件有独立event_id，一次调用的事件共享call_id，预览/确认/回执/后台执行关联operation_id。调用者声称身份和经过OA校验的身份分别记录。

- tool.started：收到调用，尚未表示校验或执行成功。
- tool.stage：已经发生的校验、连接、确认或执行阶段。
- tool.finished：本次调用正常返回；call_status=returned仍可能伴随业务failed/partial/unknown。
- tool.failed/tool.cancelled：调用异常或取消。
- dispatch.finished：后台任务返回，可能只是生成pending预览或验证拒绝。
- operation.preview_created：预览已创建。
- operation.finished：真实业务执行得到观察结果；unknown仍是未知，completed也不证明已核实收件人收到邮件。

send_draft返回processing时，原任务继续；后续后台事件保留origin_call_id。查询范围内只有开始记录时，只能说没有观察到结束，不能据此重发邮件或断定失败。业务操作库仍是持久化状态依据。

## 30天原文件与永久gzip归档

保留今天及前29个自然日的原文件。更早且已关闭的audit/errors文件进入archive/YYYY-MM/，例如：

```text
/logs/audit-2026-10-29.log
/logs/errors-2026-10-29.log
/logs/archive/2026-09/audit-2026-09-01.log.gz
```

归档长期保留，不自动删除。启动先打开当前日志，再由单个后台worker处理历史积压；跨日也触发维护，空闲时可能延迟。压缩临时文件完整关闭、校验并持久化发布后，才删除原文。已有归档相同可恢复中断清理，损坏或不同内容保留双方并告警。权限、空间、源文件变化或压缩失败均保留源文件，不吞当前事件或改变业务结果。

当前目录不能写时明确启动失败，不静默退回只有控制台。运行时落盘故障通过stderr独立报告；恢复时保留损坏半行并恢复行边界，查询对不完整JSONL返回完整性错误，不把缺口伪装成完整记录。压缩归档保存在同一挂载目录，不能代替异机备份。

## 持久化目录和非root身份

Compose将EWS_MCP_LOG_HOST_DIR（开发默认./logs）绑定到容器/logs，并设置EWS_MCP_LOG_DIR=/logs。生产建议设置宿主机固定绝对路径，避免换部署目录后相对路径指向别处。应用未设置LOG_DIR时沿用EWS_MCP_DATA_DIR/logs。

绑定目录必须先存在（create_host_path=false）。仓库配置不等于运行容器已采用该挂载；发布时验证实际挂载、目录和镜像重建后的历史保留。启动日志和查询--info会输出真实目录与数值UID/GID。

appuser只是镜像选用的非root身份，文件访问按数值UID/GID判断，不按用户名。保留应用现有身份和业务卷；不要为了查看日志把属主改成宿主机账号，或简单切换user后忽略已有数据库/缓存权限。

## 宿主机运维账号直接读取：共享读取组

采用“服务用户拥有文件、运维账号通过组读取”的部署策略：
- 专用日志目录及archive子目录使用服务实际UID作为属主、运维读取组作为属组，目录模式2750（setgid）。
- 新原文件/归档文件使用0640，继承父目录属组；不开放other读取，不给运维组删除/写入权限。
- 运维账号加入该组后重新登录。容器不需要改成root；共享日志组不改变业务数据卷权限。
- 默认组ACL保证严格umask下新子目录仍有组读取/进入权限。旧个人ACL例如u:aiuser:r--可能优先阻止目录进入，需要初始化时统一处理，不能只改chmod。

下面是给有权限的部署账号执行的一次性初始化模板，不是每次查看所需命令。仅用于已经确认的专用日志目录；将镜像、绝对目录、运维账号改成实际值。已有ACL先备份，再统一为共享组策略。应用不自动修改宿主机账号/组或清空ACL。

```sh
(
  set -eu
  log_image=ews-mcp:dev
  log_root=/srv/ews-mcp/logs
  log_reader_group=ews-log-readers
  log_reader_user=aiuser
  log_app_uid=$(docker run --rm --pull never --entrypoint id "$log_image" -u appuser)
  case "$log_app_uid" in ''|*[!0-9]*) exit 1;; esac
  test "$log_app_uid" -gt 0
  getent group "$log_reader_group" >/dev/null || groupadd --system "$log_reader_group"
  mkdir -p "$log_root"
  getfacl -R -p "$log_root" > ./ews-log-permissions.before.acl
  setfacl -R -b "$log_root"
  find "$log_root" -type d -exec setfacl -k {} +
  find "$log_root" -type d -exec chown "$log_app_uid:$log_reader_group" {} + -exec chmod 2750 {} +
  find "$log_root" -type f \( -name 'audit-*.log' -o -name 'errors-*.log' -o -name 'audit-*.log.gz' -o -name 'errors-*.log.gz' \) -exec chown "$log_app_uid:$log_reader_group" {} + -exec chmod 640 {} +
  find "$log_root" -type d -exec setfacl -m 'd:u::rwx,d:g::r-x,d:m::r-x,d:o::---' {} +
  usermod -aG "$log_reader_group" "$log_reader_user"
)
```

准备/修复已有文件权限时先停止服务，成功后再按正常部署流程启动。没有管理员或受批准部署权限时，普通账号无法自行更改其他UID属主/组成员；已授权Docker访问仍可通过容器读取。不要绕过组织权限策略。Windows离线测试不能替代Linux的setgid/ACL与共享组验收。

## 宿主机直接查询

以下以实际日志根目录为当前项目的./logs举例；生产替换为配置的绝对目录。读取账号已具备上述组权限后不需要sudo，也不依赖旧容器或旧镜像：

```sh
ls -lah ./logs
tail -n 200 ./logs/audit-2026-10-29.log
grep -nF '实际operation_id' ./logs/audit-2026-10-29.log
gzip -cd ./logs/archive/2026-09/audit-2026-09-01.log.gz | grep -nF '实际operation_id'
```

文件名按服务日期生成，不保证“今天”已经出现文件；服务跨午夜后，在首次对应写入时轮转。先列出文件或用info核对日期。查看普通原文件不会显示被脱敏的正文和凭据。

## 统一日期/ID查询：无需宿主机安装SDK

查询工具在服务镜像内运行。以下命令从宿主机执行，需要已有Docker权限；普通文件读取使用默认服务用户，不需要容器root：

```sh
docker compose exec -T ews-mcp python -m utils.log_query --info
docker compose exec -T ews-mcp python -m utils.log_query --date 2026-10-29 --kind audit
docker compose exec -T ews-mcp python -m utils.log_query --date 2026-09-01 --operation-id OP_ID --full
docker compose exec -T ews-mcp python -m utils.log_query --from 2026-10-28 --to 2026-10-29 --call-id CALL_ID --full
docker compose run --rm --no-deps -T --entrypoint python ews-mcp -m utils.log_query --info
```

停服使用run覆盖入口只做查询，不启动应用。必须使用与实际部署相同的Compose文件、env-file、项目和镜像。保留既有数据目录时，换镜像不删除日志或归档。

--kind为audit/errors/all，默认all；--status筛业务status；还可按event-id/tool筛选。默认返回最近200个逻辑事件摘要，--full输出完整允许数据，--tail控制条数。自动读取原文件/gzip，去重audit/errors镜像，原文与归档冲突时显示来源及诊断，不隐藏历史。

工具输出JSONL查询信息、事件及汇总；显示实际日期覆盖缺口，不从tail/筛选结果猜测业务完成状态。退出码0为成功（包括0匹配），2为参数错误，3为指定日期无文件，4为目录/权限/JSON完整性/gzip校验或冲突错误。旧JSON重新脱敏，旧自由文本仅输出可识别安全元数据，不改写历史文件、不伪造call_id。

跨日期范围的完整性检查会保存事件ID/内容的去重指纹，内存随唯一事件数量增长；完整结果只保留所选tail窗口，不把整段历史payload载入内存。

Compose控制台json-file仍单独限制为10MB/3份，不承担长期追溯职责。不要删除宿主机日志目录或归档来重建容器。单日志目录仅支持一个应用实例/进程，不能让多实例同时写入/归档。
