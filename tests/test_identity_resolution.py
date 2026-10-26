import json
import os
import unittest
from dataclasses import asdict
from unittest.mock import Mock, patch

from utils import lanid_email as oa


SETTINGS = {
    "OA_BASE_URL": "https://oa.example.invalid/",
    "OA_PATH": "/contacts", "SVC_CD": "service", "SVC_SCN": "scene",
    "CNSMR_SYS_ID": "consumer", "TLR_NO": "operator", "OA_TIMEOUT": "3",
}
CONTACT = {"ChinNm1": "员工", "EmailAdr": "EMPLOYEE@Example.COM"}


class IdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, SETTINGS, clear=True)
        env.start()
        self.addCleanup(env.stop)
        post = patch.object(oa.requests, "post")
        self.post = post.start()
        self.addCleanup(post.stop)
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"Body": {"CtcInfArry": [CONTACT]}}
        self.post.return_value = self.response

    def resolve(self):
        return oa.resolve_identity_by_lanid(" DOMAIN\\employee ")

    def test_single_contact_normalizes_identity_and_sends_only_lanid(self):
        identity = self.resolve()
        self.assertEqual(asdict(identity), {
            "lanid": "employee", "email": "employee@example.com",
        })
        args, kwargs = self.post.call_args
        self.assertEqual(args, ("https://oa.example.invalid/contacts",))
        self.assertEqual(kwargs["timeout"], 3)
        body = json.loads(kwargs["data"].decode("utf-8"))["Body"]
        self.assertEqual(body, {"EmlNm": "", "LANIDCd": "employee", "Email": "", "ChinNm": ""})

    def test_missing_settings_and_bad_timeout_fail_before_network(self):
        for extra in ({"OA_BASE_URL": ""}, {"OA_TIMEOUT": "0"},
                      {"OA_TIMEOUT": "-1"}, {"OA_TIMEOUT": "bad"}):
            with self.subTest(extra=extra), patch.dict(os.environ, extra):
                with self.assertRaises(ValueError):
                    self.resolve()
        self.post.assert_not_called()

    def test_blank_lanid_fails_before_network(self):
        for lanid in (None, "", " \t ", "DOMAIN\\"):
            with self.subTest(lanid=lanid):
                with self.assertRaises(oa.IdentityResolutionError):
                    oa.resolve_identity_by_lanid(lanid)
        self.post.assert_not_called()

    def test_ambiguous_missing_or_malformed_contact_is_rejected(self):
        for contacts in ([], [CONTACT, CONTACT], None, {}, [None], ["employee"]):
            with self.subTest(contacts=contacts):
                self.response.json.return_value = {"Body": {"CtcInfArry": contacts}}
                with self.assertRaises(oa.IdentityResolutionError):
                    self.resolve()

    def test_missing_oa_name_does_not_block_email_resolution(self):
        self.response.json.return_value = {
            "Body": {"CtcInfArry": [{"EmailAdr": "EMPLOYEE@Example.COM"}]},
        }
        self.assertEqual(self.resolve().email, "employee@example.com")

    def test_oa_name_value_does_not_affect_email_resolution(self):
        for name in (None, "", " ", "另一位员工", 123, False, [], {}):
            with self.subTest(name=name):
                self.response.json.return_value = {"Body": {"CtcInfArry": [{**CONTACT, "ChinNm1": name}]}}
                self.assertEqual(self.resolve().email, "employee@example.com")

    def test_missing_or_malformed_response_body_is_rejected(self):
        for payload in ({}, {"Body": None}, {"Body": []}, {"Body": "invalid"}):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaises(oa.IdentityResolutionError):
                    self.resolve()

    def test_invalid_email_never_produces_resolved_identity(self):
        for email in (None, "", "no-at", "a@@example.com", "a@bad domain",
                      "a\n@example.com", ".a@example.com", "a..b@example.com",
                      "a@-example.com", "a@example..com", "a@example.com, b@example.com"):
            with self.subTest(email=email):
                self.response.json.return_value = {"Body": {"CtcInfArry": [{**CONTACT, "EmailAdr": email}]}}
                with self.assertRaises(oa.IdentityResolutionError):
                    self.resolve()

    def test_http_failure_and_invalid_json_are_upstream_errors(self):
        self.response.status_code = 503
        with self.assertRaises(oa.OAServiceError):
            self.resolve()
        self.response.status_code = 200
        self.response.json.side_effect = ValueError("not-json")
        with self.assertRaises(oa.OAServiceError):
            self.resolve()

    def test_json_root_must_be_an_object(self):
        for payload in (None, [], "upstream-error", 3):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaises(oa.OAServiceError):
                    self.resolve()


if __name__ == "__main__":
    unittest.main()
