import base64
import io
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from llm_ae import gmail, mail, oauth, setup
from llm_ae.config import AEError, initialize
from llm_ae.store import Store
from llm_ae.workflow import Workflow


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = initialize(Path(self.tmp.name))
        self.cfg["mail"].update(oauth.mail_settings("google", "ae@example.org"))
        self.cfg["ae"]["email"] = "ae@example.org"

    def test_only_read_and_send_scopes(self):
        self.assertEqual(
            set(oauth.PROVIDERS["google"]["scopes"]),
            {
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            },
        )
        with self.assertRaises(AEError):
            oauth.normalized_tokens(
                "google",
                {
                    "access_token": "x",
                    "refresh_token": "y",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                },
            )

    def test_onboarding_checks_identity_without_reading_or_sending_messages(self):
        with patch("llm_ae.gmail.request", return_value={"emailAddress": "ae@example.org"}) as req:
            setup.check_mail(self.cfg)
        req.assert_called_once_with(self.cfg, "profile")
        with patch("llm_ae.gmail.request", return_value={"emailAddress": "wrong@example.org"}):
            with self.assertRaisesRegex(AEError, "different mailbox"):
                setup.check_mail(self.cfg)
        with patch("llm_ae.mail.imaplib.IMAP4_SSL") as imap:
            with self.assertRaises(AEError):
                mail.connect_imap(self.cfg)
            imap.assert_not_called()

    def test_fixed_origin_bounded_response_and_sanitized_error(self):
        with (
            patch("llm_ae.gmail.access_token", return_value="private-token"),
            patch("llm_ae.gmail.urlopen", return_value=io.BytesIO(b'{"messages":[]}')) as opener,
        ):
            gmail.request(self.cfg, "messages", query={"q": "after:123", "labelIds": "INBOX"})
        req = opener.call_args.args[0]
        self.assertEqual(urlsplit(req.full_url).hostname, "gmail.googleapis.com")
        self.assertEqual(parse_qs(urlsplit(req.full_url).query)["labelIds"], ["INBOX"])
        self.assertEqual(req.get_method(), "GET")
        with self.assertRaises(AEError):
            gmail.request(self.cfg, "https://evil.example.org/")
        with self.assertRaises(AEError):
            gmail.request(self.cfg, "messages/delete", body={})
        with (
            patch("llm_ae.gmail.access_token", return_value="private-token"),
            patch("llm_ae.gmail.urlopen", return_value=io.BytesIO(b"x" * 20)),
        ):
            with self.assertRaises(gmail.MessageTooLarge):
                gmail.request(self.cfg, "profile", limit=10)
        error = HTTPError(
            "https://gmail.googleapis.com/", 403, "private diagnostic", {}, io.BytesIO()
        )
        with (
            patch("llm_ae.gmail.access_token", return_value="private-token"),
            patch("llm_ae.gmail.urlopen", side_effect=error),
        ):
            with self.assertRaisesRegex(AEError, "HTTP 403") as caught:
                gmail.request(self.cfg, "profile")
        self.assertNotIn("private", str(caught.exception))

    def test_pagination_and_immutable_ids_do_not_skip_lower_ids(self):
        raw = b"From: editor@example.org\r\nSubject: hello\r\n\r\nbody"
        responses = [
            {"messages": [{"id": "ff"}], "nextPageToken": "page2"},
            {"messages": [{"id": "01"}, {"id": "aa"}]},
            {"sizeEstimate": len(raw)},
            {"raw": base64.urlsafe_b64encode(raw).decode()},
            {"sizeEstimate": len(raw)},
            {"raw": base64.urlsafe_b64encode(raw).decode()},
        ]
        with patch("llm_ae.gmail.request", side_effect=responses) as req:
            result = list(mail.fetch_messages(self.cfg, unseen_uids={"gmail:ff": 1}))
        self.assertEqual(result, [("gmail:01", "1", raw), ("gmail:aa", "1", raw)])
        self.assertEqual(req.call_args_list[1].kwargs["query"]["pageToken"], "page2")

    def test_size_limit_and_malformed_fetch_preserve_retry(self):
        self.cfg["mail"]["max_message_bytes"] = 10
        with patch(
            "llm_ae.gmail.request", side_effect=[{"messages": [{"id": "a"}]}, {"sizeEstimate": 11}]
        ) as req:
            self.assertEqual(list(gmail.fetch_messages(self.cfg)), [("gmail:a", "1", None)])
        self.assertEqual(req.call_count, 2)
        with patch(
            "llm_ae.gmail.request",
            side_effect=[{"messages": [{"id": "a"}]}, {"sizeEstimate": 1}, {"raw": "bad!"}],
        ):
            with self.assertRaises(AEError):
                list(gmail.fetch_messages(self.cfg))

    def test_sync_checkpoints_each_id_and_deduplicates_next_batch(self):
        raw = b"From: editor@example.org\r\nSubject: hello\r\nMessage-ID: <test@example.org>\r\n\r\nbody"
        store = Store(self.cfg.home)
        self.addCleanup(store.close)
        workflow = Workflow(self.cfg, store)

        def api(config, path, **kwargs):
            if path == "messages":
                return {"messages": [{"id": "a"}]}
            if kwargs["query"]["format"] == "minimal":
                return {"sizeEstimate": len(raw)}
            return {"raw": base64.urlsafe_b64encode(raw).decode()}

        with patch("llm_ae.gmail.request", side_effect=api):
            self.assertEqual(len(mail.sync(workflow)), 1)
            self.assertEqual(mail.sync(workflow), [])
        self.assertEqual(len(store.all("SELECT * FROM messages")), 1)

    def test_send_preserves_message_id_and_does_not_retry_network_failure(self):
        msg = EmailMessage()
        msg["To"] = "reviewer@example.org"
        msg["From"] = "ae@example.org"
        msg["Message-ID"] = "<stable@example.org>"
        msg.set_content("Invitation")
        with patch("llm_ae.gmail.request", return_value={"id": "sent-id"}) as req:
            mail.smtp_send(self.cfg, msg)
        self.assertEqual(req.call_args.args[1], "messages/send")
        self.assertIn(
            b"<stable@example.org>", base64.urlsafe_b64decode(req.call_args.kwargs["body"]["raw"])
        )
        with (
            patch("llm_ae.gmail.access_token", return_value="secret"),
            patch("llm_ae.gmail.urlopen", side_effect=URLError("timeout")) as opener,
        ):
            with self.assertRaises(AEError):
                gmail.send(self.cfg, msg)
        self.assertEqual(opener.call_count, 1)

    def test_custom_label_resolution(self):
        with patch(
            "llm_ae.gmail.request",
            return_value={"labels": [{"id": "Label_1", "name": "Editorial"}]},
        ):
            self.assertEqual(gmail.label_id(self.cfg, "Editorial"), "Label_1")
            with self.assertRaises(AEError):
                gmail.label_id(self.cfg, "Missing")
        self.assertEqual(gmail.label_id(self.cfg, "[Gmail]/Sent Mail"), "SENT")
