"""Editorial workflow. External input never directly changes a decision or sends mail."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from . import documents, llm
from .config import AEError, address
from .store import days_after, now, uid

ACTIVE = {"received", "assessed", "reviewer_selection", "under_review", "decision_pending"}


class Workflow:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def submission(self, sid):
        return self.store.get("submissions", sid)

    def document(self, sid):
        sub = self.submission(sid)
        doc = self.store.one(
            "SELECT * FROM documents WHERE submission_id=? AND round=?", (sid, sub["round"])
        )
        if not doc:
            raise AEError("No manuscript attached")
        return doc

    def add_submission(self, title, authors, editor, data, filename, source=None, access_link=""):
        editor = address(editor)
        if editor not in self.config["ae"]["editors"]:
            raise AEError("Editor is not in ae.editors in config.toml")
        if source:
            existing = self.store.one(
                "SELECT * FROM submissions WHERE source_message_id=?", (source,)
            )
            if existing:
                return existing
        if not title.strip():
            raise AEError("Submission title is required")
        self.validate_link(access_link)
        text, warnings = documents.extract(data, filename)
        sid, did = uid("sub"), uid("doc")
        path, digest = documents.persist(self.config.home, did, data, filename)
        try:
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO submissions(id,title,authors,editor,created_at,updated_at,source_message_id,access_link) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        title.strip(),
                        json.dumps(authors),
                        editor,
                        now(),
                        now(),
                        source,
                        access_link,
                    ),
                )
                self.store.db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
                    (did, sid, 1, Path(filename).name, path, text, digest, json.dumps(warnings)),
                )
                self.store.audit(
                    "submission_imported", sid, {"document_sha256": digest, "source": source}
                )
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
        return self.submission(sid)

    @staticmethod
    def validate_link(link):
        if link:
            parsed = urlparse(link)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise AEError("Manuscript access link must be an HTTPS journal portal URL")
            if any(c.isspace() for c in link):
                raise AEError("Manuscript access link cannot contain whitespace")

    def set_metadata(self, sid, authors=None, access_link=None, title=None):
        with self.store.transaction():
            sub = self.submission(sid)
            if sub["status"] not in {"received", "assessed", "reviewer_selection"}:
                raise AEError("Metadata must be settled before invitations are sent")
            if self.store.one(
                "SELECT id FROM drafts WHERE submission_id=? AND status IN ('sending','uncertain')",
                (sid,),
            ):
                raise AEError("Reconcile unresolved email delivery before editing metadata")
            if title is not None:
                if not title.strip():
                    raise AEError("Submission title cannot be empty")
                self.store.db.execute(
                    "UPDATE submissions SET title=?,updated_at=? WHERE id=?",
                    (title.strip(), now(), sid),
                )
            if authors is not None:
                # Changed authors invalidate conflict checks and pending invitations.
                self.store.db.execute(
                    "UPDATE submissions SET authors=?,updated_at=? WHERE id=?",
                    (json.dumps(authors), now(), sid),
                )
                self.store.db.execute(
                    "UPDATE candidates SET conflict='unchecked',conflict_note='' WHERE submission_id=?",
                    (sid,),
                )
            if access_link is not None:
                self.validate_link(access_link)
                self.store.db.execute(
                    "UPDATE submissions SET access_link=?,updated_at=? WHERE id=?",
                    (access_link, now(), sid),
                )
            self.store.db.execute(
                "UPDATE drafts SET status='cancelled' WHERE submission_id=? "
                "AND kind='invitation' AND status IN ('draft','approved')",
                (sid,),
            )
            self.store.audit("metadata_updated", sid)

    def assess(self, sid, kind="triage"):
        sub, doc = self.submission(sid), self.document(sid)
        allowed = (
            {"received", "assessed"} if kind == "triage" else {"under_review", "decision_pending"}
        )
        if sub["status"] not in allowed:
            raise AEError(f"Cannot {kind} in stage {sub['status']}")
        reports = []
        if kind == "synthesis":
            assignments = self.store.all(
                "SELECT * FROM assignments WHERE submission_id=? AND round=?", (sid, sub["round"])
            )
            reports = [
                {"report": a["report"], "recommendation": a["recommendation"]}
                for a in assignments
                if a["status"] == "completed"
            ]
            if len(reports) < self.config["workflow"]["target_reviews"]:
                raise AEError("Not enough completed reports for synthesis")
            if any(a["status"] in {"draft", "invited", "accepted"} for a in assignments):
                raise AEError("Resolve outstanding reviewer assignments before synthesis")
        result = llm.assess(self.config, sub, doc, kind, reports)
        aid = uid("eval")
        with self.store.transaction():
            latest = self.submission(sid)
            if latest["round"] != sub["round"] or latest["status"] != sub["status"]:
                raise AEError("Submission changed during assessment; run it again")
            self.store.db.execute(
                "INSERT INTO assessments VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    sid,
                    sub["round"],
                    kind,
                    self.config["llm"]["provider"],
                    self.config["llm"]["model"],
                    doc["sha256"],
                    json.dumps(result),
                    now(),
                ),
            )
            self.store.status(sid, "assessed" if kind == "triage" else "decision_pending")
            self.store.audit(
                "assessment_recorded",
                sid,
                {"assessment": aid, "recommendation": result["recommendation"]},
            )
        return {"id": aid, **result}

    def route_review(self, sid):
        with self.store.transaction():
            sub = self.submission(sid)
            if sub["status"] not in {
                "received",
                "assessed",
                "reviewer_selection",
                "decision_pending",
            }:
                raise AEError(f"Cannot send for review in stage {sub['status']}")
            if self.store.one(
                "SELECT id FROM drafts WHERE submission_id=? AND kind='decision' AND status IN ('sending','uncertain')",
                (sid,),
            ):
                raise AEError("Reconcile unresolved decision delivery before changing route")
            self.store.db.execute(
                "UPDATE drafts SET status='cancelled' WHERE submission_id=? AND kind='decision' AND status IN ('draft','approved')",
                (sid,),
            )
            self.store.status(sid, "reviewer_selection")
        return self.submission(sid)

    def add_candidate(
        self, sid, name, email="", affiliation="", source="manual", evidence="", score=0
    ):
        self.submission(sid)
        if email:
            email = address(email)
        if not name.strip():
            raise AEError("Reviewer name is required")
        existing = self.store.one(
            "SELECT * FROM candidates WHERE submission_id=? AND source=? AND name=?",
            (sid, source, name),
        )
        if existing:
            return existing
        cid = uid("rev")
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO candidates(id,submission_id,name,email,affiliation,source,evidence,score) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (cid, sid, name, email, affiliation, source, evidence, score),
            )
            self.store.audit("candidate_added", cid, {"source": source})
        return self.store.get("candidates", cid)

    def check_candidate(self, cid, conflict, note, email=None):
        if conflict not in {"clear", "blocked", "unchecked"}:
            raise AEError("Invalid conflict status")
        if not note.strip():
            raise AEError("Record the evidence for the conflict check")
        with self.store.transaction():
            candidate = self.store.get("candidates", cid)
            if self.store.one(
                "SELECT d.id FROM drafts d JOIN assignments a ON a.id=d.assignment_id "
                "WHERE a.candidate_id=? AND d.status IN ('sending','uncertain')",
                (cid,),
            ):
                raise AEError("Reconcile unresolved email delivery before editing reviewer details")
            checked_email = (
                address(email or candidate["email"]) if conflict == "clear" else candidate["email"]
            )
            authors = json.loads(self.submission(candidate["submission_id"])["authors"])
            if conflict == "clear" and not authors:
                raise AEError("Record manuscript authors before clearing conflicts")
            if conflict == "clear" and candidate["name"].casefold().strip() in {
                a.casefold().strip() for a in authors
            }:
                raise AEError("A manuscript author cannot review their own submission")
            if conflict == "clear" and checked_email in {
                self.config["ae"]["email"],
                *self.config["ae"]["editors"],
            }:
                raise AEError("AE/editor address cannot be used as a reviewer")
            self.store.db.execute(
                "UPDATE candidates SET conflict=?,conflict_note=?,email=? WHERE id=?",
                (conflict, note, checked_email, cid),
            )
            self.store.audit("candidate_checked", cid, {"conflict": conflict, "note": note})
        return self.store.get("candidates", cid)

    def _draft(
        self, sid, kind, recipient, subject, body, assignment=None, effect=None, reply_to=None
    ):
        sub = self.submission(sid)
        did = uid("mail")
        message_id = f"<{did}@{self.config['ae']['email'].split('@')[1]}>"
        self.store.db.execute(
            "INSERT INTO drafts(id,submission_id,round,assignment_id,kind,recipient,subject,body,"
            "message_id,reply_to_id,created_at,effect) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                did,
                sid,
                sub["round"],
                assignment,
                kind,
                address(recipient),
                subject,
                body,
                message_id,
                reply_to,
                now(),
                json.dumps(effect or {}),
            ),
        )
        self.store.audit("draft_created", did, {"kind": kind, "submission": sid})
        return self.store.get("drafts", did)

    def invite(self, cid):
        with self.store.transaction():
            candidate = self.store.get("candidates", cid)
            sid = candidate["submission_id"]
            sub = self.submission(sid)
            if sub["status"] not in {"reviewer_selection", "under_review"}:
                raise AEError("Use submissions route-review before inviting reviewers")
            if candidate["conflict"] != "clear" or not candidate["email"]:
                raise AEError("Verify an email address and clear conflicts before inviting")
            if not sub["access_link"]:
                raise AEError("Set a journal manuscript access link with submissions metadata")
            old = self.store.one(
                "SELECT * FROM assignments WHERE submission_id=? AND candidate_id=? AND round=?",
                (sid, cid, sub["round"]),
            )
            if old:
                existing = self.store.one(
                    "SELECT * FROM drafts WHERE assignment_id=? AND kind='invitation' "
                    "AND status!='cancelled'",
                    (old["id"],),
                )
                if existing:
                    return existing
                if old["status"] != "draft":
                    raise AEError("This reviewer already has an assignment in this round")
            duplicate = self.store.one(
                "SELECT a.id FROM assignments a JOIN candidates c ON c.id=a.candidate_id "
                "WHERE a.submission_id=? AND a.round=? AND c.email=? AND a.candidate_id!=? "
                "AND a.status!='cancelled'",
                (sid, sub["round"], candidate["email"], cid),
            )
            if duplicate:
                raise AEError("This email already has a reviewer assignment for this round")
            aid = old["id"] if old else uid("asg")
            if not old:
                self.store.db.execute(
                    "INSERT INTO assignments(id,submission_id,candidate_id,round) VALUES(?,?,?,?)",
                    (aid, sid, cid, sub["round"]),
                )
            body = (
                f"Dear {candidate['name']},\n\nWould you be willing to review “{sub['title']}” "
                f"for {self.config['ae']['journal']}? Please let me know within "
                f"{self.config['workflow']['invitation_days']} days whether you are available and "
                "whether you have any conflicts of interest.\n\n"
                f"The review would be due {self.config['workflow']['review_days']} days after acceptance. "
                f"The journal's manuscript access page is {sub['access_link']}\n\n"
                f"Thank you,\n{self.config['ae']['name']}"
            )
            return self._draft(
                sid,
                "invitation",
                candidate["email"],
                f"[{sid}] Review invitation: {sub['title']}",
                body,
                aid,
            )

    def response(self, aid, action, report=None, recommendation=None):
        with self.store.transaction():
            assignment = self.store.get("assignments", aid)
            sub = self.submission(assignment["submission_id"])
            if assignment["round"] != sub["round"] or sub["status"] not in {
                "reviewer_selection",
                "under_review",
            }:
                raise AEError("Assignment is not in an active review round")
            expected = {
                "accept": {"invited"},
                "decline": {"invited", "accepted"},
                "review": {"accepted"},
                "cancel": {"draft", "invited", "accepted"},
            }
            if action not in expected or assignment["status"] not in expected[action]:
                raise AEError(f"Cannot {action} an assignment in state {assignment['status']}")
            if self.store.one(
                "SELECT id FROM drafts WHERE assignment_id=? AND status IN ('sending','uncertain')",
                (aid,),
            ):
                raise AEError("Reconcile the unresolved outgoing message first")
            if action == "review":
                if (
                    not report
                    or not report.strip()
                    or recommendation not in {"accept", "revise", "reject", "unspecified"}
                ):
                    raise AEError("A report and valid recommendation are required")
            status = {
                "accept": "accepted",
                "decline": "declined",
                "review": "completed",
                "cancel": "cancelled",
            }[action]
            due = days_after(self.config["workflow"]["review_days"]) if action == "accept" else None
            self.store.db.execute(
                "UPDATE assignments SET status=?,due_at=?,report=?,recommendation=?,"
                "reminders=0,last_reminder_at=NULL WHERE id=?",
                (status, due, report, recommendation, aid),
            )
            self.store.db.execute(
                "UPDATE drafts SET status='cancelled' WHERE assignment_id=? AND status IN ('draft','approved')",
                (aid,),
            )
            self.store.audit("reviewer_" + action, aid)
        return self.store.get("assignments", aid)

    def decision(self, sid, verdict, rationale):
        if verdict not in {"desk_reject", "accept", "revise", "reject"}:
            raise AEError("Unknown recommendation")
        if not rationale.strip():
            raise AEError("A substantive rationale is required")
        with self.store.transaction():
            sub = self.submission(sid)
            if sub["status"] not in ACTIVE:
                raise AEError("Submission is not active")
            assignments = self.store.all(
                "SELECT * FROM assignments WHERE submission_id=? AND round=?", (sid, sub["round"])
            )
            if any(a["status"] in {"draft", "invited", "accepted"} for a in assignments):
                raise AEError("Resolve or cancel outstanding reviewer assignments first")
            if verdict == "desk_reject" and any(a["status"] == "completed" for a in assignments):
                raise AEError("Use reject for a manuscript that has completed peer review")
            if verdict in {"accept", "revise", "reject"} and not any(
                a["status"] == "completed" for a in assignments
            ):
                raise AEError("A post-review recommendation requires at least one completed report")
            if self.store.one(
                "SELECT id FROM drafts WHERE submission_id=? AND kind='decision' "
                "AND status IN ('sending','uncertain')",
                (sid,),
            ):
                raise AEError("Reconcile the unresolved decision email first")
            self.store.db.execute(
                "UPDATE drafts SET status='cancelled' WHERE submission_id=? AND kind='decision' "
                "AND status IN ('draft','approved')",
                (sid,),
            )
            body = (
                f"Dear Editor,\n\nFor “{sub['title']}” (round {sub['round']}), my recommendation is "
                f"{verdict.replace('_', ' ')}.\n\n{rationale.strip()}\n\nBest,\n{self.config['ae']['name']}"
            )
            draft = self._draft(
                sid,
                "decision",
                sub["editor"],
                f"[{sid}] AE recommendation: {sub['title']}",
                body,
                effect={"verdict": verdict},
                reply_to=sub["source_message_id"],
            )
            self.store.status(sid, "decision_pending")
            return draft

    def editor_decision(self, sid, decision, note):
        if decision not in {"accept", "reject", "revise"} or not note.strip():
            raise AEError("Record accept, reject, or revise and the editor's decision evidence")
        with self.store.transaction():
            sub = self.submission(sid)
            if sub["status"] != "decision_sent":
                raise AEError("Record the editor decision after sending the AE recommendation")
            self.store.status(sid, "awaiting_revision" if decision == "revise" else "closed")
            self.store.audit("editor_decision", sid, {"decision": decision, "note": note})
        return self.submission(sid)

    def revise(self, sid, data, filename):
        text, warnings = documents.extract(data, filename)
        did = uid("doc")
        path, digest = documents.persist(self.config.home, did, data, filename)
        try:
            with self.store.transaction():
                sub = self.submission(sid)
                if sub["status"] != "awaiting_revision":
                    raise AEError("A revised manuscript requires an editor request for revision")
                revision = sub["round"] + 1
                self.store.db.execute(
                    "UPDATE submissions SET round=?,status='received',updated_at=? WHERE id=?",
                    (revision, now(), sid),
                )
                self.store.db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
                    (
                        did,
                        sid,
                        revision,
                        Path(filename).name,
                        path,
                        text,
                        digest,
                        json.dumps(warnings),
                    ),
                )
                self.store.db.execute(
                    "UPDATE drafts SET status='cancelled' WHERE submission_id=? AND status IN ('draft','approved')",
                    (sid,),
                )
                self.store.db.execute(
                    "UPDATE candidates SET conflict='unchecked',conflict_note='' WHERE submission_id=?",
                    (sid,),
                )
                self.store.audit(
                    "revision_received", sid, {"round": revision, "document_sha256": digest}
                )
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
        return self.submission(sid)

    def reminders(self):
        results = []
        with self.store.transaction():
            assignments = self.store.all(
                "SELECT a.* FROM assignments a JOIN submissions s ON s.id=a.submission_id "
                "WHERE a.round=s.round AND s.status IN ('reviewer_selection','under_review') "
                "AND a.status IN ('invited','accepted') AND a.due_at<=?",
                (now(),),
            )
            for a in assignments:
                if a["reminders"] >= self.config["workflow"]["max_reminders"]:
                    continue
                if (
                    a["last_reminder_at"]
                    and days_after(self.config["workflow"]["reminder_days"], a["last_reminder_at"])
                    > now()
                ):
                    continue
                if self.store.one(
                    "SELECT id FROM drafts WHERE assignment_id=? AND kind='reminder' "
                    "AND status IN ('draft','approved','sending','uncertain')",
                    (a["id"],),
                ):
                    continue
                candidate = self.store.get("candidates", a["candidate_id"])
                if candidate["conflict"] != "clear":
                    continue
                sub = self.submission(a["submission_id"])
                request = (
                    "your response to the review invitation"
                    if a["status"] == "invited"
                    else "your referee report"
                )
                body = (
                    f"Dear {candidate['name']},\n\nI am following up on {request} for “{sub['title']}”. "
                    f"The requested date was {a['due_at'][:10]}. Please let me know if you need more time "
                    f"or are no longer available.\n\nThank you,\n{self.config['ae']['name']}"
                )
                invitation = self.store.one(
                    "SELECT message_id FROM drafts WHERE assignment_id=? AND kind='invitation' AND status='sent'",
                    (a["id"],),
                )
                results.append(
                    self._draft(
                        sub["id"],
                        "reminder",
                        candidate["email"],
                        f"[{sub['id']}] Review follow-up: {sub['title']}",
                        body,
                        a["id"],
                        {"assignment_status": a["status"]},
                        invitation["message_id"] if invitation else None,
                    )
                )
        if self.config["workflow"]["auto_approve_reminders"]:
            for draft in results:
                self.approve(draft["id"])
        return results

    def draft_hash(self, draft):
        fields = {
            key: draft[key]
            for key in (
                "recipient",
                "subject",
                "body",
                "effect",
                "round",
                "message_id",
                "reply_to_id",
            )
        }
        fields["sender"] = self.config["ae"]["email"]
        fields["sender_name"] = self.config["ae"]["name"]
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

    def validate_draft(self, draft):
        sub = self.submission(draft["submission_id"])
        if sub["round"] != draft["round"] or sub["status"] not in ACTIVE:
            raise AEError("Draft no longer belongs to an active submission round")
        if draft["kind"] == "decision":
            if (
                draft["recipient"] != sub["editor"]
                or sub["editor"] not in self.config["ae"]["editors"]
            ):
                raise AEError("Decision recipient no longer matches the configured editor")
            if sub["status"] != "decision_pending":
                raise AEError("Submission is not awaiting a decision")
        elif draft["assignment_id"]:
            a = self.store.get("assignments", draft["assignment_id"])
            c = self.store.get("candidates", a["candidate_id"])
            if c["conflict"] != "clear" or c["email"] != draft["recipient"]:
                raise AEError("Reviewer address or conflict clearance has changed")
            expected = (
                "draft"
                if draft["kind"] == "invitation"
                else json.loads(draft["effect"])["assignment_status"]
            )
            if a["status"] != expected:
                raise AEError("Reviewer status has changed; this draft is stale")
            if sub["status"] not in {"reviewer_selection", "under_review"}:
                raise AEError("Submission is no longer seeking referee reports")

    def approve(self, did):
        with self.store.transaction():
            draft = self.store.get("drafts", did)
            if draft["status"] not in {"draft", "approved"}:
                raise AEError("Only an unsent draft can be approved")
            self.validate_draft(draft)
            self.store.db.execute(
                "UPDATE drafts SET status='approved',approved_hash=? WHERE id=?",
                (self.draft_hash(draft), did),
            )
            self.store.audit("draft_approved", did)
        return self.store.get("drafts", did)

    def edit_draft(self, did, body):
        if not body.strip():
            raise AEError("Email body cannot be empty")
        with self.store.transaction():
            draft = self.store.get("drafts", did)
            if draft["status"] not in {"draft", "approved"}:
                raise AEError("Cannot edit a sent, cancelled, or unresolved message")
            self.store.db.execute(
                "UPDATE drafts SET body=?,status='draft',approved_hash=NULL WHERE id=?", (body, did)
            )
            self.store.audit("draft_edited", did)
        return self.store.get("drafts", did)

    def sent_effects(self, draft):
        """Must run inside the transaction recording confirmed delivery."""
        aid = draft["assignment_id"]
        if draft["kind"] == "invitation":
            self.store.db.execute(
                "UPDATE assignments SET status='invited',due_at=? WHERE id=?",
                (days_after(self.config["workflow"]["invitation_days"]), aid),
            )
            self.store.status(draft["submission_id"], "under_review")
        elif draft["kind"] == "reminder":
            self.store.db.execute(
                "UPDATE assignments SET reminders=reminders+1,last_reminder_at=? WHERE id=?",
                (now(), aid),
            )
        elif draft["kind"] == "decision":
            self.store.status(draft["submission_id"], "decision_sent")

    def tasks(self):
        tasks = []
        for sub in self.store.all(
            "SELECT * FROM submissions WHERE status!='closed' ORDER BY created_at"
        ):
            stage = sub["status"]
            action = {
                "received": "Assess manuscript",
                "assessed": "Review assessment and choose a route",
                "reviewer_selection": "Find and invite reviewers",
                "under_review": "Track referee reports",
                "decision_pending": "Review and send editor recommendation",
                "decision_sent": "Record editor decision",
                "awaiting_revision": "Await revised manuscript",
            }[stage]
            tasks.append({"submission": sub["id"], "title": sub["title"], "action": action})
        for row in self.store.all(
            "SELECT * FROM assignments WHERE status IN ('invited','accepted') AND due_at<=?",
            (now(),),
        ):
            tasks.append(
                {
                    "submission": row["submission_id"],
                    "assignment": row["id"],
                    "action": "Overdue: escalate to AE"
                    if row["reminders"] >= self.config["workflow"]["max_reminders"]
                    else "Overdue: reminder due",
                    "due": row["due_at"],
                }
            )
        for row in self.store.all(
            "SELECT id,submission_id,status FROM drafts WHERE status IN ('draft','approved','sending','uncertain')"
        ):
            tasks.append(
                {
                    "submission": row["submission_id"],
                    "draft": row["id"],
                    "action": {
                        "draft": "Approve or edit email",
                        "approved": "Send approved email",
                        "sending": "Reconcile interrupted delivery",
                        "uncertain": "Reconcile uncertain delivery",
                    }[row["status"]],
                }
            )
        for row in self.store.all(
            "SELECT id,submission_id,note FROM messages WHERE status='pending'"
        ):
            tasks.append(
                {
                    "submission": row["submission_id"],
                    "message": row["id"],
                    "action": "Process inbound message",
                    "note": row["note"],
                }
            )
        return tasks
