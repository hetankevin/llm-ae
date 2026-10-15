import io
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

from llm_ae import discovery, llm, mail
from llm_ae.cli import locked, main
from llm_ae.config import AEError, Config, initialize
from llm_ae.demo import MANUSCRIPT
from llm_ae.store import Store
from llm_ae.workflow import Workflow


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = initialize(Path(self.tmp.name))
        self.store = Store(self.cfg.home)
        self.w = Workflow(self.cfg, self.store)
        self.sub = self.w.add_submission(
            "Normal mean", ["Alex Example"], "editor@example.org", MANUSCRIPT.encode(), "paper.txt"
        )
        self.doc = self.w.document(self.sub["id"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def valid_assessment(self):
        return {
            "summary": "A mean estimator",
            "recommendation": "review",
            "strengths": ["Clear scope"],
            "concerns": [
                {
                    "issue": "Check moment assumptions",
                    "quote": "finite variance",
                    "location": "Paragraph 1",
                }
            ],
            "questions": ["Does the result extend?"],
            "limitations": ["No proof verification"],
            "reviewer_topics": ["mean estimation"],
            "editor_note": "Recommend expert review of the assumptions.",
        }

    def configure_remote(self):
        self.cfg["llm"].update(provider="openai", model="test-model", allow_remote_manuscripts=True)

    def response(self, value):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(value).encode()
        return response

    def test_remote_manuscripts_require_explicit_configuration(self):
        self.cfg["llm"].update(provider="openai", model="test-model")
        with patch("llm_ae.llm.urlopen") as network:
            with self.assertRaises(AEError):
                llm.assess(self.cfg, self.sub, self.doc)
            network.assert_not_called()

    def test_responses_api_payload_and_result(self):
        self.configure_remote()
        response = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(self.valid_assessment())}
                    ],
                }
            ],
        }
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}):
            with patch("llm_ae.llm.urlopen", return_value=self.response(response)) as network:
                result = llm.assess(self.cfg, self.sub, self.doc)
                request = network.call_args.args[0]
                payload = json.loads(request.data)
                self.assertFalse(payload["store"])
                self.assertEqual(payload["text"]["format"]["type"], "json_schema")
                self.assertNotIn("tools", payload)
                self.assertIn("untrusted", payload["instructions"])
                self.assertEqual(result["recommendation"], "review")
                self.assertTrue(result["concerns"][0]["quote_verified"])

    def test_ungrounded_quotes_force_manual_review(self):
        result = self.valid_assessment()
        result["recommendation"] = "desk_reject"
        result["concerns"][0]["quote"] = "A completely invented claim"
        result = llm.validate(result, "triage", MANUSCRIPT)
        self.assertEqual(result["recommendation"], "needs_human")
        self.assertFalse(result["concerns"][0]["quote_verified"])

    def test_unknown_model_fields_rejected(self):
        result = self.valid_assessment()
        result["recipient"] = "attacker@example.org"
        with self.assertRaises(AEError):
            llm.validate(result, "triage", MANUSCRIPT)

    def test_input_is_never_silently_truncated(self):
        self.configure_remote()
        self.cfg["llm"]["max_input_chars"] = 10
        with patch("llm_ae.llm.urlopen") as network:
            with self.assertRaises(AEError):
                llm.assess(self.cfg, self.sub, self.doc)
            network.assert_not_called()

    def test_refusal_does_not_create_assessment(self):
        self.configure_remote()
        response = {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}],
        }
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}):
            with patch("llm_ae.llm.urlopen", return_value=self.response(response)):
                with self.assertRaises(AEError):
                    self.w.assess(self.sub["id"])
        self.assertEqual(self.store.all("SELECT * FROM assessments"), [])

    def test_incomplete_model_response_does_not_create_assessment(self):
        self.configure_remote()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}):
            with patch("llm_ae.llm.urlopen", return_value=self.response({"status": "incomplete"})):
                with self.assertRaises(AEError):
                    self.w.assess(self.sub["id"])

    def test_malformed_model_response_shape_fails_cleanly(self):
        self.configure_remote()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}):
            with patch("llm_ae.llm.urlopen", return_value=self.response(["unexpected"])):
                with self.assertRaisesRegex(AEError, "malformed JSON"):
                    self.w.assess(self.sub["id"])
        self.assertEqual(self.store.all("SELECT * FROM assessments"), [])

    def test_arxiv_results_preserve_source_and_do_not_invent_email(self):
        xml = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/1234.5678</id>
        <title>A public paper</title><author><name>Sample Researcher</name></author></entry></feed>"""
        with patch("llm_ae.discovery.fetch", return_value=xml) as network:
            candidates = discovery.discover(self.w, self.sub["id"], "arxiv", "all:statistics")
            self.assertEqual(candidates[0]["email"], "")
            self.assertEqual(candidates[0]["conflict"], "unchecked")
            self.assertIn("1234.5678", candidates[0]["evidence"])
            repeated = discovery.discover(self.w, self.sub["id"], "arxiv", "all:statistics")
            self.assertEqual(candidates[0]["id"], repeated[0]["id"])
            network.assert_called_once()

    def test_openalex_includes_affiliation_and_author_identity(self):
        payload = {
            "results": [
                {
                    "title": "Public paper",
                    "id": "https://openalex.org/W1",
                    "authorships": [
                        {
                            "author": {
                                "display_name": "Sample Researcher",
                                "id": "https://openalex.org/A1",
                            },
                            "institutions": [{"display_name": "Example University"}],
                        }
                    ],
                }
            ]
        }
        with patch("llm_ae.discovery.fetch", return_value=json.dumps(payload).encode()):
            candidates = discovery.discover(self.w, self.sub["id"], "openalex", "mean estimation")
        self.assertEqual(candidates[0]["affiliation"], "Example University")
        self.assertEqual(
            json.loads(candidates[0]["evidence"])["author_id"], "https://openalex.org/A1"
        )

    def test_scholar_is_a_link_and_makes_no_network_call(self):
        with patch("llm_ae.discovery.fetch") as network:
            result = discovery.discover(self.w, self.sub["id"], "scholar", "mean estimation")
            network.assert_not_called()
        self.assertTrue(result["url"].startswith("https://scholar.google.com/"))

    def test_imap_read_only_peek_size_and_cursor(self):
        client = MagicMock()
        client.select.return_value = ("OK", [b"3"])
        client.response.return_value = ("UIDVALIDITY", [b"999"])

        def uid(command, *args):
            if command == "search":
                return "OK", [b"3 1 2"]  # SEARCH results need not arrive sorted.
            if args[1] == "(RFC822.SIZE)":
                return "OK", [b"2 (RFC822.SIZE 100)"]
            return "OK", [(b"2 (BODY[] {100}", b"From: editor@example.org\r\n\r\nhello"), b")"]

        client.uid.side_effect = uid
        self.cfg["mail"]["max_messages"] = 1
        with patch("llm_ae.mail.connect_imap", return_value=client):
            messages = list(mail.fetch_messages(self.cfg, unseen_uids={"999": 1}))
        self.assertEqual(messages[0][1], "2")
        client.select.assert_called_once_with("INBOX", readonly=True)
        self.assertIn(unittest.mock.call("fetch", b"2", "(BODY.PEEK[])"), client.uid.call_args_list)
        client.logout.assert_called_once()

    def test_imap_uidvalidity_reset_restarts_uid_sequence(self):
        client = MagicMock()
        client.select.return_value = ("OK", [b"1"])
        client.response.return_value = ("UIDVALIDITY", [b"new"])
        client.uid.side_effect = [
            ("OK", [b"1"]),
            ("OK", [b"1 (RFC822.SIZE 10)"]),
            ("OK", [(b"", b"message")]),
        ]
        with patch("llm_ae.mail.connect_imap", return_value=client):
            messages = list(mail.fetch_messages(self.cfg, unseen_uids={"old": 100}))
        self.assertEqual(messages[0][:2], ("new", "1"))

    def test_imap_missing_uidvalidity_does_not_guess_a_cursor(self):
        client = MagicMock()
        client.select.return_value = ("OK", [b"1"])
        client.response.return_value = ("UIDVALIDITY", [None])
        with patch("llm_ae.mail.connect_imap", return_value=client):
            with self.assertRaisesRegex(AEError, "UIDVALIDITY"):
                list(mail.fetch_messages(self.cfg))
        client.uid.assert_not_called()

    def test_smtp_starttls_never_authenticates_before_tls(self):
        self.cfg["mail"].update(
            smtp_host="smtp.example.org", username="ae@example.org", smtp_security="starttls"
        )
        msg = mail.build_email(
            self.w,
            {
                "recipient": "reviewer@example.org",
                "subject": "Invitation",
                "message_id": "<test@example.org>",
                "reply_to_id": None,
                "body": "Test",
            },
        )
        client = MagicMock()
        client.send_message.return_value = {}
        with patch.dict("os.environ", {"LLM_AE_MAIL_PASSWORD": "test-secret"}):
            with patch("llm_ae.mail.smtplib.SMTP", return_value=client):
                mail.smtp_send(self.cfg, msg)
        names = [c[0] for c in client.method_calls]
        self.assertLess(names.index("starttls"), names.index("login"))
        self.assertEqual(client.send_message.call_args.kwargs["from_addr"], "ae@example.org")
        self.assertEqual(client.send_message.call_args.kwargs["to_addrs"], ["reviewer@example.org"])

    def test_cli_lock_prevents_overlapping_work_cycles(self):
        with locked(self.cfg.home):
            with self.assertRaises(AEError):
                with locked(self.cfg.home):
                    self.fail("Lock must be exclusive")

    def test_cli_json_status_and_error_exit_code(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            status = main(["--home", str(self.cfg.home), "--json", "status"])
        self.assertEqual(status, 0)
        self.assertEqual(len(json.loads(output.getvalue())["submissions"]), 1)
        with patch("sys.stderr", new_callable=io.StringIO) as output:
            status = main(
                ["--home", str(self.cfg.home), "--json", "submissions", "show", "missing"]
            )
        self.assertEqual(status, 1)
        self.assertIn("error", json.loads(output.getvalue()))

    def test_boolean_strings_are_not_treated_as_authorization(self):
        path = self.cfg.home / "config.toml"
        path.write_text(
            path.read_text().replace(
                "allow_remote_manuscripts = false", 'allow_remote_manuscripts = "false"'
            )
        )
        with self.assertRaises(AEError):
            Config(self.cfg.home)

    def test_contacts_accumulate_topics_and_deduplicate_observations(self):
        def message(mid, subject):
            msg = EmailMessage()
            msg["From"] = "ae@example.org"
            msg["To"] = "Taylor Reviewer <taylor@example.org>"
            msg["Message-ID"] = mid
            msg["Subject"] = subject
            msg.set_content("Private content should not be indexed")
            return msg.as_bytes()

        first = message("<contact1@example.org>", "causal inference")
        second = message("<contact2@example.org>", "missing data")
        self.assertEqual(mail._observe_contacts(self.w, first, "Sent"), 1)
        self.assertEqual(mail._observe_contacts(self.w, first, "Sent"), 0)
        self.assertEqual(mail._observe_contacts(self.w, second, "Sent"), 1)
        contact = self.store.one("SELECT * FROM contacts")
        self.assertEqual(contact["interactions"], 2)
        self.assertIn("causal inference", contact["topics"])
        self.assertIn("missing data", contact["topics"])
        self.assertNotIn("Private content", contact["topics"])
        candidates = discovery.discover(self.w, self.sub["id"], "contacts", "causal inference")
        self.assertEqual(candidates[0]["email"], "taylor@example.org")

    def test_contacts_scan_uses_saved_cursor(self):
        with patch("llm_ae.mail.fetch_messages", return_value=iter([("1", "20", None)])):
            mail.scan_contacts(self.w, "Sent")
        with patch("llm_ae.mail.fetch_messages", return_value=iter([])) as fetch:
            mail.scan_contacts(self.w, "Sent")
        self.assertEqual(fetch.call_args.kwargs["unseen_uids"], {"1": 20})

    def test_invalid_config_types_fail_with_actionable_error(self):
        path = self.cfg.home / "config.toml"
        path.write_text(path.read_text().replace('email = "ae@example.org"', "email = 123"))
        with self.assertRaisesRegex(AEError, "ae.email"):
            Config(self.cfg.home)


if __name__ == "__main__":
    unittest.main()
