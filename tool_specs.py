"""Flat tool schemas with OA-scoped identities and persistent write confirmation."""

from __future__ import annotations


SIDE_READ = "read"
SIDE_WRITE = "write"

_BIG_TEXT = frozenset({"body", "message", "internal_reply", "external_reply", "query", "notes"})

_ID_FIELDS = frozenset(
    {
        "id",
        "ids",
        "message_id",
        "event_id",
        "draft_id",
        "task_id",
        "contact_id",
        "reply_to",
        "parent",
    }
)

# Accept folder aliases only; raw FolderIds could escape the mailbox scope.
_FOLDER_FIELDS = frozenset({"folder", "to_folder"})

# Only these schema fields receive specific codes; others stay INVALID_PARAMS.
_FIELD_ERROR_CODES = {}
for _code, _names in (
    ("INVALID_DATETIME", ("start", "end", "start_date", "due_date", "since", "until")),
    ("INVALID_PAGINATION", ("limit", "offset")),
    ("INVALID_FOLDER", tuple(sorted(_FOLDER_FIELDS))),
    ("INVALID_BODY_FORMAT", ("body_format",)),
    ("INVALID_BODY_ACTION", ("body_action",)),
):
    for _name in _names:
        _FIELD_ERROR_CODES[_name] = _code
del _code, _names, _name

_ID_HINT = (
    "Use mailbox-scoped returned/raw EWS IDs; no short aliases."
)

_FOLDER_HINT = (
    "Use a listed folder alias, never a raw folder ID."
)

_READ_TOOLS = frozenset(
    {
        "list_folders",
        "find_message",
        "get_message",
        "get_thread",
        "get_attachment",
        "prepare_attachment_download",
        "prepare_attachment_upload",
        "get_mailbox_overview",
        "list_events",
        "get_event",
        "list_tasks",
        "find_people",
        "get_contact",
        "check_availability",
        "get_oof_settings",
        "get_server_status",
        "list_flagged_messages",
    }
)


def _s(name, type_, description, *, required=False, default=None, enum=None,
       items=None, minimum=None, maximum=None, min_length=None, max_length=None, min_items=None, max_items=None):
    field = {
        "name": name,
        "type": type_,
        "description": description,
        "required": required,
    }
    if default is not None:
        field["default"] = default
    if enum is not None:
        field["enum"] = list(enum)
    if items is not None:
        field["items"] = items
    if maximum is not None:
        field["maximum"] = maximum
    if minimum is not None:
        field["minimum"] = minimum
    if min_length is not None:
        field["min_length"] = min_length
    if max_length is not None:
        field["max_length"] = max_length
    if max_items is not None:
        field["max_items"] = max_items
    if min_items is not None:
        field["min_items"] = min_items
    return field


