"""Small, scriptable command surface. All commands can emit JSON."""

import argparse
import fcntl
import imaplib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from . import __version__, accounts, discovery, documents, mail, runner
from .config import AEError, Config, initialize
from .store import Store, days_after
from .workflow import Workflow


def parser():
    root = argparse.ArgumentParser(
        prog="llm-ae", description="A local assistant for associate editors"
    )
    root.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("LLM_AE_HOME", "~/.llm-ae")),
        help="private runtime directory (default: ~/.llm-ae)",
    )
    root.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="guided account login and email connection")
    setup.add_argument("--device-auth", action="store_true", help="OpenAI device login")
    commands.add_parser("init", help="initialize local state and configuration")
    commands.add_parser("doctor", help="check local configuration without contacting services")
    commands.add_parser("status", help="list submissions and next actions")
    commands.add_parser("tasks", help="list AE actions and deadlines")
    auth = commands.add_parser(
        "auth", help="sign in with a ChatGPT or Claude account"
    ).add_subparsers(dest="action", required=True)
    for name in ("login", "use"):
        command = auth.add_parser(
            name,
            help="sign in and select an account"
            if name == "login"
            else "select an already signed-in account",
        )
        command.add_argument("provider", choices=list(accounts.ALIASES))
        command.add_argument(
            "--model",
            default="",
            help="optional model override; default is chosen by the official CLI",
        )
        command.add_argument(
            "--allow-remote-manuscripts",
            action="store_true",
            help="enable remote assessment under your journal's policy",
        )
        if name == "login":
            command.add_argument(
                "--device-auth",
                action="store_true",
                help="OpenAI device login on a headless machine",
            )
    status = auth.add_parser(
        "status", help="check official CLI sign-ins without showing credentials"
    )
    status.add_argument("provider", choices=list(accounts.ALIASES), nargs="?")
    commands.add_parser(
        "demo", help="run a synthetic lifecycle in a NEW --home directory; no network"
    )
    audit = commands.add_parser("audit", help="read the local audit log")
    audit.add_argument("--limit", type=int, default=50)
    run = commands.add_parser("run", help="run one work cycle, suitable for a scheduler")
    run.add_argument("--sync", action="store_true", help="fetch email from IMAP")
    run.add_argument(
        "--analyze", action="store_true", help="assess new submissions and completed review rounds"
    )
    run.add_argument(
        "--send-approved",
        action="store_true",
        help="deliver approved drafts using configured transport",
    )

    subs = commands.add_parser("submissions", help="import and manage manuscripts").add_subparsers(
        dest="action", required=True
    )
    subs.add_parser("list")
    add = subs.add_parser("add")
    add.add_argument("file", type=Path)
    add.add_argument("--title", required=True)
    add.add_argument("--author", action="append", default=[])
    add.add_argument("--editor", required=True)
    add.add_argument("--access-link", default="")
    for name in ("show", "assess", "synthesize", "route-review", "export"):
        cmd = subs.add_parser(name)
        cmd.add_argument("id")
    meta = subs.add_parser("metadata")
    meta.add_argument("id")
    meta.add_argument("--author", action="append")
    meta.add_argument("--access-link")
    meta.add_argument("--title")
    revise = subs.add_parser("revise")
    revise.add_argument("id")
    revise.add_argument("file", type=Path)
    decision = subs.add_parser("recommend")
    decision.add_argument("id")
    decision.add_argument("verdict", choices=["desk_reject", "accept", "revise", "reject"])
    decision.add_argument("--rationale", type=Path, required=True, help="UTF-8 rationale file")
    ed = subs.add_parser("editor-decision")
    ed.add_argument("id")
    ed.add_argument("decision", choices=["accept", "reject", "revise"])
    ed.add_argument("--note", required=True)

    inbox = commands.add_parser(
        "inbox", help="import, sync, and process editorial mail"
    ).add_subparsers(dest="action", required=True)
    inbox.add_parser("sync")
    inbox.add_parser("list")
    imp = inbox.add_parser("import")
    imp.add_argument("file", type=Path)
    show = inbox.add_parser("show")
    show.add_argument("id")
    apply = inbox.add_parser("apply")
    apply.add_argument("id")
    apply.add_argument("response", choices=["accept", "decline", "review", "dismiss", "link"])
    apply.add_argument("--note", default="")
    apply.add_argument("--submission")
    apply.add_argument("--report", type=Path)
    apply.add_argument(
        "--recommendation",
        choices=["accept", "revise", "reject", "unspecified"],
        default="unspecified",
    )

    contacts = commands.add_parser(
        "contacts", help="build a local contact index from a selected mailbox"
    ).add_subparsers(dest="action", required=True)
    contacts.add_parser("list")
    scan = contacts.add_parser("scan")
    scan.add_argument(
        "--folder",
        required=True,
        help="usually your Sent folder; the scan is bounded by mail limits",
    )

    reviewers = commands.add_parser(
        "reviewers", help="discover, vet, and invite reviewers"
    ).add_subparsers(dest="action", required=True)
    listing = reviewers.add_parser("list")
    listing.add_argument("submission")
    add = reviewers.add_parser("add")
    add.add_argument("submission")
    add.add_argument("--name", required=True)
    add.add_argument("--email", default="")
    add.add_argument("--affiliation", default="")
    add.add_argument("--evidence", required=True)
    search = reviewers.add_parser("find")
    search.add_argument("submission")
    search.add_argument(
        "--source", choices=["contacts", "arxiv", "openalex", "scholar"], required=True
    )
    search.add_argument(
        "--query", required=True, help="public research terms; sent to the selected search service"
    )
    search.add_argument("--limit", type=int, default=10)
    check = reviewers.add_parser("check")
    check.add_argument("id")
    check.add_argument("conflict", choices=["clear", "blocked", "unchecked"])
    check.add_argument(
        "--note",
        required=True,
        help="evidence: identity, coauthorship, affiliation, other conflicts",
    )
    check.add_argument("--email")
    invite = reviewers.add_parser("invite")
    invite.add_argument("id")

    assignments = commands.add_parser(
        "assignments", help="record responses, reports, and deadline changes"
    ).add_subparsers(dest="action", required=True)
    listing = assignments.add_parser("list")
    listing.add_argument("--submission")
    for name in ("accept", "decline", "cancel"):
        cmd = assignments.add_parser(name)
        cmd.add_argument("id")
    report = assignments.add_parser("report")
    report.add_argument("id")
    report.add_argument("file", type=Path)
    report.add_argument(
        "--recommendation", choices=["accept", "revise", "reject", "unspecified"], required=True
    )
    extend = assignments.add_parser("extend")
    extend.add_argument("id")
    extend.add_argument(
        "--days", type=int, required=True, help="new due date, this many days from today"
    )
    extend.add_argument("--note", required=True)

    outbox = commands.add_parser(
        "outbox", help="inspect, approve, and deliver exact messages"
    ).add_subparsers(dest="action", required=True)
    outbox.add_parser("list")
    for name in ("show", "approve", "send", "cancel"):
        cmd = outbox.add_parser(name)
        cmd.add_argument("id")
    edit = outbox.add_parser("edit")
    edit.add_argument("id")
    edit.add_argument("--body", type=Path, required=True)
    reconcile = outbox.add_parser("reconcile")
    reconcile.add_argument("id")
    reconcile.add_argument("outcome", choices=["sent", "not-sent"])
    reconcile.add_argument("--note", required=True, help="evidence from the mailbox or server logs")
    return root


