"""IMAP intake and a durable SMTP outbox, with explicit uncertain-send recovery."""

import hashlib
import imaplib
import json
import re
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr, getaddresses, parseaddr

from .config import AEError, address
from .store import now, uid


def parsed_message(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=("plain",))
    text = body.get_content() if body else ""
    if not isinstance(text, str):
        text = ""
    return message, text


def ingest(workflow, raw):
    config, store = workflow.config, workflow.store
    if len(raw) > config["mail"]["max_message_bytes"]:
        raise AEError("Message exceeds mail.max_message_bytes")
    msg, body = parsed_message(raw)
    mid = (
        str(msg.get("Message-ID", "")).strip()
        or f"<sha256-{hashlib.sha256(raw).hexdigest()}@local>"
    )
    existing = store.one("SELECT * FROM messages WHERE message_id=?", (mid,))
    if existing:
        return {
            "status": "duplicate",
            "message": existing["id"],
            "submission": existing["submission_id"],
        }
    senders = getaddresses(msg.get_all("From", []))
    sender = address(senders[0][1]) if len(senders) == 1 else ""
    if not sender:
        raise AEError("Inbound message must have exactly one sender")
    subject = str(msg.get("Subject", "(no subject)"))
    references = re.findall(
        r"<[^<>\s]+>", str(msg.get("In-Reply-To", "")) + " " + str(msg.get("References", ""))
    )
    linked = []
    for reference in references:
        draft = store.one("SELECT * FROM drafts WHERE message_id=? AND status='sent'", (reference,))
        if draft and draft["recipient"] == sender:
            linked.append(draft)
    # Subject IDs are only hints, and only for an editor or assigned reviewer.
    sid_matches = re.findall(r"\bsub-[a-f0-9]{12}\b", subject)
    sid, aid, status, note = None, None, "pending", ""
    if linked:
        targets = {(d["submission_id"], d["assignment_id"]) for d in linked}
        if len(targets) == 1:
            sid, aid = targets.pop()
            note = "Threaded reply: inspect and apply the reviewer response or editor decision"
        else:
            note = "Ambiguous thread references; associate manually"
    elif len(set(sid_matches)) == 1:
        candidate_sub = store.one("SELECT * FROM submissions WHERE id=?", (sid_matches[0],))
        if candidate_sub:
            assignments = store.all(
                "SELECT a.id FROM assignments a JOIN candidates c ON c.id=a.candidate_id "
                "WHERE a.submission_id=? AND a.round=? AND c.email=? AND a.status IN ('invited','accepted')",
                (candidate_sub["id"], candidate_sub["round"], sender),
            )
            if sender == candidate_sub["editor"]:
                sid = candidate_sub["id"]
                note = "Editor correspondence: inspect before applying a decision or revision"
            elif len(assignments) == 1:
                sid, aid = candidate_sub["id"], assignments[0]["id"]
                note = "Reviewer reply matched by submission ID; inspect before applying"
    attachments = [
        p
        for p in msg.iter_attachments()
        if p.get_filename()
        and str(p.get_filename()).lower().endswith((".pdf", ".txt", ".md", ".tex"))
    ]
    is_reply = bool(
        msg.get("In-Reply-To") or msg.get("References") or re.match(r"(?i)^\s*re:", subject)
    )
    if (
        sid is None
        and not linked
        and not is_reply
        and sender in config["ae"]["editors"]
        and re.search(config["mail"]["submission_subject"], subject)
    ):
        if len(attachments) == 1:
            part = attachments[0]
            try:
                sub = workflow.add_submission(
                    subject,
                    [],
                    sender,
                    part.get_payload(decode=True) or b"",
                    str(part.get_filename()),
                    source=mid,
                )
                sid, status = sub["id"], "imported"
                note = "Confirm the title, authors, and portal link before inviting reviewers"
            except AEError as exc:
                note = str(exc)
        else:
            note = "No unique manuscript attachment; import the selected file manually and link this message"
    if sid is None and not note:
        known = sender in config["ae"]["editors"] or store.one(
            "SELECT id FROM candidates WHERE email=?", (sender,)
        )
        status = "pending" if known else "ignored"
        note = (
            "Unmatched editorial correspondence"
            if known
            else "Sender is not a configured editor or reviewer"
        )
    iid = uid("in")
    if status == "ignored":
        body = ""  # Do not retain unrelated inbox contents.
    with store.transaction():
        store.db.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
            (iid, mid, sender, subject, body, now(), sid, aid, status, note),
        )
        store.audit("message_ingested", iid, {"status": status, "submission": sid})
    return {"status": status, "message": iid, "submission": sid, "assignment": aid, "note": note}


