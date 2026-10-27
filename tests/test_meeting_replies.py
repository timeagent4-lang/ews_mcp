"""Meeting invitation text replies through the real SDK SOAP save boundary."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Account, Body, HTMLBody, Mailbox
from exchangelib.folders import Drafts, Root
from exchangelib.items.calendar_item import (
    MeetingCancellation, MeetingRequest, MeetingResponse,
)
from exchangelib.services.common import EWSService
from exchangelib.version import Build, Version
from requests import Response

from tool_support import ToolOperationError
from write_operations import WriteOperations


MAILBOX = "employee@example.com"
NS = {
    "m": "http://schemas.microsoft.com/exchange/services/2006/messages",
    "t": "http://schemas.microsoft.com/exchange/services/2006/types",
}
CREATE_RESPONSE = b'''<s:Envelope
 xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
 <s:Body><m:CreateItemResponse><m:ResponseMessages>
  <m:CreateItemResponseMessage ResponseClass="Success">
   <m:ResponseCode>NoError</m:ResponseCode>
   <m:Items><t:Message><t:ItemId Id="saved-reply" ChangeKey="saved-key"/></t:Message></m:Items>
  </m:CreateItemResponseMessage>
 </m:ResponseMessages></m:CreateItemResponse></s:Body>
</s:Envelope>'''


class MeetingReplyTests(unittest.TestCase):
    def setUp(self):
        self.account = Mock(spec=Account)
        self.account.primary_smtp_address = MAILBOX
        self.account.version = Version(build=Build(15, 2, 0, 0))
        self.account.protocol = Mock()
        self.account.protocol.version = self.account.version
        self.account.protocol.RETRY_WAIT = 1
        self.folder = Drafts(
            root=Root(account=self.account, id="root"),
            id="drafts", changekey="drafts-key",
        )
        self.ops = WriteOperations()
        self.ops.account = self.account
        self.ops.config = SimpleNamespace(email=MAILBOX)
        self.ops._tool_folder = Mock(return_value=self.folder)
        self.ops._tool_item = Mock()
        self.payloads = []

        def response(service, payload, api_version):
            # SOAP generation and parsing stay real; HTTP is the only replaced layer.
            self.assertEqual(service.SERVICE_NAME, "CreateItem")
            self.payloads.append(payload)
            result = Response()
            result.status_code = 200
            result._content = CREATE_RESPONSE
            result._content_consumed = True
            return result

        for patcher in (
            patch.object(EWSService, "_get_response", autospec=True, side_effect=response),
            patch("requests.sessions.Session.send", side_effect=AssertionError("Network is offline")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def invitation(self, **changes):
        values = {
            "account": self.account,
            "id": "invitation",
            "changekey": "invitation-key",
            "subject": "Planning",
            "body": Body("ORIGINAL INVITATION"),
            "author": Mailbox(email_address="organizer@example.com"),
            "sender": Mailbox(email_address="delegate@example.com"),
            "to_recipients": [Mailbox(email_address=MAILBOX)],
            "cc_recipients": [],
            "bcc_recipients": [Mailbox(email_address="hidden@example.com")],
            "my_response_type": "NoResponseReceived",
        }
        values.update(changes)
        item = MeetingRequest(**values)
        self.ops._tool_item.return_value = item
        return item

    def create(self, mode="reply", **changes):
        params = {"mode": mode, "reply_to": "invitation", "body": "Reply", "confirm": True}
        params.update(changes)
        try:
            return self.ops.create_draft(**params)
        except ToolOperationError as exc:
            self.fail(f"MeetingRequest {mode} rejected: {exc.code}: {exc.public_message}")

    def reply_xml(self):
        return self.payloads[-1].find("m:Items", NS)[0]

    def addresses(self, field):
        return self.reply_xml().xpath(
            f"t:{field}/t:Mailbox/t:EmailAddress/text()", namespaces=NS,
        )

    def test_preview_has_no_service_request_and_keeps_attendance_state(self):
        invitation = self.invitation()
        for mode in ("reply", "reply_all"):
            with self.subTest(mode=mode):
                preview = self.create(mode, confirm=False)
                self.assertTrue(preview["confirmation_required"])
                self.assertEqual(self.payloads, [])
                self.assertEqual(invitation.my_response_type, "NoResponseReceived")

    def test_confirmation_saves_native_replies_with_reference_and_current_author(self):
        for mode, element in (("reply", "ReplyToItem"), ("reply_all", "ReplyAllToItem")):
            with self.subTest(mode=mode):
                invitation = self.invitation()
                result = self.create(mode)
                self.assertEqual(result["id"], "saved-reply")
                self.assertEqual(result["changekey"], "saved-key")
                payload = self.payloads[-1]
                self.assertEqual(payload.get("MessageDisposition"), "SaveOnly")
                self.assertEqual(payload.get("SendMeetingInvitations"), "SendToNone")
                self.assertEqual(payload.find("m:SavedItemFolderId/t:FolderId", NS).get("Id"), "drafts")
                reply = self.reply_xml()
                self.assertEqual(reply.tag.rsplit("}", 1)[-1], element)
                reference = reply.find("t:ReferenceItemId", NS)
                self.assertEqual(reference.get("Id"), "invitation")
                self.assertEqual(reference.get("ChangeKey"), "invitation-key")
                self.assertEqual(reply.findtext("t:From/t:Mailbox/t:EmailAddress", namespaces=NS), MAILBOX)
                self.assertEqual(reply.findtext("t:Subject", namespaces=NS), "RE: Planning")
                self.assertIsNone(reply.find("t:Body", NS))
                self.assertEqual(invitation.my_response_type, "NoResponseReceived")

    def test_new_body_preserves_quote_type_and_escapes_plain_text(self):
        for mode in ("reply", "reply_all"):
            for source, body_type, expected in (
                (Body("ORIGINAL INVITATION"), "Text", "<b>A & B</b>\nNext"),
                (HTMLBody("<p>ORIGINAL INVITATION</p>"), "HTML", "&lt;b&gt;A &amp; B&lt;/b&gt;<br>Next"),
            ):
                with self.subTest(mode=mode, body_type=body_type):
                    self.invitation(body=source)
                    self.create(mode, body="<b>A & B</b>\nNext")
                    body = self.reply_xml().find("t:NewBodyContent", NS)
                    self.assertEqual(body.get("BodyType"), body_type)
                    self.assertEqual(body.text, expected)
                    self.assertNotIn("ORIGINAL", body.text)

    def test_html_fragment_is_native_new_body_for_both_reply_modes(self):
        fragment = '<p>A &amp; B</p><table><tr><td>Notes</td></tr></table>'
        for mode in ("reply", "reply_all"):
            with self.subTest(mode=mode):
                self.invitation(body=HTMLBody("<p>ORIGINAL INVITATION</p>"))
                self.create(mode, body=fragment, body_format="html")
                body = self.reply_xml().find("t:NewBodyContent", NS)
                self.assertEqual(body.get("BodyType"), "HTML")
                self.assertEqual(body.text, fragment)

    def test_reply_to_takes_priority_over_author_and_delegate_sender(self):
        self.invitation(reply_to=[Mailbox(email_address="reply-desk@example.com")])
        self.create()
        self.assertEqual(self.addresses("ToRecipients"), ["reply-desk@example.com"])
        self.assertEqual(self.addresses("BccRecipients"), [])

    def test_author_is_reply_target_when_reply_to_is_absent(self):
        self.invitation()
        self.create()
        self.assertEqual(self.addresses("ToRecipients"), ["organizer@example.com"])

    def test_reply_all_inference_excludes_self_and_deduplicates_without_copying_bcc(self):
        self.invitation(
            reply_to=[Mailbox(email_address="reply-desk@example.com")],
            to_recipients=[Mailbox(email_address=MAILBOX.upper()),
                           Mailbox(email_address="reply-desk@example.com"),
                           Mailbox(email_address="peer@example.com")],
            cc_recipients=[Mailbox(email_address="peer@example.com"),
                           Mailbox(email_address=MAILBOX),
                           Mailbox(email_address="copied@example.com")],
        )
        self.create("reply_all")
        self.assertEqual(self.addresses("ToRecipients"), ["reply-desk@example.com", "peer@example.com"])
        self.assertEqual(self.addresses("CcRecipients"), ["copied@example.com"])
        self.assertEqual(self.addresses("BccRecipients"), [])

    def test_explicit_self_and_bcc_are_preserved_and_deduplicated_across_groups(self):
        for mode in ("reply", "reply_all"):
            with self.subTest(mode=mode):
                self.invitation()
                self.create(
                    mode, to_emails=f"{MAILBOX};peer@example.com;PEER@example.com",
                    cc_emails="peer@example.com;copied@example.com",
                    bcc_emails="copied@example.com;private@example.com",
                )
                self.assertEqual(self.addresses("ToRecipients"), [MAILBOX, "peer@example.com"])
                self.assertEqual(self.addresses("CcRecipients"), ["copied@example.com"])
                self.assertEqual(self.addresses("BccRecipients"), ["private@example.com"])

    def test_other_meeting_types_and_invitation_forward_remain_rejected(self):
        for item, modes in (
            (MeetingResponse(account=self.account, id="response"), ("reply", "reply_all", "forward")),
            (MeetingCancellation(account=self.account, id="cancellation"), ("reply", "reply_all", "forward")),
            (self.invitation(), ("forward",)),
        ):
            self.ops._tool_item.return_value = item
            for mode in modes:
                with self.subTest(item=type(item).__name__, mode=mode):
                    with self.assertRaises(ToolOperationError) as caught:
                        self.ops.create_draft(
                            mode=mode, reply_to=item.id, body="Reply", confirm=True,
                        )
                    self.assertEqual(caught.exception.code, "NOT_MAIL_MESSAGE")
        self.assertEqual(self.payloads, [])


if __name__ == "__main__":
    unittest.main()
