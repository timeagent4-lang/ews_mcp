"""The tool vocabulary, wrapped in one strict unified params contract.

33 tools are defined; 28 are registered. Disabled tool definitions are retained
but excluded from registration via ``DISABLED_TOOLS``.

Mirrors the reference ``ews4s_oa_delegate`` registry: every tool exposes only a
single ``arguments.params`` object; ``lanid`` / ``name`` are always required; IDs are
mailbox-scoped; write tools route through the persistent two-phase confirmation
(``operation_id`` + ``confirm_token`` + optional ``idempotency_key``).
"""

from __future__ import annotations

# ---------------- field helpers ----------------

SIDE_READ = "read"
SIDE_WRITE = "write"

# string fields that may carry long free text (larger maxLength).
_BIG_TEXT = frozenset({"body", "message", "internal_reply", "external_reply", "query"})

# fields describing an item reference; get a mailbox-scoped hint.
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

# Folder references are a separate vocabulary: only the well-known aliases that
# list_folders reports are accepted, so a caller cannot construct an arbitrary
# FolderId and reach outside this employee's own mailbox.
_FOLDER_FIELDS = frozenset({"folder", "to_folder"})

_ID_HINT = (
    "Use the exact mailbox-scoped ID returned by this service, or a raw EWS ID. "
    "Cross-mailbox / unscoped short aliases are rejected."
)

_FOLDER_HINT = (
    "Use a well-known alias as returned by list_folders and accepted by this "
    "field's enum. Raw EWS folder IDs are rejected."
)

