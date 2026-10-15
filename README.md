# LLM-AE

A local CLI assistant for associate editors. It turns incoming manuscripts into
tracked editorial cases, prepares assessments and correspondence, finds reviewer
leads, and follows review rounds through to the editor's decision.

The core has no third-party runtime dependencies. State lives in SQLite and local
files. Python 3.11+ on macOS or Linux is supported.

## Try it

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[pdf]'

# Use a new directory. This is synthetic and makes no network calls.
llm-ae --home /tmp/llm-ae-demo demo
llm-ae --home /tmp/llm-ae-demo status
llm-ae --home /tmp/llm-ae-demo outbox list
```

The demo imports a manuscript from an email, prepares an assessment, simulates
two invitations and a reminder, records reports, sends a simulated recommendation,
and imports a revision. A second case follows the desk-rejection branch. Exported
`.eml` files can be inspected in the demo's `outbox/`. All identities and reports
are fixtures. Simulated deliveries are explicitly identified in the audit log.

Without installing anything, `python3 -m llm_ae` runs the same CLI. PDF support
requires the optional `pypdf` dependency.

## What is implemented

| Stage | LLM-AE does | AE action |
|---|---|---|
| Intake | Reads a selected IMAP folder without marking messages read, imports a unique manuscript attachment from configured editors, deduplicates messages | Resolve ambiguous attachments, confirm title/authors and manuscript portal link |
| Assessment | Extracts PDF/text/Markdown/LaTeX, produces a structured, evidence-linked triage with a configured model | Inspect the manuscript and assessment, choose review or a desk-rejection recommendation |
| Reviewer discovery | Searches collected email contacts, arXiv, and OpenAlex; supplies a Scholar search link | Verify identity and address, record conflicts, choose reviewers |
| Invitations | Drafts messages signed by the AE, sends through the AE's SMTP account after approval, tracks response deadlines | Approve invitations, confirm replies |
| Review | Tracks reports, drafts bounded follow-ups, supports extensions, synthesizes completed reports | Resolve ambiguous replies and report disagreements |
| Decision | Drafts the AE's recommendation to the editor, tracks the editor's response and revision rounds | Approve the recommendation and record the editor's decision |
| Follow-up | Lists actions/deadlines, exports a case dossier, runs repeatable work cycles | Review tasks or connect the CLI to a local scheduler |

LLM-AE does not infer AI authorship. Suspected use of AI is not a rejection
criterion. An assessment is advisory: neither PDF extraction nor a model output
establishes that a proof is correct, an experiment reproduces, or a contribution
is novel. The offline provider only produces a checklist and always requests
human assessment.

## Configure an editorial account

```sh
llm-ae init
# Edit ~/.llm-ae/config.toml
llm-ae doctor
```

Use `--home PATH` or `LLM_AE_HOME` to select a different private runtime directory.
Put global options before the command. All commands support `--json` for scripts.

1. Set `[ae]` name, email, journal, scope, and the allowed editor addresses.
2. Configure IMAP/SMTP hostnames, username, and folder. SMTP supports implicit TLS
   (usually port 465) or required STARTTLS (usually port 587). IMAP uses TLS.
3. Supply credentials through the named environment variables. Password/app-password
   authentication and externally supplied OAuth2 access tokens are supported.
   OAuth login and refresh are not implemented. Do not put passwords in TOML.
4. Leave `mail.transport = "file"` while inspecting drafts. Change it to `"smtp"`
   when ready to send from the AE account. Preview export never marks mail sent
   or advances assignments.
5. For model assessments, set `llm.provider = "openai"`, choose `llm.model`, and set
   `OPENAI_API_KEY` in your shell. The adapter uses the Responses API with strict
   structured output and `store: false`. A Responses-compatible local service can
   be configured through `base_url` and a dummy key if that service needs none.
6. Before using a remote model with confidential manuscripts, confirm the journal
   permits it and set `allow_remote_manuscripts = true`. This transmits extracted
   text and, for synthesis, referee reports to the configured endpoint.
   `store: false` is not a guarantee of zero provider retention.

`doctor` reports whether configuration and environment variables are present. It
does not test account access or display secrets. No live mailbox or API
credentials are included in this repository.

## A complete workflow

Import from IMAP, an `.eml` file, or a manuscript on disk:

```sh
llm-ae inbox sync
llm-ae inbox list
llm-ae inbox import /path/to/assignment.eml

