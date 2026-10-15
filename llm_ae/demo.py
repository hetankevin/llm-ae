"""A deliberately synthetic end-to-end demonstration. No network is used."""

from email import policy
from email.message import EmailMessage
from unittest.mock import patch

from . import mail
from .config import initialize
from .runner import run
from .store import Store, days_after
from .workflow import Workflow

MANUSCRIPT = """A synthetic manuscript for the LLM-AE demo
Authors: Alex Example, Sam Example

We study estimation of a normal mean from independent observations. The sample
mean is unbiased under finite first moments, and its variance is sigma squared
divided by the number of observations when the observations are independent and
identically distributed with finite variance. This fixture is not a real paper.
"""


def demo(home):
    config = initialize(home)  # Refuse to touch an existing editorial database.
    store = Store(config.home)
    workflow = Workflow(config, store)
    transcript = []

    def fake_smtp(cfg, msg):
        path = cfg.home / "outbox" / (str(msg["Message-ID"]).split("@")[0].lstrip("<") + ".eml")
        path.write_bytes(msg.as_bytes(policy=policy.SMTP))
        path.chmod(0o600)
        transcript.append({"to": str(msg["To"]), "subject": str(msg["Subject"]), "path": str(path)})

    def simulate_send(draft):
        workflow.approve(draft["id"])
        store.audit("demo_simulated_delivery", draft["id"])
        config["mail"]["transport"] = "smtp"
        try:
            with patch("llm_ae.mail.smtp_send", fake_smtp):
                mail.deliver(workflow, draft["id"])
        finally:
            config["mail"]["transport"] = "file"

    try:
        message = EmailMessage()
        message["From"] = "editor@example.org"
        message["To"] = "ae@example.org"
        message["Subject"] = "New submission: normal mean estimation"
        message["Message-ID"] = "<demo-submission@example.org>"
        message.set_content("Please handle this synthetic submission.")
        message.add_attachment(
            MANUSCRIPT.encode(), maintype="text", subtype="plain", filename="manuscript.txt"
        )
        imported = mail.ingest(workflow, message.as_bytes())
        sid = imported["submission"]
        workflow.set_metadata(
            sid, ["Alex Example", "Sam Example"], "https://journal.example.org/manuscripts/demo"
        )
        assessment = workflow.assess(sid)
        workflow.route_review(sid)
        assignments = []
        for name, email in (
            ("Taylor Reviewer", "taylor@example.org"),
            ("Jordan Reviewer", "jordan@example.org"),
        ):
            c = workflow.add_candidate(sid, name, email, evidence="Synthetic demo contact")
            workflow.check_candidate(
                c["id"], "clear", "Synthetic fixture: no real identity or conflict check"
            )
            draft = workflow.invite(c["id"])
            simulate_send(draft)
            assignments.append(draft["assignment_id"])
        # Demonstrate deadline chasing and cancellation of obsolete reminders.
        store.db.execute(
            "UPDATE assignments SET due_at=? WHERE id=?", (days_after(-2), assignments[0])
        )
        for draft in workflow.reminders():
            simulate_send(draft)
        for aid in assignments:
            workflow.response(aid, "accept")
            workflow.response(
                aid,
                "review",
                "Synthetic report: clarify the finite-variance assumption and provide a worked example.",
                "revise",
            )
        workflow.assess(sid, "synthesis")
        decision = workflow.decision(
            sid,
            "revise",
            "Both synthetic reports ask for a clearer statement of assumptions and an example.",
        )
        simulate_send(decision)
        workflow.editor_decision(sid, "revise", "Synthetic editor response requesting revision")
        workflow.revise(
            sid,
            (MANUSCRIPT + "\nRevision: finite variance is now an explicit assumption.\n").encode(),
            "revision.txt",
        )
        # A second submission demonstrates the desk-rejection route.
        rejected = workflow.add_submission(
            "Synthetic out-of-scope submission",
            ["Example Author"],
            "editor@example.org",
            MANUSCRIPT.encode(),
            "out-of-scope.txt",
        )
        reject_draft = workflow.decision(
            rejected["id"],
            "desk_reject",
            "This synthetic fixture is outside the example journal's scope.",
        )
        simulate_send(reject_draft)
        workflow.editor_decision(rejected["id"], "reject", "Synthetic editor rejection")
        result = run(workflow)
        return {
            "demo": True,
            "network_calls": 0,
            "real_emails_sent": 0,
            "home": str(config.home),
            "assessment": assessment,
            "submissions": store.all("SELECT id,title,status,round FROM submissions"),
            "simulated_messages": transcript,
            "tasks": result["tasks"],
            "note": "All people, manuscripts, reports, and deliveries are synthetic. Config remains offline/file.",
        }
    finally:
        store.close()