_READ_TOOLS = frozenset(
    {
        "list_folders",
        "find_message",
        "get_message",
        "get_thread",
        "get_attachment",
        "prepare_attachment_download",
        "get_mailbox_overview",
        "list_events",
        "get_event",
        "list_tasks",
        "find_people",
        "get_contact",
        "check_availability",
        "get_oof_settings",
        "waiting_on",
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


# ---------------- tool vocabulary ----------------

# keyed by tool name; value is a list of business parameter specs.
TOOLS: dict[str, list[dict]] = {
    # --- mail read ---
    "list_folders": [],
    "find_message": [
        _s("query", "string", "Subject contains this text; may combine with structured filters.", default=""),
        _s("aqs", "string", "Exchange AQS; cannot combine with nonempty query or structured filters. Omit when unused.", min_length=1),
        _s("folder", "string", "Mail folder to search.", default="inbox", enum=["inbox", "drafts", "sent"]),
        _s("since", "string", "Inclusive start: received time for inbox, sent time for sent, created time for drafts. Datetimes without a timezone use Asia/Shanghai.", default=None),
        _s("until", "string", "Exclusive end using the same folder time field as since; a date-only value means midnight at the start of that date in Asia/Shanghai.", default=None),
        _s("is_unread", "boolean", "True unread only, False read only; omit for no filter.", default=None),
        _s("has_attachments", "boolean", "True/False attachment filter.", default=None),
        _s("limit", "integer", "Max results.", default=10, minimum=1, maximum=100),
        _s("offset", "integer", "Pagination offset.", default=0, minimum=0),
        _s("include_body", "boolean", "Return up to 10000 body characters per message without automatically cleaning HTML; use get_message to read further.", default=True),
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
        _s("mode", "string", "info returns metadata only; auto/text read supported text attachments, otherwise return metadata with a notice. Omit attachment_id to list metadata in any mode.", default="auto", enum=["auto", "info", "text"]),

    ],
    "prepare_attachment_download": [
        _s("message_id", "string", "Scoped message ID owning the attachment.", required=True),
        _s("attachment_id", "string", "Exact attachment ID returned for this message; required.", required=True),
    ],
    "get_mailbox_overview": [
        _s("limit", "integer", "Recent-unread cap.", default=5, minimum=1, maximum=100),
    ],
    # --- draft / mail writes ---
    "create_draft": [
        _s("mode", "string", "Draft mode; reply/reply_all/forward require reply_to; new forbids reply_to.", default="new", enum=["new", "reply", "reply_all", "forward"]),
        _s("subject", "string", "Subject; omitted for reply/forward uses RE:/FW: prefix.", default=""),
        _s("body", "string", "Draft body.", default="", max_length=65536),
        _s("to_emails", "string", "To recipients, ; separated.", default=""),
        _s("cc_emails", "string", "Cc recipients, ; separated.", default=""),
        _s("bcc_emails", "string", "Bcc recipients, ; separated.", default=""),

        _s("reply_to", "string", "Original message ID for reply/forward.", default=None),
    ],
    "update_draft": [
        _s("draft_id", "string", "Scoped draft ID.", required=True),
        _s("subject", "string", "New subject; omit to leave unchanged.", default=None),
        _s("body", "string", "New body; omit to leave unchanged.", default=None, max_length=65536),
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
    "set_message_flag": [
        _s("message_id", "string", "Scoped message ID to flag.", required=True),
        _s("flag", "string", "Follow-up state to apply.", required=True, enum=["flagged", "complete", "clear"]),
        _s("due_date", "string", "ISO due date: flagged sets it (omission clears it); complete updates it when supplied (omission preserves it). For clear, omit this field; all flag dates are removed.", default=None),
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
        _s("start", "string", "Window start datetime; values without a timezone use Asia/Shanghai.", required=True),
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
        _s("event_id", "string", "Scoped event ID.", required=True),
        _s("subject", "string", "New subject.", default=None),
        _s("body", "string", "New body.", default=None, max_length=65536),
        _s("start", "string", "New start.", default=None),
        _s("end", "string", "New end.", default=None),
        _s("location", "string", "New location; empty string clears.", default=None),
        _s("notify_attendees", "boolean", "Notify attendees (requires send switch + confirm).", default=False),
    ],
    "respond_to_event": [
        _s("event_id", "string", "Scoped event ID.", required=True),
        _s("response", "string", "Attendee response.", required=True, enum=["accept", "tentative", "decline"]),
        _s("message", "string", "Response message.", default=None),
    ],
    "cancel_event": [
        _s("event_id", "string", "Organizer-owned scoped event ID.", required=True),
        _s("message", "string", "Cancellation note; omitting it does not suppress meeting cancellation notices.", default=None),
    ],
    # --- availability ---
    "check_availability": [
        _s("start", "string", "Window start.", required=True),
        _s("end", "string", "Window end.", required=True),
        _s("attendees", "array", "Attendee emails to check.", required=True, items="string", max_items=100),
        _s("duration", "integer", "Suggested slot duration in minutes.", default=30, maximum=1440),
    ],
    # --- people / contacts ---
    "find_people": [
        _s("query", "string", "Search text.", required=True),
        _s("source", "string", "gal, contacts, or auto.", default="auto", enum=["auto", "gal", "contacts"]),
        _s("limit", "integer", "Max per source.", default=20, maximum=100),
    ],
    "get_contact": [
        _s("contact_id", "string", "Scoped Contacts item ID, or exact GAL email.", required=True),
    ],
    "create_contact": [

        _s("email", "string", "Single contact email address; never selects the target mailbox.", required=True),
        _s("phone", "string", "Business phone.", default=None),
        _s("company_name", "string", "Company name.", default=None),
        _s("job_title", "string", "Job title.", default=None),
    ],
    # --- tasks ---
    "list_tasks": [
        _s("folder", "string", "Task folder alias.", default="tasks", enum=["tasks"]),
        _s("incomplete_only", "boolean", "Only incomplete tasks.", default=True),
        _s("limit", "integer", "Max results.", default=50, maximum=100),
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
    # --- mirror heuristic ---
    "waiting_on": [
        _s("id", "string", "Scoped message ID of the outgoing message.", required=True),
        _s("days", "integer", "Mirror window in days (max 365).", default=30, maximum=365),
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
    "list_folders": "Report which folders this employee can access, with each one's item and unread counts; an inaccessible folder is reported in coverage, it does not block the others.",
    "find_message": "Search one authorized employee mail folder: query matches subject text and supports structured filters; aqs is exclusive with nonempty query and structured filters. This does not search the service account mailbox; not finding a meeting notice in employee Sent does not establish delivery failure.",
    "get_message": "Read one scoped message with paginated body and attachment inventory; clean_body defaults to false, so HTML is preserved unless explicitly cleaned; stale IDs require a new search.",
    "get_thread": "Read a scoped conversation from employee Inbox and Sent; successful results include folder coverage and partial status. Each body is capped at 10000 characters; use get_message to read further. This does not search service-account Sent; missing meeting notices do not establish delivery failure.",
    "get_attachment": "List attachment metadata or read supported text files; the 5 MiB input and 20000-character output limits apply to text reading, not metadata listing. Other file types return metadata only. To download an original file, use prepare_attachment_download and then HTTP GET its URL from the employee execution environment.",
    "prepare_attachment_download": "Prepare an original file attachment for HTTP download after checking its message and mailbox scope. Returns download_url, filename, content_type, size, sha256 and expires_at; no file bytes or server paths. The URL is a temporary bearer credential: download into the task workspace, verify SHA-256, and do not publish it. Retry GET while valid; call this tool again after expiry. Requires server download configuration; item attachments are unsupported.",
    "get_mailbox_overview": "Read Inbox total/unread counts and recent unread messages only.",
    "create_draft": "Create a new draft or native EWS reply/reply_all/forward draft without sending; use an original message ID, not a consumed draft ID.",
    "update_draft": "Update supplied draft fields and align the draft author with the OA employee when needed; empty values clear supported fields. Confirmation re-reads the draft and does not lock its state between preview and execution.",
    "delete_draft": "Move one draft to DeletedItems; requires DeletedItems access; never permanently deletes or falls back to hard delete.",
    "send_draft": "Send one existing employee draft and save a copy in employee Sent; source draft ID is consumed, not a sent/received ID; no auto resend; idempotency_key persists across restarts.",
    "update_messages": "Update read status/categories of ordinary mail only; meeting request/cancellation objects are rejected per item.",
    "set_message_flag": "Set the Outlook follow-up flag (flagged/complete/clear) on one ordinary message; meeting request/cancellation objects are rejected.",
    "list_flagged_messages": "List items with open or completed follow-up flags in one authorized mail folder, open flags by default; status=all includes both states and excludes unflagged items.",
    "move_messages": "Move ordinary non-draft mail between this employee's Inbox and Sent only; use the returned NEW ID after moving; old ID is not rebound.",
    "delete_messages": "Move ordinary mail to DeletedItems only; soft/permanent deletion is prohibited; batch outcomes identify failed/uncertain items; no auto retries.",
    "list_events": "List calendar occurrences in a time window with pagination and instance expansion. has_more is a full-page hint; the next page may still be empty.",
    "get_event": "Read one scoped calendar event with organizer, attendees, response and recurrence metadata.",
    "create_event": "Create an event returning its real ID; invitations off by default; send_invitations=true emails attendees and requires the send switch.",
    "update_event": "Update an organizer-owned single event or explicit occurrence; recurring masters rejected; notify_attendees=false is silent; empty location clears it.",
    "respond_to_event": "Accept, decline or tentatively respond as an attendee, sending a response after confirmation and the send switch check; organizer-owned and recurring-master objects are rejected.",
    "cancel_event": "Cancel an organizer-owned event: meetings send native EWS cancellation notices and require the send switch; only a non-meeting appointment with no attendees is moved to Deleted Items without a notice. Omitting message does not suppress cancellation notices; recurring masters rejected.",
    "check_availability": "Query directory free/busy and suggest shared slots; any unavailable or NoData attendee prevents claiming a mutually free slot.",
    "find_people": "Search the organization directory (GAL) and/or the OA mailbox Contacts folder independently; different sources with separate permissions and coverage.",
    "get_contact": "Read a scoped Contacts item, or resolve an exact unique email in the organization directory; directory address cannot switch the target mailbox.",
    "create_contact": "Create one contact in the OA employee's own Contacts folder; never touches the GAL and sends no mail.",
    "list_tasks": "Read scoped Exchange tasks live, incomplete by default; use returned IDs for update_task.",
    "create_task": "Create one task in the OA employee's own Tasks folder; sends no mail and invites nobody.",
    "update_task": "Update a scoped task completion/due date using a checked change key; no fields means no write.",
    "waiting_on": "Opt-in local mirror follow-up heuristic, not proof of no reply; requires current online Inbox/Sent permissions and complete bounded mirror coverage.",
    "get_oof_settings": "Read this OA mailbox out-of-office settings; folder delegation may not authorize this mailbox-level operation.",
    "set_oof": "Set employee out-of-office settings; omitting external_reply disables external replies, supplying nonempty external_reply enables audience All (shown in preview); internal text is never copied outward by default.",
    "get_server_status": "Return the current mailbox, whether this adapter has an account, send/cache flags, available mirror coverage and the data-directory basename; no credentials or other mailbox statistics.",
}

# Notification-copy placement is deployment-dependent; employee Sent is not a
# delivery receipt. Keep the existing EWS send/save behavior unchanged.
for _name in ("create_event", "update_event", "cancel_event"):
    DESCRIPTIONS[_name] += (
        " Meeting notification copies are not guaranteed to appear in employee Sent; "
        "a missing copy alone does not establish sending failure or justify resending."
    )

# ---------------- deliberately unregistered tools ----------------
# These definitions are retained but are NOT registered, so no external caller
# can see or invoke them. Re-exposure also requires the corresponding business
# module and deployment permissions. Three distinct reasons live here:
#   * delete_draft / delete_messages -- deletion-class policy: withheld by
#     decision, not a defect.
#   * get_oof_settings / set_oof -- OOF is a mailbox-level Exchange right this
#     delegated service account does not hold on the current deployment; even a
#     correct request is refused with ErrorAccessDenied, so the tool could only
#     ever fail. Re-expose only if the right is actually granted.
#   * waiting_on -- requires the local mirror (EWS_MCP_CACHE_ENABLED=true),
#     which is off; without it the tool can only ever refuse.
DISABLED_TOOLS = frozenset(
    {
        "delete_draft",
        "delete_messages",
        "get_oof_settings",
        "set_oof",
        "waiting_on",
    }
)

TOOL_NAMES = [name for name in TOOLS if name not in DISABLED_TOOLS]
assert len(TOOL_NAMES) == 28, f"expected 28 registered tools, got {len(TOOL_NAMES)}"

# Registered read tools: kept in step with TOOL_NAMES so this constant never
# advertises a tool the dispatcher cannot route.
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
    """Build registration specs from the current enabled tool set."""
    result = []
    for name in TOOL_NAMES:
        fields = TOOLS[name]
        business_schema, business_required = _business_schema(fields)

        params = dict(business_schema)
        params["properties"]["lanid"] = {
            "type": "string", "minLength": 1, "maxLength": 128,
            "description": "Requester LANID; verified against OA on every call.",
        }
        params["properties"]["name"] = {
            "type": "string", "minLength": 1, "maxLength": 128,
            "description": "Requester Chinese name; must match the OA ChinNm.",
        }
        params["required"] = ["lanid", "name"] + [
            f for f in business_required if f not in ("lanid", "name")
        ]

        description = (
            DESCRIPTIONS[name]
            + " OA verification is required on every call; the target mailbox comes only from OA."
        )

        if name not in _READ_TOOLS:
            params["required"] = ["lanid", "name"]
            params["properties"].update(
                {
                    "confirm_token": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2048,
                        "description": "Return the preview token unchanged to execute the identical operation.",
                    },
                    "operation_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "With identity only: query a receipt without repeating any write.",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "Optional caller-chosen key; a completed key returns its receipt and never re-executes.",
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
                " All writes require a preview, then identical params plus operation_id and "
                "confirm_token. Query operation_id after an uncertain response; never blindly retry."
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
        elif name == "find_message":
            params["allOf"] = [{
                "if": {"required": ["aqs"]},
                "then": {
                    "properties": {"query": {"const": ""}},
                    "not": {"anyOf": [{"required": [key]} for key in
                                      ("since", "until", "is_unread", "has_attachments")]},
                },
            }]

        result.append(
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": {"params": params},
                    "required": ["params"],
                    "additionalProperties": False,
                },
            }
        )
    return result


SPECS = {tool["name"]: tool for tool in public_tools()}
assert len(SPECS) == 28