llm-ae submissions add /path/to/paper.pdf \
  --title 'A statistical method' --author 'First Author' --author 'Second Author' \
  --editor editor@example.org --access-link https://journal.example.org/manuscript/123
```

Commands return IDs such as `sub-…`, `rev-…`, `asg-…`, and `mail-…`. Use the
actual returned IDs in place of `SUBMISSION`, `REVIEWER`, `ASSIGNMENT`, and `DRAFT`.
For email imports, authors are deliberately left unconfirmed:

```sh
llm-ae submissions metadata SUBMISSION --title 'Confirmed paper title' \
  --author 'First Author' --author 'Second Author' \
  --access-link https://journal.example.org/manuscript/123
llm-ae submissions assess SUBMISSION
llm-ae submissions show SUBMISSION
llm-ae submissions route-review SUBMISSION
```

Find reviewer leads. The contacts scan is explicit and bounded by the configured
lookback and batch limit. Repeating the scan advances to the next batch. It retains names, addresses, and subject-based topic
hints from direct correspondence with the AE, not message bodies. A connection
is a lead, not evidence that the person is qualified or free of conflicts.

```sh
llm-ae contacts scan --folder Sent
llm-ae reviewers find SUBMISSION --source contacts --query 'causal inference'
llm-ae reviewers find SUBMISSION --source arxiv --query 'all:"causal inference"'
llm-ae reviewers find SUBMISSION --source openalex --query 'causal inference'
llm-ae reviewers find SUBMISSION --source scholar --query 'causal inference statistics'
llm-ae reviewers list SUBMISSION
llm-ae reviewers add SUBMISSION --name 'A Researcher' --email researcher@example.org \
  --affiliation 'Example University' --evidence 'Verified faculty page URL'
llm-ae reviewers check REVIEWER clear --email researcher@example.org \
  --note 'Verified identity and address; checked coauthorship, institution, and declared conflicts'