TOOLS: dict[str, list[dict]] = {
    # --- mail read ---
    "list_folders": [],
    "find_message": [
        _s("query", "string", "Subject contains this text; combines with the other filters.", default=""),
        _s("folder", "string", "Mail folder to search.", default="inbox", enum=["inbox", "drafts", "sent"]),
        _s("since", "string", "Inclusive start: inbox received time, sent sent time, drafts created time. Missing timezone uses Asia/Shanghai.", default=None),
        _s("until", "string", "Exclusive end, using since's time field. A date means its starting midnight in Asia/Shanghai.", default=None),
        _s("is_unread", "boolean", "True unread only, False read only; omit for no filter.", default=None),
        _s("has_attachments", "boolean", "True/False attachment filter.", default=None),
        _s("limit", "integer", "Max results.", default=10, minimum=1, maximum=100),
        _s("offset", "integer", "Pagination offset.", default=0, minimum=0),
        _s("include_body", "boolean", "Include up to 10000 body characters, preserving HTML; get_message reads further.", default=True),
    ],
    "get_message": [
        _s("message_id", "string", "Scoped message ID.", required=True),
        _s("clean_body", "boolean", "Strip HTML to plain text.", default=False),
        _s("max_body_chars", "integer", "Body char cap.", default=10000, minimum=1, maximum=100000),
        _s("body_offset", "integer", "Body read offset.", default=0, minimum=0),
    ],
    "get_thread": [
        _s("message_id", "string", "Scoped message ID in the thread.", required=True),
        _s("offset", "integer", "Pagination offset.", default=0, minimum=0),
        _s("limit", "integer", "Page size.", default=20, minimum=1, maximum=100),
    ],
    "get_attachment": [
        _s("message_id", "string", "Scoped message ID owning the attachment.", required=True),
        _s("attachment_id", "string", "Scoped attachment ID; omit to list.", default=None),
        _s("mode", "string", "info: metadata only; auto/text: supported text or metadata with a notice. Without attachment_id, list metadata.", default="auto", enum=["auto", "info", "text"]),

    ],
    "prepare_attachment_download": [
        _s("message_id", "string", "Scoped message ID owning the attachment.", required=True),
        _s("attachment_id", "string", "Attachment ID returned for this message.", required=True),
    ],
    "prepare_attachment_upload": [
        _s("filename", "string", "File name; path components are removed. Upload bytes over HTTP, never MCP.", required=True, min_length=1),
        _s("size", "integer", "Exact byte length; zero allowed, server limit applies.", required=True, minimum=0),
        _s("sha256", "string", "File SHA-256, 64 hexadecimal characters.", required=True, min_length=64, max_length=64),
        _s("content_type", "string", "MIME type; inline images are decoded and verified separately.", default=None, min_length=1, max_length=255),
    ],
    "get_mailbox_overview": [
        _s("limit", "integer", "Recent-unread cap.", default=5, minimum=1, maximum=100),
    ],
    # --- draft / mail writes ---
    "create_draft": [
        _s("mode", "string", "reply/reply_all/forward require reply_to; new forbids it.", default="new", enum=["new", "reply", "reply_all", "forward"]),
        _s("subject", "string", "Subject; omitted for reply/forward uses RE:/FW: prefix.", default=""),
        _s("body", "string", "New content only for reply/reply_all/forward; Exchange quotes the original. With new, the complete body. Empty allowed.", default="", max_length=65536),
        _s("body_format", "string", "text preserves the original's body type. html: full body/fragment for new; fragment only for reply/reply_all/forward (no html/head/body/doctype). Confirm CID uploads first and use the returned content_id.", default="text", enum=["text", "html"]),
        _s("to_emails", "string", "To recipients, ; separated.", default=""),
        _s("cc_emails", "string", "Cc recipients, ; separated.", default=""),
        _s("bcc_emails", "string", "Bcc recipients, ; separated.", default=""),

        _s("reply_to", "string", "Original message ID for reply/forward.", default=None),
    ],
    "update_draft": [
        _s("draft_id", "string", "Scoped draft ID.", required=True),
        _s("subject", "string", "New subject; omit to leave unchanged.", default=None),
        _s("body", "string", "Omit to preserve the body; body_format/body_action require body. prepend+html accepts fragments only, without html/head/body/doctype.", default=None, max_length=65536),
        _s("body_format", "string", "text keeps the draft's body type. html replaces with verbatim HTML; prepend requires an HTML draft and a fragment whose CIDs already exist. Confirm new image attachments first.", default=None, enum=["text", "html"]),
        _s("body_action", "string", "replace overwrites the whole body, including quotes. prepend adds only new content before the server-read body, preserving quotes, styles and images.", default=None, enum=["replace", "prepend"]),
        _s("to_emails", "string", "New to recipients; omit to leave unchanged.", default=None),
        _s("cc_emails", "string", "New cc recipients; omit to leave unchanged.", default=None),
        _s("bcc_emails", "string", "New bcc recipients; omit to leave unchanged.", default=None),
    ],
    "delete_draft": [
        _s("draft_id", "string", "Scoped draft ID.", required=True),
    ],
    "send_draft": [
        _s("draft_id", "string", "Scoped draft ID to send.", required=True),
    ],
    "attach_draft_attachment": [
        _s("draft_id", "string", "Existing ordinary draft.", required=True),
        _s("upload_id", "string", "Temporary upload ID after successful HTTP PUT; not an EWS ID.", required=True, min_length=32, max_length=32),
        _s("is_inline", "boolean", "Verified single-frame PNG/JPEG CID image; use the receipt's content_id in HTML.", default=False),
    ],
    "set_message_flag": [
        _s("message_id", "string", "Scoped message ID to flag.", required=True),
        _s("flag", "string", "Follow-up state to apply.", required=True, enum=["flagged", "complete", "clear"]),
        _s("due_date", "string", "ISO date. flagged: omit to clear; complete: omit to preserve. clear forbids this field and removes all flag dates.", default=None),
    ],
    "list_flagged_messages": [
        _s("folder", "string", "Mail folder to list.", default="inbox", enum=["inbox", "drafts", "sent"]),
        _s("status", "string", "Flag state; all includes flagged and complete.", default="flagged", enum=["flagged", "complete", "all"]),
        _s("limit", "integer", "Max results.", default=20, minimum=1, maximum=100),
    ],
    "update_messages": [
        _s("ids", "array", "Scoped message IDs (1-50).", required=True, items="string", max_items=50),
        _s("set_read", "boolean", "True read, False unread; omit to leave unchanged.", default=None),
        _s("categories_add", "array", "Categories to add.", default=None, items="string", max_items=50),
        _s("categories_remove", "array", "Categories to remove.", default=None, items="string", max_items=50),
    ],
    "move_messages": [
        _s("ids", "array", "Scoped message IDs (1-50).", required=True, items="string", max_items=50),
        _s("to_folder", "string", "inbox or sent.", required=True, enum=["inbox", "sent"]),
    ],
    "delete_messages": [
        _s("ids", "array", "Scoped message IDs (1-50).", required=True, items="string", max_items=50),
    ],
    # --- calendar ---
    "list_events": [
        _s("start", "string", "Window start; missing timezone uses Asia/Shanghai.", required=True),
        _s("end", "string", "ISO end.", required=True),
        _s("offset", "integer", "Pagination offset.", default=0, minimum=0),
        _s("limit", "integer", "Page size.", default=20, minimum=1, maximum=100),
    ],
    "get_event": [
        _s("event_id", "string", "Scoped calendar event ID.", required=True),
    ],
    "create_event": [
        _s("subject", "string", "Event subject.", required=True),
        _s("body", "string", "Event body.", default="", max_length=65536),
        _s("start", "string", "Start (ISO or YYYY-MM-DD HH:mm).", required=True),
        _s("end", "string", "End; defaults to start+1h.", default=None),
        _s("location", "string", "Location.", default=None),
        _s("attendees", "array", "Attendee emails.", default=None, items="string", max_items=100),
        _s("send_invitations", "boolean", "Email attendees (requires send switch + confirm).", default=False),
    ],
    "update_event": [
        _s("event_id", "string", "Scoped calendar event ID.", required=True),
        _s("subject", "string", "New subject.", default=None),
        _s("body", "string", "New body.", default=None, max_length=65536),
        _s("start", "string", "New start.", default=None),
        _s("end", "string", "New end.", default=None),
        _s("location", "string", "New location; empty string clears.", default=None),
        _s("notify_attendees", "boolean", "Notify attendees (requires send switch + confirm).", default=False),
    ],
    "respond_to_event": [
        _s("event_id", "string", "Calendar event ID, not a meeting-request mail ID.", required=True),
        _s("response", "string", "Attendee response.", required=True, enum=["accept", "tentative", "decline"]),
        _s("message", "string", "Response message.", default=None),
    ],
    "cancel_event": [
        _s("event_id", "string", "Organizer-owned scoped event ID.", required=True),
        _s("message", "string", "Cancellation note; omission still sends meeting notices.", default=None),
    ],
    # --- availability ---
    "check_availability": [
        _s("start", "string", "Window start.", required=True),
        _s("end", "string", "Window end.", required=True),
        _s("attendees", "array", "Emails; trimmed, lowercased and deduplicated.", required=True, items="string", min_items=1, max_items=100),
        _s("interval_minutes", "integer", "Sampling interval in minutes, not meeting duration.", default=30, minimum=5, maximum=1440),
    ],
    # --- people / contacts ---
    "find_people": [
        _s("query", "string", "Search text.", required=True),
        _s("source", "string", "gal, contacts, or auto.", default="auto", enum=["auto", "gal", "contacts"]),
        _s("limit", "integer", "Max per source.", default=20, minimum=1, maximum=100),
        _s(
            "directory_id",
            "string",
            "Configured team directory only; requires explicit source=contacts. Omit for personal Contacts/GAL.",
            default=None,
            min_length=1,
        ),
    ],
    "get_contact": [
        _s("contact_id", "string", "Scoped Contacts item ID, or exact GAL email.", required=True),
        _s(
            "directory_id",
            "string",
            "Configured team directory; contact_id must be an item ID within it, never a GAL email.",
            default=None,
            min_length=1,
        ),
    ],
    "create_contact": [
        _s(
            "display_name",
            "string",
            "Contact's display name, separate from the caller's name.",
            required=True,
            min_length=1,
        ),
        _s("email", "string", "Optional contact email; validated when supplied, never selects the mailbox.", default=None),
        _s("phone", "string", "Business phone.", default=None),
        _s("company_name", "string", "Company name.", default=None),
        _s("job_title", "string", "Job title.", default=None),
        _s("department", "string", "Department.", default=None),
        _s("mobile_phone", "string", "Mobile phone.", default=None),
        _s("office", "string", "Office location.", default=None),
        _s("notes", "string", "Plain text notes.", default=None, max_length=65536),
        _s(
            "directory_id",
            "string",
            "Configured team directory requiring create permission; omit for personal Contacts.",
            default=None,
            min_length=1,
        ),
    ],
    # --- tasks ---
    "list_tasks": [
        _s("folder", "string", "Task folder alias.", default="tasks", enum=["tasks"]),
        _s("incomplete_only", "boolean", "Only incomplete tasks.", default=True),
        _s("limit", "integer", "Page size.", default=50, minimum=1, maximum=100),
        _s("offset", "integer", "Filtered-list offset; follow next_offset.", default=0, minimum=0),
    ],
    "create_task": [
        _s("subject", "string", "Task subject.", required=True),
        _s("body", "string", "Task body.", default=None, max_length=65536),
        _s("start_date", "string", "Start date (ISO).", default=None),
        _s("due_date", "string", "Due date (ISO).", default=None),
    ],
    "update_task": [
        _s("task_id", "string", "Scoped task ID.", required=True),
        _s("complete", "boolean", "Mark complete/incomplete.", default=None),
        _s("due_date", "string", "New due date.", default=None),
    ],
    # --- oof ---
    "get_oof_settings": [],
    "set_oof": [
        _s("enabled", "boolean", "Enable/disable OOF.", required=True),
        _s("internal_reply", "string", "Internal reply text.", default=None, max_length=65536),
        _s("external_reply", "string", "External reply text; non-empty enables audience All.", default=None, max_length=65536),
        _s("start", "string", "Scheduled start (optional).", default=None),
        _s("end", "string", "Scheduled end (optional).", default=None),
    ],
    # --- status ---
    "get_server_status": [],
}

