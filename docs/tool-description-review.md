# 工具描述逐项复核（2026-09-22）

本轮按最新工作区核对描述、公开参数、方法签名和实际分支。会议通知副本按用户决定保持现状；本轮源码仅修改 tool_specs.py 中的描述和注释，不修改发送、保存副本、确认流程或业务方法。

## 范围与结果

当前实际注册 28 个工具，21 个存在实现，7 个缺少实现。已用脚本双向比较这 21 个工具的业务参数名与公开参数，并逐个比较公开默认值，全部一致。
确认流程的 confirm/confirmation_id 属于内部参数；旧附件方法的 save 按约定不公开。这些不属于漏项。

| 工具 | 核对结果 |
| --- | --- |
| list_folders | 六个固定文件夹逐个探测，成功结果包含数量和覆盖情况；并非任意文件夹遍历。 |
| find_message | query 为主题包含；AQS 排他；folder 默认 inbox；补充不同文件夹使用的时间字段、日期边界、正文最多 10000 字符和会议通知查询边界。 |
| get_message | 不存在 compact/full；clean_body 默认 false；正文通过 body_offset/max_body_chars 分段读取。 |
| get_thread | 仅员工 Inbox/Sent；正文最多 10000 字符；修正覆盖情况描述，不再笼统承诺任意文件夹查询失败都返回 partial。 |
| get_attachment | 明确 5 MiB 和 20000 字符仅限制文本读取；元数据查询不受该文本大小限制；auto/text 对不支持的格式返回元数据和说明。 |
| prepare_attachment_download | 文件附件授权后导出并生成限时 HTTP 链接；不返回字节或服务器路径；保留实现及描述。 |
| get_mailbox_overview | 仅 Inbox 统计和最近未读，不承诺日历。 |
| create_draft | new/reply/reply_all/forward，原邮件引用约束和默认值一致。 |
| update_draft | 会更新传入字段，并在必要时校正 author；删除“checked change key protects concurrent edits”的过度承诺，明确确认时重新读取草稿，不锁定预览至执行期间的状态。 |
| send_draft | 现有草稿发送并显式保存到员工 Sent；发送开关和回执保持不变。 |
| set_message_flag | 修正 due_date：flagged 不传时清空；complete 传入时更新、不传时保留；clear 清除日期。 |
| list_flagged_messages | all 为 flagged/complete 的并集，不包含未标记项目；不再承诺本方法逐项检查普通 Message 类型。 |
| update_messages | 普通邮件逐项读状态/分类更新；批量 1–50 个非空且唯一 ID；未提供更新字段时可返回未更新。 |
| move_messages | 明确仅限员工 Inbox/Sent 间移动非草稿普通邮件，使用返回的新 ID。 |
| list_events | 展开指定时间窗的日历实例；明确 has_more 只是“本页已满”的提示，后续页仍可能为空。 |
| get_event | 返回组织者、参会人、响应和周期信息，与实现一致。 |
| create_event | 邀请默认关闭；开启需发送开关；补充员工 Sent 中未见通知不代表发送失败。 |
| update_event | 组织者、明确单次/实例限制一致；补充 notify_attendees 需要发送开关；保留通知副本现状。 |
| respond_to_event | 必填响应枚举一致；补充实际会发送响应且受发送开关约束。 |
| cancel_event | 精确区分会议取消通知与“非会议且无参会人”预约移至已删除项目；不传说明不等于不发通知。 |
| get_server_status | 明确实际返回当前邮箱、account 是否存在、send/cache 开关、已有镜像覆盖及数据目录末级名称，避免泛称运行计数器。 |

缺少实现，不能确认全部描述的 7 个注册工具：check_availability、find_people、get_contact、create_contact、list_tasks、create_task、update_task。本轮保留其现有声明，不编造模块。
其中 update_task 的“checked change key”及“不传字段不写入”等声明仍需取得 task_operations.py 后核实。

禁用集合保持不变。修正注释中“禁用工具均已完整实现、移除禁用项即可使用”的绝对表述；恢复注册仍需对应实现和部署权限。

## 验证方式

- 对比本轮修改前后的全部 SPECS，递归剔除 description 后完全相等：注册集合、参数类型、默认值、必填性、枚举、上下限和条件分支均未变。
- 21 个工具的参数名／默认值双向核对通过。
- 离线调用真实 `_flag_changes`，确认 complete 支持指定截止时间，省略时不修改截止时间。
- 现有离线测试继续运行，不增加只断言文案字面值的测试。
- 无真实 OA、Exchange 网络请求，无邮件发送或日历写入。

并发描述依据：本地锁定版 exchangelib 5.6.0 的 Item.save 默认使用 AutoResolve；update_draft 确认时重新读取草稿，没有将预览的 changekey 作为确认前置条件。Microsoft 的 [UpdateItem 文档](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/updateitem-operation)也展示了 ChangeKey 与 ConflictResolution，不能仅凭请求携带 ChangeKey 就宣称预览后的并发编辑一定被拒绝。

## 读取问题修复跟进

以下问题在文案复核时发现，随后按用户要求实施修复：

1. find_message：读取 limit+1 行后，先检查全部行中的异常，再处理当前页。异常保持原类型进入现有错误封装，不再被忽略或变成 AttributeError；正常分页保持不变。
2. get_thread：仅当 Inbox/Sent 均不可访问时报告 THREAD_FOLDERS_UNAVAILABLE。成功查询没有匹配时返回空 items 和 next_offset=null；一个目录不可访问、另一目录成功但为空时，返回空 items、原 coverage 和 partial=true。

修复仅涉及 mail_operations.py，增加 5 行搜索检查（含注释）并替换 1 处会话判断条件。新增 7 个读取回归测试和 2 个实际 MCP 入口测试，全部 63 个离线测试通过。未发送真实邮件或写入日历。

目录解析后发生的会话查询异常仍沿用原行为：整次调用失败，不转成空结果或 partial；此项未扩展。会议通知副本位置不属于待处理问题；缺失模块和真实部署联调仍是验证边界。