llm-ae reviewers invite REVIEWER
```

Only the explicitly supplied search query goes to public discovery services, not
the manuscript. Public sources do not supply guessed email addresses. OpenAlex
can require an API key; supply `OPENALEX_API_KEY` if needed. Public results are
cached for 24 hours. arXiv queries are spaced at least three seconds apart.
Scholar is a browser link, not a scraper. Automated exhaustive conflict checks
are not implemented.

Inspect and deliver the exact message:

```sh
llm-ae outbox show DRAFT
llm-ae outbox edit DRAFT --body /path/to/email-body.txt  # optional
llm-ae outbox approve DRAFT
llm-ae outbox send DRAFT
```

Editing resets approval. Changing the sender, recipient identity, conflict
status, or relevant workflow state prevents stale delivery. Invitations contain
the journal's manuscript portal link; PDFs are not automatically attached.

Record reviewer replies from the inbox or directly:

```sh
llm-ae inbox sync
llm-ae inbox show INBOUND_MESSAGE
llm-ae inbox apply INBOUND_MESSAGE accept
# Or record a response received through a journal portal:
llm-ae assignments accept ASSIGNMENT
llm-ae assignments extend ASSIGNMENT --days 14 --note 'Reviewer requested more time'
llm-ae assignments report ASSIGNMENT /path/to/report.txt --recommendation revise
llm-ae assignments list --submission SUBMISSION
```

An inbound reply is linked using its thread and expected sender, or a submission
ID and known assignment. It never becomes an acceptance, report, or decision
solely because a model or email says so. For attached reports, extract or provide
the report text with `inbox apply … review --report … --recommendation …`.
Unmatched editor messages can be linked with `inbox apply … link --submission …`.
Use `assignments decline` or `assignments cancel` to close unavailable reviewers.

After the configured number of reports arrive and outstanding assignments are
resolved, synthesize and prepare the recommendation:

```sh
llm-ae submissions synthesize SUBMISSION
llm-ae submissions recommend SUBMISSION revise --rationale /path/to/rationale.txt
llm-ae outbox show DRAFT
llm-ae outbox approve DRAFT
llm-ae outbox send DRAFT
llm-ae submissions editor-decision SUBMISSION revise --note 'Editor requested revision in email dated …'
llm-ae submissions revise SUBMISSION /path/to/revision.pdf
```

The revised manuscript starts a new round and retains the previous manuscript,
assessments, correspondence, and reports. Conflict checks are reset for the new
round. For desk rejection, use `submissions recommend SUBMISSION desk_reject
--rationale …`, inspect/approve/send the draft, and record the editor's decision.
`accept`, `revise`, and `reject` recommendations require a completed report.

## Work cycles and reminders

```sh
llm-ae run                                  # draft due reminders and list actions
llm-ae run --sync --analyze                  # also ingest and assess
llm-ae run --sync --analyze --send-approved  # also deliver approved mail
llm-ae tasks
llm-ae --json submissions export SUBMISSION > /private/path/case.json
llm-ae audit
```

`run --analyze` assesses new manuscripts and completed review rounds. A model
recommendation for rejection or a post-review verdict becomes a draft, not an
approved email. Review recommendations remain an AE routing task. `run` does not
choose reviewers or automatically interpret their replies.

Set `workflow.auto_approve_reminders = true` to delegate routine follow-ups.
With `--send-approved` and SMTP enabled, due reminders then send automatically.
Intervals and per-stage reminder limits prevent repeated chasing. After the
limit, the task list asks the AE to intervene. A response or deadline extension
cancels obsolete pending reminders. Invitations and decisions always require
explicit approval.

Run one cycle from cron, systemd, or launchd at your preferred interval. Use an
absolute executable path, provide credentials through the scheduler's protected
environment, and keep logs private. For example, a manually installed cron entry
can run every 30 minutes:

```cron
*/30 * * * * /absolute/path/.venv/bin/llm-ae --home /private/editorial/state --json run --sync --analyze --send-approved >> /private/editorial/run.log 2>&1
```

No scheduler is installed automatically. Tasks are emitted to stdout; desktop,
SMS, and push notifications are not implemented. Connect that output to your
existing notification system if you want unattended alerts. Concurrent CLI
processes using the same home are rejected. A work cycle returns a nonzero exit
code if a processing step fails.

## Delivery recovery and data handling

SMTP does not provide exactly-once delivery. LLM-AE records `sending` before the
network call and `sent` after the server accepts the message. A timeout leaves
`uncertain`; a process crash can leave `sending`. Neither is retried automatically.
Check Sent mail or server logs using the stored Message-ID, then record evidence:

```sh
llm-ae outbox reconcile DRAFT sent --note 'Server log confirms acceptance for Message-ID …'
llm-ae outbox reconcile DRAFT not-sent --note 'Server log confirms no acceptance …'
```

A negative reconciliation returns the message to draft and requires approval
again. SMTP server acceptance does not establish recipient delivery or reading.
The app does not append mail to IMAP Sent; whether it appears there depends on
your provider. Bounces are retained as pending correspondence when they match
a known sender; inspect the mailbox for other delivery notices.

Runtime data belongs outside the repository, by default in `~/.llm-ae/`.
The CLI creates private files and directories, and excludes common runtime data
and secret files from Git. Local storage is not encrypted by this application.
Back up the whole runtime directory while the CLI is stopped. The audit trail
is append-only through the application, not tamper-proof against database edits.

Intake trusts the configured mailbox and editor address allowlist, not headers
as cryptographic authentication. Use your provider's spam/authentication filtering
and a dedicated editorial folder. Unsupported/encrypted PDFs, scanned pages,
multiple plausible attachments, and portal-only assignments need manual intake.
The current extractor does not OCR, inspect figures, or execute TeX. Large model
inputs fail explicitly instead of silently dropping parts of a manuscript.

## Development

```sh
pip install -e '.[pdf,dev]'
ruff check .
python -m unittest discover -s tests -v
```

Tests cover workflow transitions, revision history, IMAP cursoring and read-only
fetching, API response handling, evidence validation, reviewer provenance,
approval invalidation, duplicate prevention, reminders, and uncertain-send
recovery. Integration tests use mocked services; no real reviewer is contacted.
Read [HANDOFF.md](HANDOFF.md) for current validation and remaining work.

Implementation references: [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[arXiv API manual](https://info.arxiv.org/help/api/user-manual.html), and
[OpenAlex works](https://help.openalex.org/data/works/).