DESCRIPTIONS = {
    "prepare_attachment_upload": "Authorize a local upload without Exchange. Returns upload_id, private upload_url/upload_token, expiry and limits. HTTP PUT exact bytes and Content-Length with Bearer upload_token; repeat PUT of a ready file returns 409. Then attach_draft_attachment. No Base64, URL fetching or server paths.",
    "attach_draft_attachment": "Attach one completed, owner-scoped upload to an ordinary draft; preserve existing content. One logical operation consumes it. Inline: verified single-frame PNG/JPEG; use the returned content_id, then get a fresh send preview. Reject stale draft/file versions. Unknown: query the original receipt even if the upload expired; never reattach automatically.",
    "list_folders": "Probe folder access and return counts plus coverage; inaccessible folders do not fail the probe.",
    "find_message": "Search mail by subject, dates, read state and attachments; item_class identifies meeting messages. Use get_message for meeting details. Missing meeting notices in Sent do not prove delivery failure.",
    "get_message": "Read a message, paginated body, attachments and item_class. MeetingRequest adds meeting times, organizer and associated_calendar_item_id; use a returned calendar ID with get_event/respond_to_event. An empty attachment list does not rule out a meeting. Preserve HTML unless clean_body=true; search again for stale IDs.",
    "get_thread": "Read Inbox/Sent conversation mail with source coverage; partial=true retains available results. Body cap: 10000 characters; get_message reads further. Excludes service-account Sent; missing meeting notices do not prove delivery failure.",
    "get_attachment": "List metadata or read supported text: 5 MiB input, 20000 output characters. Other types return metadata. For original bytes use prepare_attachment_download, then HTTP GET.",
    "prepare_attachment_download": "Return a private temporary download_url, filename, content_type, size, sha256 and expiry for a scoped file attachment. HTTP GET into the task workspace and verify SHA-256; never publish the URL. Retry until expiry, then prepare again. Requires download configuration; item attachments unsupported.",
    "get_mailbox_overview": "Read Inbox total/unread counts and recent unread mail.",
    "create_draft": "Create a new or native reply/forward draft without sending; use the original message ID. MeetingRequest supports reply/reply_all only, without changing RSVP; use respond_to_event with the calendar ID for RSVP. Other meeting objects remain unsupported.",
    "update_draft": "Update supplied fields; empty values clear supported fields. Align the draft author with the employee. Preview snapshots the version; confirmation rejects later changes and save detects conflicts.",
    "delete_draft": "Move a draft to DeletedItems; requires access and never falls back to permanent deletion.",
    "send_draft": "Send a draft and save in employee Sent; consumes its ID. No automatic resend; idempotency survives restart. Wait at most 30 seconds: processing with wait_timed_out=true returns '邮箱服务不稳定 请五分钟后查看结果' while the same task continues. After five minutes query the original receipt and verify actual Sent; processing does not prove submission/success. Preview timeout returns RESPONSE_WAIT_TIMEOUT without sending.",
    "update_messages": "Update read state/categories of ordinary mail. Meeting requests, responses and cancellations support read/unread only; omit category parameters. Does not RSVP or change calendar events. Report results per item.",
    "set_message_flag": "Set an ordinary message's follow-up flag; reject meeting requests/cancellations.",
    "list_flagged_messages": "List open follow-up flags by default; status=all includes completed flags, never unflagged mail.",
    "move_messages": "Move ordinary non-draft mail between Inbox and Sent; use the returned new ID afterward.",
    "delete_messages": "Move ordinary mail to DeletedItems; no soft/permanent deletion or automatic retries. Report failed/uncertain batch items.",
    "list_events": "List expanded calendar occurrences with pagination. has_more hints at a full page; the next may be empty.",
    "get_event": "Read an event's organizer, attendees, responses and recurrence; unavailable response times are null.",
    "create_event": "Create an event; invitations default off. Sending invitations requires the send switch.",
    "update_event": "Update an organizer-owned event/occurrence; reject recurring masters. notify_attendees=false is silent; empty location clears it.",
    "respond_to_event": "RSVP using a calendar event ID: accept, tentative or decline. Requires attendee membership and send switch; rejects organizer-owned events and recurring masters.",
    "cancel_event": "Cancel an organizer-owned event; reject recurring masters. Meetings send native cancellation notices and require the send switch, even without message. Only appointments with no attendees move to DeletedItems silently.",
    "check_availability": "Return per-attendee free/busy, tentative, OOF or unknown intervals; unusable data stays unknown. Does not choose or book a meeting.",
    "find_people": "Search GAL/personal Contacts independently with source coverage; partial=true retains available results. With directory_id, search only that authorized directory and require source=contacts.",
    "get_contact": "Read personal Contacts by ID or GAL by exact unique email. directory_id restricts reads to an authorized directory and verifies item ownership; returns directory identity.",
    "create_contact": "Create a personal or authorized team contact; email is optional. No GAL writes or mail. Preview/receipt identify the team target; confirmation rechecks it, requiring a fresh preview if changed.",
    "list_tasks": "Read live tasks, incomplete by default; filter before paging. Follow next_offset until null; use returned IDs for update_task.",
    "create_task": "Create a personal task without sending mail or invitations.",
    "update_task": "Update task completion/due date with change-key checking; no fields means no write.",
    "get_oof_settings": "Read mailbox out-of-office settings; folder delegation may not grant access.",
    "set_oof": "Set out-of-office replies. Omit external_reply to disable external replies; nonempty text enables audience All in preview. Internal text is never copied externally by default.",
    "get_server_status": "Return mailbox, account presence, send switch and data-directory basename; no credentials or other mailbox statistics.",
}