def connect_imap(config):
    mail = config["mail"]
    if not mail["imap_host"] or not mail["username"]:
        raise AEError("Configure mail.imap_host and mail.username first")
    client = imaplib.IMAP4_SSL(
        mail["imap_host"], mail["imap_port"], ssl_context=ssl.create_default_context(), timeout=30
    )
    try:
        if mail["oauth_token_env"]:
            token = config.secret("mail", "oauth_token_env")
            client.authenticate(
                "XOAUTH2",
                lambda _: f"user={mail['username']}\x01auth=Bearer {token}\x01\x01".encode(),
            )
        else:
            client.login(mail["username"], config.secret("mail", "password_env"))
        return client
    except BaseException:
        client.logout()
        raise


def fetch_messages(config, folder=None, unseen_uids=None):
    """Fetch a bounded batch in UID order. The caller persists a UID cursor."""
    client = connect_imap(config)
    try:
        status, _ = client.select(folder or config["mail"]["imap_folder"], readonly=True)
        if status != "OK":
            raise AEError("Cannot select the configured IMAP folder")
        validity_response = client.response("UIDVALIDITY")[1]
        validity = (validity_response[0] or b"unknown").decode()
        since = (datetime.now(UTC) - timedelta(days=config["mail"]["lookback_days"])).strftime(
            "%d-%b-%Y"
        )
        status, data = client.uid("search", None, "SINCE", since)
        if status != "OK":
            raise AEError("IMAP search failed")
        cursor = int((unseen_uids or {}).get(validity, 0))
        ids = [x for x in data[0].split() if int(x) > cursor][: config["mail"]["max_messages"]]
        for message_uid in ids:
            status, size_data = client.uid("fetch", message_uid, "(RFC822.SIZE)")
            size_text = b" ".join(x for x in size_data if isinstance(x, bytes))
            size_match = re.search(rb"RFC822.SIZE (\d+)", size_text)
            if status != "OK" or not size_match:
                raise AEError("IMAP could not report message size; cursor retained for retry")
            if int(size_match[1]) > config["mail"]["max_message_bytes"]:
                yield validity, message_uid.decode(), None
                continue
            status, fetched = client.uid("fetch", message_uid, "(BODY.PEEK[])")
            raw_parts = [part[1] for part in fetched if isinstance(part, tuple)]
            if status != "OK" or len(raw_parts) != 1:
                raise AEError("IMAP fetch failed; cursor retained for retry")
            yield validity, message_uid.decode(), raw_parts[0]
    finally:
        client.logout()


def sync(workflow):
    cfg, store = workflow.config, workflow.store
    key = "imap:" + json.dumps([cfg["mail"][k] for k in ("imap_host", "username", "imap_folder")])
    saved = store.one("SELECT value FROM settings WHERE key=?", (key,))
    cursors = json.loads(saved["value"]) if saved else {}
    results = []
    for validity, message_uid, raw in fetch_messages(cfg, unseen_uids=cursors):
        if raw is None:
            result = {
                "status": "skipped",
                "uid": message_uid,
                "note": "Message exceeds size limit; inspect in mail client",
            }
        else:
            try:
                result = ingest(workflow, raw)
            except (AEError, ValueError) as exc:
                result = {"status": "skipped", "uid": message_uid, "note": str(exc)}
        results.append(result)
        cursors[validity] = int(message_uid)
        with store.transaction():
            store.db.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(cursors))
            )
            store.audit(
                "imap_message_processed",
                message_uid,
                {"uidvalidity": validity, "status": result["status"]},
            )
    return results