@contextmanager
def locked(home):
    """Serialize CLI processes, including intake and SMTP recovery operations."""
    home = home.expanduser().resolve()
    if not home.is_dir():
        raise AEError("Run llm-ae init first")
    with (home / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AEError(
                "Another llm-ae process is using this home; try again after it finishes"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def text_file(path):
    return documents.read_file(path).decode("utf-8")


def dispatch(args, w):
    s = w.store
    command, action = args.command, getattr(args, "action", "")
    if command == "auth":
        if action == "status":
            return (
                accounts.status(args.provider)
                if args.provider
                else [accounts.status(p) for p in ("openai", "anthropic")]
            )
        result = accounts.select(
            w.config,
            args.provider,
            login=action == "login",
            device_auth=getattr(args, "device_auth", False),
            model=args.model,
            allow_remote=args.allow_remote_manuscripts,
        )
        s.audit(
            "model_account_selected",
            result["provider"],
            {"remote_manuscripts_allowed": result["remote_manuscripts_allowed"]},
        )
        return result
    if command == "doctor":
        return {
            "home": str(w.config.home),
            "database_version": 1,
            "mail_transport": w.config["mail"]["transport"],
            "llm_provider": w.config["llm"]["provider"],
            "account_clis_installed": {
                name: bool(shutil.which(name)) for name in ("codex", "claude")
            },
            "model": w.config["llm"]["model"],
            "remote_manuscripts_allowed": w.config["llm"]["allow_remote_manuscripts"],
            "pdf_support": importlib.util.find_spec("pypdf") is not None,
            "imap_configured": bool(w.config["mail"]["imap_host"]),
            "smtp_configured": bool(w.config["mail"]["smtp_host"]),
            "mail_keychain_configured": bool(w.config["mail"]["password_keyring"]),
            "credentials_present": {
                key: bool(os.environ.get(w.config[section][key], ""))
                for section, key in [
                    ("mail", "password_env"),
                    ("mail", "oauth_token_env"),
                    ("llm", "api_key_env"),
                    ("discovery", "openalex_key_env"),
                ]
            },
            "note": "No network checks performed. Secrets are never displayed.",
        }
    if command == "status":
        return {
            "submissions": s.all(
                "SELECT id,title,status,round FROM submissions ORDER BY created_at"
            ),
            "tasks": w.tasks(),
        }
    if command == "tasks":
        return w.tasks()
    if command == "audit":
        return s.all(
            "SELECT * FROM audit ORDER BY sequence DESC LIMIT ?", (max(1, min(args.limit, 1000)),)
        )
    if command == "run":
        return runner.run(w, args.sync, args.analyze, args.send_approved)
    if command == "submissions":
        if action == "list":
            return s.all("SELECT id,title,status,round,editor FROM submissions ORDER BY created_at")
        if action == "add":
            return w.add_submission(
                args.title,
                args.author,
                args.editor,
                documents.read_file(args.file),
                args.file.name,
                access_link=args.access_link,
            )
        if action in {"show", "export"}:
            return runner.dossier(w, args.id)
        if action in {"assess", "synthesize"}:
            return w.assess(args.id, "triage" if action == "assess" else "synthesis")
        if action == "route-review":
            return w.route_review(args.id)
        if action == "metadata":
            w.set_metadata(args.id, args.author, args.access_link, args.title)
            return w.submission(args.id)
        if action == "revise":
            return w.revise(args.id, documents.read_file(args.file), args.file.name)
        if action == "recommend":
            return w.decision(args.id, args.verdict, text_file(args.rationale))
        if action == "editor-decision":
            return w.editor_decision(args.id, args.decision, args.note)
    if command == "inbox":
        if action == "list":
            return s.all(
                "SELECT id,sender,subject,submission_id,assignment_id,status,note FROM messages WHERE status='pending'"
            )
        if action == "show":
            return s.get("messages", args.id)
        if action == "import":
            with args.file.open("rb") as stream:
                return mail.ingest(w, stream.read(w.config["mail"]["max_message_bytes"] + 1))
        if action == "sync":
            return mail.sync(w)
        if action == "apply":
            return mail.process_inbound(
                w,
                args.id,
                args.response,
                args.note,
                text_file(args.report) if args.report else None,
                args.recommendation,
                args.submission,
            )
    if command == "contacts":
        return (
            s.all("SELECT * FROM contacts")
            if action == "list"
            else mail.scan_contacts(w, args.folder)
        )
    if command == "reviewers":
        if action == "list":
            return s.all(
                "SELECT * FROM candidates WHERE submission_id=? ORDER BY score DESC",
                (args.submission,),
            )
        if action == "add":
            return w.add_candidate(
                args.submission, args.name, args.email, args.affiliation, evidence=args.evidence
            )
        if action == "find":
            return discovery.discover(w, args.submission, args.source, args.query, args.limit)
        if action == "check":
            return w.check_candidate(args.id, args.conflict, args.note, args.email)
        if action == "invite":
            return w.invite(args.id)
    if command == "assignments":
        if action == "list":
            return s.all(
                "SELECT * FROM assignments" + (" WHERE submission_id=?" if args.submission else ""),
                (args.submission,) if args.submission else (),
            )
        if action == "report":
            return w.response(args.id, "review", text_file(args.file), args.recommendation)
        if action in {"accept", "decline", "cancel"}:
            return w.response(args.id, action)
        if action == "extend":
            if args.days < 1 or not args.note.strip():
                raise AEError("A positive number of days and an explanatory note are required")
            with s.transaction():
                a = s.get("assignments", args.id)
                sub = w.submission(a["submission_id"])
                if a["status"] not in {"invited", "accepted"} or a["round"] != sub["round"]:
                    raise AEError("Only active assignments can be extended")
                if s.one(
                    "SELECT id FROM drafts WHERE assignment_id=? AND status IN ('sending','uncertain')",
                    (args.id,),
                ):
                    raise AEError("Reconcile unresolved delivery first")
                s.db.execute(
                    "UPDATE assignments SET due_at=?,reminders=0,last_reminder_at=NULL WHERE id=?",
                    (days_after(args.days), args.id),
                )
                s.db.execute(
                    "UPDATE drafts SET status='cancelled' WHERE assignment_id=? AND kind='reminder' AND status IN ('draft','approved')",
                    (args.id,),
                )
                s.audit("deadline_extended", args.id, {"days": args.days, "note": args.note})
            return s.get("assignments", args.id)
    if command == "outbox":
        if action == "list":
            return s.all(
                "SELECT id,submission_id,kind,recipient,subject,status FROM drafts ORDER BY created_at"
            )
        if action == "show":
            return s.get("drafts", args.id)
        if action == "approve":
            return w.approve(args.id)
        if action == "edit":
            return w.edit_draft(args.id, text_file(args.body))
        if action == "send":
            return mail.deliver(w, args.id)
        if action == "reconcile":
            return mail.reconcile(w, args.id, args.outcome == "sent", args.note)
        if action == "cancel":
            with s.transaction():
                d = s.get("drafts", args.id)
                if d["status"] not in {"draft", "approved"}:
                    raise AEError("Only pending drafts can be cancelled")
                s.db.execute(
                    "UPDATE drafts SET status='cancelled',approved_hash=NULL WHERE id=?", (args.id,)
                )
                s.audit("draft_cancelled", args.id)
            return s.get("drafts", args.id)
    raise AEError("Unknown command")


def emit(result, as_json=False):
    # JSON is also the readable default for detailed records; compact tables for lists.
    if (
        isinstance(result, list)
        and result
        and not as_json
        and all(isinstance(x, dict) for x in result)
    ):
        preferred = (
            "id",
            "submission",
            "name",
            "provider",
            "account_ready",
            "kind",
            "title",
            "subject",
            "status",
            "conflict",
            "action",
            "due",
        )
        keys = [key for key in preferred if any(key in row for row in result)]
        if keys:
            print("\t".join(keys))
            for row in result:
                print(
                    "\t".join(
                        json.dumps(str(row.get(k, "")), ensure_ascii=True)[1:-1][:100] for k in keys
                    )
                )
            return
    print(json.dumps(result, ensure_ascii=True, indent=2))


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if args.command == "setup":
            from . import setup

            if args.json:
                raise AEError("Setup is interactive and does not support --json")
            args.home = args.home.expanduser().resolve()
            if not sys.stdin.isatty():
                raise AEError("Run llm-ae setup in an interactive terminal")
            args.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            with locked(args.home):
                setup.run(args.home, args.device_auth)
            return 0
        if args.command == "init":
            config = initialize(args.home)
            store = Store(config.home)
            store.close()
            emit(
                {
                    "home": str(config.home),
                    "config": str(config.home / "config.toml"),
                    "transport": "file",
                    "provider": "offline",
                },
                args.json,
            )
            return 0
        if args.command == "demo":
            from .demo import demo

            emit(demo(args.home), args.json)
            return 0
        with locked(args.home):
            config = Config(args.home)
            store = Store(config.home)
            try:
                result = dispatch(args, Workflow(config, store))
                emit(result, args.json)
                return 1 if args.command == "run" and result.get("errors") else 0
            finally:
                store.close()
    except (AEError, OSError, ValueError, sqlite3.Error, imaplib.IMAP4.error) as exc:
        error = (
            str(exc)
            if isinstance(exc, AEError)
            else f"{type(exc).__name__}: operation failed; check configuration and local files"
        )
        print(json.dumps({"error": error}) if args.json else f"Error: {error}", file=sys.stderr)
        return 1
    except EOFError:
        print("Setup paused. Run llm-ae setup to resume.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.command == "setup":
            print("\nSetup paused. Run llm-ae setup to resume.", file=sys.stderr)
            return 130
        print("Interrupted. Check outbox status before retrying any delivery.", file=sys.stderr)
        return 130