for _name in ("create_event", "update_event", "cancel_event"):
    DESCRIPTIONS[_name] += (
        " Meeting notices may be absent from employee Sent; absence does not justify resending."
    )

DESCRIPTIONS['send_draft'] += ' Preview snapshots body, recipients and attachments; observed changes require a fresh preview.'

# Deletion is disabled by policy; the service lacks mailbox-level OOF permission.
DISABLED_TOOLS = frozenset(
    {
        "delete_draft",
        "delete_messages",
        "get_oof_settings",
        "set_oof",
    }
)

TOOL_NAMES = [name for name in TOOLS if name not in DISABLED_TOOLS]
assert len(TOOL_NAMES) == 30, f"expected 30 registered tools, got {len(TOOL_NAMES)}"

READ_TOOLS = frozenset(_READ_TOOLS) & frozenset(TOOL_NAMES)


def _business_schema(fields):
    properties = {}
    required = []
    for field in fields:
        name, type_ = field["name"], field["type"]
        schema = {"type": type_}
        if type_ == "array":
            schema = {"type": "array"}
            if field.get("items"):
                schema["items"] = {"type": field["items"], "maxLength": 4096}
            if field.get("max_items"):
                schema["maxItems"] = field["max_items"]
            if "min_items" in field:
                schema["minItems"] = field["min_items"]
            if name == "ids":
                schema.update(minItems=1, uniqueItems=True)
                schema["items"]["minLength"] = 1
        elif type_ == "string":
            if field.get("enum"):
                schema["enum"] = field["enum"]
            else:
                is_big = name in _BIG_TEXT
                schema["maxLength"] = 65536 if is_big else 4096
            if name in _ID_FIELDS:
                schema["minLength"] = 1
            for key, keyword in (("min_length", "minLength"), ("max_length", "maxLength")):
                if key in field:
                    schema[keyword] = field[key]
        elif type_ == "integer":
            for key in ("minimum", "maximum"):
                if key in field:
                    schema[key] = field[key]
        if name in _FIELD_ERROR_CODES:
            schema["x-error-code"] = _FIELD_ERROR_CODES[name]
        desc = field["description"]
        if name in _ID_FIELDS:
            desc = desc + " " + _ID_HINT
        elif name in _FOLDER_FIELDS:
            desc = desc + " " + _FOLDER_HINT
        schema["description"] = desc
        if "default" in field and field["default"] is not None:
            schema["default"] = field["default"]
        properties[name] = schema
        if field.get("required"):
            required.append(name)
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema, required