def scan_contacts(workflow, folder):
    """Explicitly scan a chosen folder. Retain addresses and subjects, not message bodies."""
    store, cfg = workflow.store, workflow.config
    key = "contacts-imap:" + json.dumps([cfg["mail"]["imap_host"], cfg["mail"]["username"], folder])
    saved = store.one("SELECT value FROM settings WHERE key=?", (key,))
    cursors = json.loads(saved["value"]) if saved else {}
    count = 0
    skipped = []
    for validity, message_uid, raw in fetch_messages(cfg, folder, unseen_uids=cursors):
        try:
            observed = _observe_contacts(workflow, raw, folder) if raw is not None else 0
            if raw is None:
                skipped.append(message_uid)
        except (AEError, ValueError):
            observed = 0
            skipped.append(message_uid)
        count += observed
        cursors[validity] = int(message_uid)
        store.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(cursors)))
    return {"contacts_observed": count, "skipped_uids": skipped}


def _observe_contacts(workflow, raw, folder):
    msg, _ = parsed_message(raw)
    mid = str(msg.get("Message-ID", "")) or hashlib.sha256(raw).hexdigest()
    count = 0
    with workflow.store.transaction():
        ae = workflow.config["ae"]["email"].lower()
        # Require direct participation by the AE, not arbitrary list traffic.
        sender = parseaddr(str(msg.get("From", "")))[1].lower()
        recipients = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
        if ae != sender and ae not in {email.lower() for _, email in recipients}:
            return 0
        people = recipients if sender == ae else getaddresses(msg.get_all("From", []))
        for name, email in people:
            try:
                email = address(email)
            except AEError:
                continue
            if email == ae or email in workflow.config["ae"]["editors"]:
                continue
            if workflow.store.one(
                "SELECT email FROM contact_observations WHERE email=? AND message_id=?",
                (email, mid),
            ):
                continue
            workflow.store.db.execute(
                "INSERT INTO contacts(email,name,topics,source) VALUES(?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET name=excluded.name,"
                "topics=substr(contacts.topics || ' ' || excluded.topics,-10000),"
                "interactions=contacts.interactions+1",
                (email, name or email, str(msg.get("Subject", ""))[:10000], f"email:{folder}"),
            )
            workflow.store.db.execute("INSERT INTO contact_observations VALUES(?,?)", (email, mid))
            workflow.store.audit("contact_observed", email, {"folder": folder})
            count += 1
    return count


def build_email(workflow, draft):
    msg = EmailMessage()
    msg["From"] = formataddr((workflow.config["ae"]["name"], workflow.config["ae"]["email"]))
    msg["To"] = draft["recipient"]
    msg["Subject"] = draft["subject"]
    msg["Message-ID"] = draft["message_id"]
    msg["Date"] = format_datetime(datetime.now(UTC))
    if draft["reply_to_id"]:
        msg["In-Reply-To"] = draft["reply_to_id"]
        msg["References"] = draft["reply_to_id"]
    msg.set_content(draft["body"])
    return msg


def smtp_send(config, msg):
    mail = config["mail"]
    if not mail["smtp_host"] or not mail["username"]:
        raise AEError("Configure mail.smtp_host and mail.username first")
    context = ssl.create_default_context()
    if mail["smtp_security"] == "ssl":
        client = smtplib.SMTP_SSL(mail["smtp_host"], mail["smtp_port"], context=context, timeout=30)
    else:
        client = smtplib.SMTP(mail["smtp_host"], mail["smtp_port"], timeout=30)
    try:
        client.ehlo()
        if mail["smtp_security"] == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if mail["oauth_token_env"]:
            token = config.secret("mail", "oauth_token_env")
            client.auth(
                "XOAUTH2",
                lambda challenge=None: f"user={mail['username']}\x01auth=Bearer {token}\x01\x01",
            )
        else:
            client.login(mail["username"], config.secret("mail", "password_env"))
        refused = client.send_message(
            msg, from_addr=config["ae"]["email"], to_addrs=[str(msg["To"])]
        )
        if refused:
            raise AEError("SMTP recipient was refused")
    finally:
        # A failed QUIT after DATA acceptance must not mislabel a successful send.
        client.close()


