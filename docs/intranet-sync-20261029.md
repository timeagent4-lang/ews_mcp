# 内网同步：八项审查修复与详细日志归档

仓库：https://github.com/timeagent4-lang/ews_mcp
目标分支：codex/attachment-upload-20261026
本轮增量基于父提交 dfd858beb5afa710ce3710802f01d0b2c76f63ce。请使用发布此文件的固定提交及其相对父提交的差异，不使用可能继续移动的分支头代替已指定版本。

## 合并范围

1. 日历更新、参会响应、取消绑定预览事件 ID/ChangeKey；状态变化拒绝旧确认，更新使用 NeverOverwrite。
2. ErrorIrresolvableConflict 识别为确定拒绝，释放本次草稿占用；真实超时继续保留未知结果。
3. 内部上传和操作归属统一小写 LANID，保留 OA 查询语义。
4. 批量保留逐项 completed/failed/unknown 及计数，有未知项时整体 unknown；原操作查询/重确认不再次提交，禁止把未知项当成确定失败提示重发。
5. 重启保留 pending 预览及其原有效期；仅将 confirmed/executing 转 unknown。历史 unknown 不自动修复或清空。
6. 幂等重放预览与首次返回结构一致，业务条目仍在 preview.preview.items。
7. 空 MAIL_LOCAL_TIMEZONE 回退 Asia/Shanghai，示例填写默认值。
8. 旗标列表兼容增加 offset，返回 offset/limit/has_more/next_offset，使用 lookahead 判断末页。
9. 详细日志沿用 audit/errors 两类按日 JSONL；增加调用/操作阶段、完整允许参数和结果、安全异常信息及日志查询工具。正文、凭据和附件原始载荷不记录，不启用原始 HTTP/SOAP DEBUG。
10. 保留最近30个自然日原日志，更早日志 gzip 长期归档，不自动删除归档；提供按日期、operation_id/call_id 查询原日志和归档的方式。

第9项审查问题“任务截止日早于开始日”继续暂缓，task_operations.py 未纳入本轮差异。没有新增依赖或数据库表结构；不改变已有发送权限、OA 有效邮箱校验、附件范围、幂等或未知结果隔离原则。

## 获取、保护与合并

- 先记录内网 HEAD、分支、工作区修改及部署镜像版本；备份本地定制，不覆盖真实 .env、凭据、SQLite/上传卷、日志及归档。
- 已有父提交可 cherry-pick 本轮提交；版本不同则逐项三方合并。部署配置仅按差异迁移，保留实际端口、网络、挂载、镜像和构建源。
- Git 不可达时可通过 Firecrawl 读取固定提交的本说明、docs/intranet-sync-20261029-manifest.json、提交差异及 raw 源码；必须取得完整文件，不以截图、Markdown 摘要或截断内容替代源码。manifest 中的 SHA-256 对应 UTF-8/LF 固定版本文件；合并内网定制后内容可以不同，但应逐项解释差异。
- 不清库、不批量改历史 unknown、不自动释放历史草稿占用、不自动重发邮件。历史记录如需恢复，应单独核实实际结果并受控处理。

## 测试与部署

- 在隔离临时目录运行测试，避免继承生产 .env；普通 MCP 测试不启动真实附件存储或外部服务，附件专项使用独立测试存储。运行全套 unittest、import smoke 和依赖检查。
- 重点验证：事件变化拒绝旧确认；明确冲突释放占用而超时仍隔离；大小写 LANID 上传/附加；批量未知结果及回执重放；重启 pending/TTL；预览结构；空时区导入；旗标101条分页；日志脱敏/归档/查询及后台结果关联。
- 源端离线验证记录见 docs/release-validation.md。Windows 上跳过的两项 POSIX 权限检查须在 Linux 部署环境执行，不能视为已通过。
- 部署前按 docs/logging.md 确认实际日志宿主机绝对路径、服务 UID、共享读取组、setgid/ACL 和历史文件权限。Compose 使用 create_host_path=false，目录须预先准备。继续使用现有非 root 服务身份，不为读日志改业务卷属主或开放所有人读写。
- 按既有部署流程备份持久数据并重建/重启应用镜像；不得仅复制源码却沿用旧镜像。保留持久卷和历史日志，核对实际容器挂载。普通只读验收不执行真实发送；涉及写入的验收需获准测试对象和明确授权。
- 日志验收可用 python -m utils.log_query --info，以及 --date/--operation-id/--call-id；确认宿主机有落盘文件、重启追加、旧日志可归档查询，正文/密钥不出现。

请反馈：合并提交和文件清单、冲突处理、实际测试数量/跳过原因、镜像和部署结果、日志路径与读取权限验证、尚未完成的真实环境验收。
