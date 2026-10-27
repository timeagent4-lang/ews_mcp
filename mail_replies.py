"""Native text replies to meeting requests, without changing attendance."""

from exchangelib.items.message import ReplyAllToItem, ReplyToItem
from exchangelib.properties import ReferenceItemId


def meeting_text_reply(original, *, mode, subject, body, author, to, cc, bcc):
    response = ReplyAllToItem if mode == "reply_all" else ReplyToItem
    return response(
        account=original.account,
        reference_item_id=ReferenceItemId(id=original.id, changekey=original.changekey),
        subject=subject,
        new_body=body,
        author=author,
        to_recipients=to,
        cc_recipients=cc,
        bcc_recipients=bcc,
    )
