# HTML 草稿调用说明

`create_draft` 可创建新邮件、回复、全部回复或转发草稿，不会发送邮件。所有工具的 `arguments` 都是平铺对象，每次必须提供 `lanid` 和与 OA 校验结果一致的中文姓名 `name`；不要再包一层 `params`。本文身份、邮箱和 ID 均为示例占位值。

## 正文与原邮件

| 调用方式 | `body_format="html"` 时的 `body` |
| --- | --- |
| `create_draft`，`mode="new"` | 新邮件的完整正文，可用完整 HTML 文档或 HTML 片段。不要传 `reply_to`。 |
| `create_draft`，`mode="reply"`、`"reply_all"`、`"forward"` | 仅本次新增的 HTML 片段，例如 `<p>`、`<div>`、`<table>`；禁止 `html`、`head`、`body` 标签或 `doctype` 文档声明。必须提供 `reply_to` 原邮件 ID。 |

回复和转发由 Exchange 根据 `reply_to` 引用原邮件。只提交新增内容，不要把原文、清洗后的纯文本原文或自行重拼的引文放回 `body`。`reply_to` 应是服务返回的原邮件 ID，不是随后创建的草稿 ID。`body` 可为 `""`，省略时也使用空字符串。

默认 `body_format="text"`，原有纯文本调用行为不变；写 HTML 时应显式传 `"html"`。正文上限为 **65,536 个字符**，标签及样式内容也计入。

内联 CSS（`style="..."`）是邮件客户端兼容性建议，不是 CSS 白名单或所有样式均能显示的保证。当前没有图片上传接口，写入 `cid:` 不会自动创建附件，也不承诺新增片段可以复用原邮件中的 CID；原邮件的内嵌图片是否保留须由内网 Exchange 和 Outlook 验收。

## 新建 HTML 草稿：预览

以下是 MCP `tools/call` 的参数对象；替换身份及收件人后调用。

```json
{
  "name": "create_draft",
  "arguments": {
    "lanid": "example_lanid",
    "name": "示例姓名",
    "mode": "new",
    "subject": "项目进度",
    "body": "<!doctype html><html><body><p style=\"color:#1f4e79\">本周进度已更新。</p><table style=\"border-collapse:collapse\"><tr><td style=\"border:1px solid #999;padding:6px\">开发完成</td></tr></table></body></html>",
    "body_format": "html",
    "to_emails": "recipient@example.com"
  }
}
```

新建正文也可直接用 `<p>本周进度已更新。</p>` 这样的片段。

## HTML 回复草稿：预览

```json
{
  "name": "create_draft",
  "arguments": {
    "lanid": "example_lanid",
    "name": "示例姓名",
    "mode": "reply",
    "reply_to": "原邮件ID",
    "body": "<p style=\"color:#1f4e79\">已收到，请查看以下进度。</p><table style=\"border-collapse:collapse\"><tr><td style=\"border:1px solid #999;padding:6px\">开发完成</td></tr></table>",
    "body_format": "html"
  }
}
```

`reply_all` 使用同样的片段规则。`forward` 也相同，按需要同时提供 `to_emails` 等收件人字段。回复未指定主题、收件人时沿用现有推导规则。

## 确认及回执

以上调用先返回预览。核对后，复制预览请求的整个 `arguments`，保持原业务参数完全相同（包括 HTML、收件人和可选 `idempotency_key`），仅增加响应中的 `operation_id` 和 `confirm_token`，再次调用同一工具。不要提交预览结果作为正文，不要额外传 `confirm`、`confirmation_id` 或 `params`。

例如确认上面的回复草稿：

```json
{
  "name": "create_draft",
  "arguments": {
    "lanid": "example_lanid",
    "name": "示例姓名",
    "mode": "reply",
    "reply_to": "原邮件ID",
    "body": "<p style=\"color:#1f4e79\">已收到，请查看以下进度。</p><table style=\"border-collapse:collapse\"><tr><td style=\"border:1px solid #999;padding:6px\">开发完成</td></tr></table>",
    "body_format": "html",
    "operation_id": "预览返回的operation_id",
    "confirm_token": "预览返回的confirm_token"
  }
}
```

新建草稿或其他写工具也使用同样的确认方法。改正文或其他业务参数时重新预览。结果未知时，用同一工具仅传 `lanid`、`name`、原 `operation_id` 查询回执；不要盲目重新创建或重复发送。创建草稿与发送仍是不同操作，沿用已有发送开关和确认机制。

## 已有草稿的 HTML 前置规则

`update_draft` 的现有限制不变：`body_action="prepend"` 加 `body_format="html"` 只接受**非空 HTML 片段**，目标草稿必须已经是 HTML；片段中的 `cid:` 必须匹配草稿现有附件的 ContentId。它在服务端读取的既有正文前插入片段，无需调用方重传原文。`body_action="replace"` 则替换整个正文。只改主题或收件人时省略 `body`、`body_format` 和 `body_action`。

## 内网验收

在获准测试邮箱验收新建、回复、全部回复和转发草稿：确认表格及内联样式显示、新增片段只出现一次、Exchange 引用原文无重复；覆盖 HTML 原邮件、纯文本原邮件、空新增正文及原邮件内嵌图片。验证回复/转发拒绝完整文档、65,536 字符边界、默认 text 行为和预览/确认参数一致性；同时回归 `update_draft` 的 HTML 前置及 CID 限制。仅离线构造成功或 XML 正确，不能证明 Outlook 的最终显示效果。

微软的 [NewBodyContent 文档](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/newbodycontent)说明：该元素用于回复、全部回复和转发的新增正文，`BodyType="HTML"` 会将组合正文转换为 HTML。以上片段限制、参数上限和两阶段确认属于本项目调用协议；最终组合及显示效果须按实际 Exchange/Outlook 版本验收。本次不改邮件读取、批量已读或发送确认机制。