def public_tools():
    result = []
    for name in TOOL_NAMES:
        fields = TOOLS[name]
        business_schema, business_required = _business_schema(fields)

        params = dict(business_schema)
        params["properties"]["lanid"] = {
            "type": "string", "minLength": 1, "maxLength": 128,
            "description": "Logged-in requester LANID; resolves the mailbox through OA on each call.",
        }
        params["properties"]["name"] = {
            "type": "string", "maxLength": 128,
            "description": "Optional legacy label; ignored for identity and authorization.",
        }
        params["required"] = ["lanid"] + [
            f for f in business_required if f not in ("lanid", "name")
        ]

        description = (
            DESCRIPTIONS[name]
            + " Mailbox comes only from lanid."
        )

        if name not in _READ_TOOLS:
            params["required"] = ["lanid"]
            params["properties"].update(
                {
                    "confirm_token": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2048,
                        "description": "Preview token for the identical operation.",
                    },
                    "operation_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "With lanid only: query the saved receipt without writing.",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "Caller key; completed operations replay the receipt without execution.",
                    },
                }
            )
            params["oneOf"] = [
                {"required": business_required,
                 "not": {"anyOf": [{"required": ["operation_id"]}, {"required": ["confirm_token"]}]}},
                {"required": business_required + ["operation_id", "confirm_token"]},
                {"required": ["operation_id"],
                 "not": {"anyOf": [{"required": [key]} for key in
                         [f["name"] for f in fields] + ["confirm_token", "idempotency_key"]]}},
            ]
            description += (
                " Preview first; confirm with identical params, operation_id and confirm_token. "
                "If uncertain, query the original operation_id; never retry blindly."
            )

        if name == "create_draft":
            # Applies to preview/confirm only; identity + operation_id stays valid.
            params["allOf"] = [{
                "if": {"anyOf": [{"not": {"required": ["operation_id"]}},
                                  {"required": ["confirm_token"]}]},
                "then": {
                    "if": {"required": ["mode"], "properties": {"mode": {"enum": ["reply", "reply_all", "forward"]}}},
                    "then": {"required": ["reply_to"]},
                    "else": {"not": {"required": ["reply_to"]}},
                },
            }]
        elif name == "update_draft":
            params["allOf"] = [{
                "dependentRequired": {
                    "body_format": ["body"],
                    "body_action": ["body"],
                },
            }]
        elif name == "set_message_flag":
            params["allOf"] = [{
                "if": {"required": ["flag"], "properties": {"flag": {"const": "clear"}}},
                "then": {"not": {"required": ["due_date"]}},
            }]
        elif name == "find_people":
            # Shared directories require explicit contacts source; auto/GAL cannot include them.
            params["allOf"] = [{
                "if": {"required": ["directory_id"]},
                "then": {
                    "required": ["source"],
                    "properties": {"source": {"const": "contacts"}},
                },
            }]

        result.append(
            {
                "name": name,
                "description": description,
                "inputSchema": params,
            }
        )
    return result


SPECS = {tool["name"]: tool for tool in public_tools()}
assert len(SPECS) == 30