def deliver(workflow, did):
    store = workflow.store
    with store.transaction():
        draft = store.get("drafts", did)
        if draft["status"] != "approved":
            raise AEError(
                "Approve the exact draft before sending; unresolved sends require reconciliation"
            )
        workflow.validate_draft(draft)
        if draft["approved_hash"] != workflow.draft_hash(draft):
            raise AEError("Message content or sender changed after approval")
        msg = build_email(workflow, draft)
        if workflow.config["mail"]["transport"] == "file":
            path = workflow.config.home / "outbox" / f"{did}.eml"
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(msg.as_bytes(policy=policy.SMTP))
            path.chmod(0o600)
            store.audit("email_preview_exported", did)
            return {
                "status": "preview",
                "path": str(path),
                "note": "No email sent and no workflow stage advanced",
            }
        store.db.execute("UPDATE drafts SET status='sending',error=NULL WHERE id=?", (did,))
        store.audit("send_started", did)
    try:
        smtp_send(workflow.config, msg)
    except Exception as exc:
        with store.transaction():
            store.db.execute(
                "UPDATE drafts SET status='uncertain',error=? WHERE id=?", (type(exc).__name__, did)
            )
            store.audit("send_uncertain", did, {"error_type": type(exc).__name__})
        raise AEError(
            "SMTP delivery is unresolved. Check Sent mail/server logs by Message-ID, "
            "then use outbox reconcile; automatic retry is disabled."
        ) from exc
    with store.transaction():
        store.db.execute("UPDATE drafts SET status='sent',sent_at=? WHERE id=?", (now(), did))
        workflow.sent_effects(draft)
        store.audit("send_confirmed", did)
    return {"status": "sent", "draft": did, "message_id": draft["message_id"]}


def reconcile(workflow, did, sent, note):
    if not note.strip():
        raise AEError("Record evidence from the mailbox or server logs")
    with workflow.store.transaction():
        draft = workflow.store.get("drafts", did)
        if draft["status"] not in {"sending", "uncertain"}:
            raise AEError("Only interrupted or uncertain delivery can be reconciled")
        if sent:
            workflow.store.db.execute(
                "UPDATE drafts SET status='sent',sent_at=?,error=NULL WHERE id=?", (now(), did)
            )
            workflow.sent_effects(draft)
        else:
            workflow.store.db.execute(
                "UPDATE drafts SET status='draft',approved_hash=NULL,error=NULL WHERE id=?", (did,)
            )
        workflow.store.audit("send_reconciled", did, {"sent": sent, "note": note})
    return workflow.store.get("drafts", did)


def process_inbound(workflow, iid, action, note="", report=None, recommendation=None, sid=None):
    incoming = workflow.store.get("messages", iid)
    if incoming["status"] != "pending":
        raise AEError("Message is not pending")
    if action in {"accept", "decline", "review"}:
        if not incoming["assignment_id"]:
            raise AEError("Message has no unambiguous reviewer assignment")
        workflow.response(
            incoming["assignment_id"], action, report or incoming["body"], recommendation
        )
    elif action == "link":
        if not sid:
            raise AEError("Supply --submission for manual linking")
        sub = workflow.submission(sid)
        if incoming["sender"] != sub["editor"]:
            raise AEError("Only an editor's message can be manually linked to a submission")
    elif action != "dismiss":
        raise AEError("Unsupported inbound action")
    with workflow.store.transaction():
        workflow.store.db.execute(
            "UPDATE messages SET status='processed',note=?,submission_id=COALESCE(?,submission_id) WHERE id=?",
            (note or action, sid, iid),
        )
        workflow.store.audit("inbound_processed", iid, {"action": action, "note": note})
    return workflow.store.get("messages", iid)
