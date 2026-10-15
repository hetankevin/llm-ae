"""One repeatable work cycle. Scheduling remains under the AE's control."""

import json

from . import mail
from .config import AEError


def run(workflow, sync=False, analyze=False, send=False):
    result = {"intake": [], "assessments": [], "drafts": [], "deliveries": [], "errors": []}
    if sync:
        try:
            result["intake"] = mail.sync(workflow)
        except (AEError, OSError) as exc:
            result["errors"].append({"step": "intake", "error": str(exc)})
    if analyze:
        for sub in workflow.store.all(
            "SELECT * FROM submissions WHERE status IN ('received','under_review')"
        ):
            try:
                if sub["status"] == "under_review":
                    assignments = workflow.store.all(
                        "SELECT * FROM assignments WHERE submission_id=? AND round=?",
                        (sub["id"], sub["round"]),
                    )
                    if any(a["status"] in {"draft", "invited", "accepted"} for a in assignments):
                        continue
                    if (
                        sum(a["status"] == "completed" for a in assignments)
                        < workflow.config["workflow"]["target_reviews"]
                    ):
                        continue
                assessment = workflow.assess(
                    sub["id"], "triage" if sub["status"] == "received" else "synthesis"
                )
                result["assessments"].append({"submission": sub["id"], **assessment})
                # Recommendations become drafts; routing to review requires the AE's action.
                if assessment["recommendation"] in {"desk_reject", "accept", "revise", "reject"}:
                    draft = workflow.decision(
                        sub["id"], assessment["recommendation"], assessment["editor_note"]
                    )
                    result["drafts"].append(draft)
            except AEError as exc:
                result["errors"].append({"submission": sub["id"], "error": str(exc)})
    result["drafts"].extend(workflow.reminders())
    if send:
        for draft in workflow.store.all(
            "SELECT id FROM drafts WHERE status='approved' ORDER BY created_at,id"
        ):
            try:
                result["deliveries"].append(mail.deliver(workflow, draft["id"]))
            except (AEError, OSError) as exc:
                result["errors"].append({"draft": draft["id"], "error": str(exc)})
    result["tasks"] = workflow.tasks()
    workflow.store.audit(
        "run_completed", "runner", {"errors": len(result["errors"]), "tasks": len(result["tasks"])}
    )
    return result


def dossier(workflow, sid):
    result = {"submission": workflow.submission(sid)}
    for table in ("documents", "assessments", "candidates", "assignments", "drafts", "messages"):
        result[table] = workflow.store.all(f"SELECT * FROM {table} WHERE submission_id=?", (sid,))
    for doc in result["documents"]:
        doc.pop("text")
    for assessment in result["assessments"]:
        assessment["result"] = json.loads(assessment["result"])
    result["submission"]["authors"] = json.loads(result["submission"]["authors"])
    return result
