import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from llm_ae import mail
from llm_ae.config import AEError, initialize
from llm_ae.demo import MANUSCRIPT, demo
from llm_ae.runner import run
from llm_ae.store import Store, days_after
from llm_ae.workflow import Workflow


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = initialize(self.home)
        self.store = Store(self.home)
        self.w = Workflow(self.cfg, self.store)
        self.sub = self.w.add_submission(
            "Normal mean estimation",
            ["Alex Example"],
            "editor@example.org",
            MANUSCRIPT.encode(),
            "manuscript.txt",
            access_link="https://journal.example.org/1",
        )
        self.sid = self.sub["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def candidate(self, name="Taylor Reviewer", email="taylor@example.org"):
        c = self.w.add_candidate(self.sid, name, email, evidence="Fixture")
        return self.w.check_candidate(c["id"], "clear", "Verified identity and no known conflicts")

    def invitation(self):
        self.w.route_review(self.sid)
        return self.w.invite(self.candidate()["id"])

    def send(self, d):
        self.w.approve(d["id"])
        self.cfg["mail"]["transport"] = "smtp"
        with patch("llm_ae.mail.smtp_send") as sender:
            result = mail.deliver(self.w, d["id"])
            sender.assert_called_once()
        return result


class WorkflowTest(Fixture):
    def test_offline_assessment_never_makes_a_quality_verdict(self):
        result = self.w.assess(self.sid)
        self.assertEqual(result["recommendation"], "needs_human")
        self.assertEqual(self.w.submission(self.sid)["status"], "assessed")
        self.assertEqual(self.store.all("SELECT * FROM drafts"), [])

    def test_conflict_and_email_required(self):
        self.w.route_review(self.sid)
        c = self.w.add_candidate(self.sid, "Unverified", evidence="Arxiv")
        with self.assertRaises(AEError):
            self.w.invite(c["id"])
        with self.assertRaises(AEError):
            self.w.check_candidate(c["id"], "clear", "No address yet")

    def test_author_cannot_review_own_paper(self):
        c = self.w.add_candidate(self.sid, "Alex Example", "author@example.org")
        with self.assertRaises(AEError):
            self.w.check_candidate(c["id"], "clear", "Claimed clear")

    def test_invite_is_idempotent(self):
        d = self.invitation()
        a = self.store.get("assignments", d["assignment_id"])
        self.assertEqual(d["id"], self.w.invite(a["candidate_id"])["id"])
        self.assertEqual(len(self.store.all("SELECT * FROM drafts")), 1)

    def test_duplicate_email_cannot_receive_two_invitations(self):
        self.invitation()
        c = self.candidate("T. Reviewer", "taylor@example.org")
        with self.assertRaises(AEError):
            self.w.invite(c["id"])

    def test_preview_does_not_advance_workflow(self):
        d = self.invitation()
        self.w.approve(d["id"])
        result = mail.deliver(self.w, d["id"])
        self.assertEqual(result["status"], "preview")
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(self.store.get("assignments", d["assignment_id"])["status"], "draft")
        self.assertEqual(self.w.submission(self.sid)["status"], "reviewer_selection")

    def test_send_requires_approval(self):
        with self.assertRaises(AEError):
            mail.deliver(self.w, self.invitation()["id"])

    def test_edit_invalidates_approval(self):
        d = self.invitation()
        self.w.approve(d["id"])
        self.w.edit_draft(d["id"], "Changed body")
        with self.assertRaises(AEError):
            mail.deliver(self.w, d["id"])

    def test_sender_change_invalidates_approval(self):
        d = self.invitation()
        self.w.approve(d["id"])
        self.cfg["ae"]["name"] = "Another editor"
        with self.assertRaises(AEError):
            mail.deliver(self.w, d["id"])

    def test_conflict_change_blocks_delivery(self):
        d = self.invitation()
        self.w.approve(d["id"])
        a = self.store.get("assignments", d["assignment_id"])
        self.w.check_candidate(a["candidate_id"], "blocked", "Recent coauthor")
        with self.assertRaises(AEError):
            mail.deliver(self.w, d["id"])

    def test_no_duplicate_send(self):
        d = self.invitation()
        self.send(d)
        with patch("llm_ae.mail.smtp_send") as sender:
            with self.assertRaises(AEError):
                mail.deliver(self.w, d["id"])
            sender.assert_not_called()

    def test_uncertain_send_requires_reconciliation(self):
        d = self.invitation()
        self.w.approve(d["id"])
        self.cfg["mail"]["transport"] = "smtp"
        with patch("llm_ae.mail.smtp_send", side_effect=TimeoutError):
            with self.assertRaises(AEError):
                mail.deliver(self.w, d["id"])
        self.assertEqual(self.store.get("drafts", d["id"])["status"], "uncertain")
        with self.assertRaises(AEError):
            self.w.approve(d["id"])
        with self.assertRaises(AEError):
            self.w.response(d["assignment_id"], "cancel")
        mail.reconcile(self.w, d["id"], True, "Confirmed Message-ID in server logs")
        self.assertEqual(self.store.get("assignments", d["assignment_id"])["status"], "invited")
        with self.assertRaises(AEError):
            mail.reconcile(self.w, d["id"], True, "Repeated")

    def test_negative_reconciliation_requires_fresh_approval(self):
        d = self.invitation()
        self.store.db.execute("UPDATE drafts SET status='sending' WHERE id=?", (d["id"],))
        mail.reconcile(self.w, d["id"], False, "Server confirms rejection before DATA")
        self.assertEqual(self.store.get("drafts", d["id"])["status"], "draft")
        with self.assertRaises(AEError):
            mail.deliver(self.w, d["id"])

    def test_accept_cancels_pending_invitation_reminder(self):
        d = self.invitation()
        self.send(d)
        self.store.db.execute(
            "UPDATE assignments SET due_at=? WHERE id=?", (days_after(-1), d["assignment_id"])
        )
        reminders = self.w.reminders()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(self.w.reminders(), [])
        self.w.approve(reminders[0]["id"])
        self.w.response(d["assignment_id"], "accept")
        with self.assertRaises(AEError):
            mail.deliver(self.w, reminders[0]["id"])

    def test_reminder_limits_and_spacing(self):
        d = self.invitation()
        self.send(d)
        self.store.db.execute(
            "UPDATE assignments SET due_at=? WHERE id=?", (days_after(-1), d["assignment_id"])
        )
        reminders = self.w.reminders()
        self.send(reminders[0])
        self.assertEqual(self.w.reminders(), [])
        self.store.db.execute(
            "UPDATE assignments SET last_reminder_at=?,reminders=? WHERE id=?",
            (days_after(-30), self.cfg["workflow"]["max_reminders"], d["assignment_id"]),
        )
        self.assertEqual(self.w.reminders(), [])
        self.assertTrue(any(t["action"] == "Overdue: escalate to AE" for t in self.w.tasks()))

    def test_review_cannot_be_recorded_before_acceptance(self):
        d = self.invitation()
        self.send(d)
        with self.assertRaises(AEError):
            self.w.response(d["assignment_id"], "review", "Report", "accept")

    def test_decision_blocked_while_reviews_outstanding(self):
        d = self.invitation()
        self.send(d)
        with self.assertRaises(AEError):
            self.w.decision(self.sid, "desk_reject", "Some reason")

    def test_synthesis_waits_for_target_reports(self):
        d = self.invitation()
        self.send(d)
        self.w.response(d["assignment_id"], "accept")
        self.w.response(d["assignment_id"], "review", "Report", "revise")
        with self.assertRaises(AEError):
            self.w.assess(self.sid, "synthesis")

    def test_complete_review_revision_lifecycle(self):
        d = self.invitation()
        self.send(d)
        self.w.response(d["assignment_id"], "accept")
        self.w.response(d["assignment_id"], "review", "Clarify assumptions", "revise")
        decision = self.w.decision(self.sid, "revise", "Clarify assumptions per the report")
        self.send(decision)
        self.assertEqual(self.w.submission(self.sid)["status"], "decision_sent")
        self.w.editor_decision(self.sid, "revise", "Editor requested revision by email")
        self.w.revise(self.sid, (MANUSCRIPT + "\nRevision").encode(), "revision.txt")
        self.assertEqual(self.w.submission(self.sid)["round"], 2)
        self.assertEqual(len(self.store.all("SELECT * FROM documents")), 2)
        self.assertEqual(
            self.store.all("SELECT conflict FROM candidates")[0]["conflict"], "unchecked"
        )

    def test_desk_rejection_is_recommendation_until_editor_reply(self):
        d = self.w.decision(self.sid, "desk_reject", "Outside journal scope")
        self.send(d)
        self.assertEqual(self.w.submission(self.sid)["status"], "decision_sent")
        self.w.editor_decision(self.sid, "reject", "Editor confirmed")
        self.assertEqual(self.w.submission(self.sid)["status"], "closed")

    def test_runner_is_idempotent_and_does_not_send_unapproved_mail(self):
        with patch("llm_ae.mail.smtp_send") as sender:
            run(self.w, analyze=True, send=True)
            run(self.w, analyze=True, send=True)
            sender.assert_not_called()
        self.assertEqual(len(self.store.all("SELECT * FROM assessments")), 1)

    def test_filename_path_traversal_cannot_escape_documents(self):
        sub = self.w.add_submission(
            "Other", [], "editor@example.org", MANUSCRIPT.encode(), "../../escape.txt"
        )
        path = Path(self.w.document(sub["id"])["path"])
        self.assertEqual(path.parent, self.home.resolve() / "documents")

    def test_audit_records_decisions_and_approvals(self):
        d = self.w.decision(self.sid, "desk_reject", "Scope mismatch")
        self.w.approve(d["id"])
        actions = [row["action"] for row in self.store.all("SELECT * FROM audit")]
        self.assertIn("draft_created", actions)
        self.assertIn("draft_approved", actions)

    def test_rerouting_cancels_an_unsent_rejection(self):
        d = self.w.decision(self.sid, "desk_reject", "Initial assessment")
        self.w.approve(d["id"])
        self.w.route_review(self.sid)
        self.assertEqual(self.store.get("drafts", d["id"])["status"], "cancelled")
        with self.assertRaises(AEError):
            mail.deliver(self.w, d["id"])

    def test_metadata_change_invalidates_conflicts_and_pending_invitation(self):
        d = self.invitation()
        self.w.approve(d["id"])
        self.w.set_metadata(self.sid, authors=["Additional Author"], title="Correct title")
        self.assertEqual(self.store.get("drafts", d["id"])["status"], "cancelled")
        self.assertEqual(self.w.submission(self.sid)["title"], "Correct title")
        self.assertEqual(self.store.one("SELECT conflict FROM candidates")["conflict"], "unchecked")

    def test_unresolved_invitation_blocks_metadata_changes(self):
        d = self.invitation()
        self.store.db.execute("UPDATE drafts SET status='uncertain' WHERE id=?", (d["id"],))
        with self.assertRaises(AEError):
            self.w.set_metadata(self.sid, title="New title")

    def test_offline_demo_exercises_two_branches_without_network(self):
        with tempfile.TemporaryDirectory() as parent:
            with patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
                result = demo(Path(parent) / "demo")
            self.assertEqual(result["real_emails_sent"], 0)
            self.assertEqual({s["status"] for s in result["submissions"]}, {"received", "closed"})
            self.assertEqual(len(result["simulated_messages"]), 5)


class InboxTest(Fixture):
    def message(self, sender="editor@example.org", mid="<fixture@example.org>", attachments=1):
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "ae@example.org"
        msg["Subject"] = "New submission: normal mean"
        msg["Message-ID"] = mid
        msg.set_content("Please handle the attached manuscript.")
        for i in range(attachments):
            msg.add_attachment(
                MANUSCRIPT.encode(), maintype="text", subtype="plain", filename=f"paper-{i}.txt"
            )
        return msg

    def test_intake_deduplicates_message_id(self):
        raw = self.message().as_bytes()
        first, second = mail.ingest(self.w, raw), mail.ingest(self.w, raw)
        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.store.all("SELECT * FROM submissions")), 2)

    def test_unknown_sender_cannot_create_submission(self):
        result = mail.ingest(self.w, self.message("outsider@example.org").as_bytes())
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(len(self.store.all("SELECT * FROM submissions")), 1)

    def test_ambiguous_attachments_require_manual_selection(self):
        result = mail.ingest(self.w, self.message(attachments=2).as_bytes())
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["submission"])

    def test_threaded_reply_requires_matching_sender(self):
        d = self.invitation()
        self.send(d)
        msg = self.message("spoof@example.org", attachments=0)
        msg["In-Reply-To"] = d["message_id"]
        result = mail.ingest(self.w, msg.as_bytes())
        self.assertIsNone(result["assignment"])

    def test_reviewer_response_matches_thread_but_waits_for_confirmation(self):
        d = self.invitation()
        self.send(d)
        msg = self.message("taylor@example.org", attachments=0)
        msg["In-Reply-To"] = d["message_id"]
        result = mail.ingest(self.w, msg.as_bytes())
        self.assertEqual(result["assignment"], d["assignment_id"])
        self.assertEqual(self.store.get("assignments", d["assignment_id"])["status"], "invited")
        mail.process_inbound(self.w, result["message"], "accept")
        self.assertEqual(self.store.get("assignments", d["assignment_id"])["status"], "accepted")

    def test_oversize_message_rejected(self):
        self.cfg["mail"]["max_message_bytes"] = 10
        with self.assertRaises(AEError):
            mail.ingest(self.w, b"a" * 11)

    def test_intake_recovers_after_submission_saved_but_message_not_recorded(self):
        raw = self.message().as_bytes()
        first = mail.ingest(self.w, raw)
        self.store.db.execute("DELETE FROM messages WHERE id=?", (first["message"],))
        second = mail.ingest(self.w, raw)
        self.assertEqual(first["submission"], second["submission"])
        self.assertEqual(len(self.store.all("SELECT * FROM submissions")), 2)

    def test_unmatched_editor_reply_does_not_create_a_new_submission(self):
        message = self.message()
        message["In-Reply-To"] = "<unknown-thread@example.org>"
        result = mail.ingest(self.w, message.as_bytes())
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["submission"])


if __name__ == "__main__":
    unittest.main()
