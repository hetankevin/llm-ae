# LLM-AE

A local CLI assistant for associate editors. It turns incoming manuscripts into
tracked editorial cases, prepares assessments and correspondence, finds reviewer
leads, and follows review rounds through to the editor's decision.

The core has no third-party runtime dependencies. State lives in SQLite and local
files. Python 3.11+ on macOS or Linux is supported.

## Install and connect

On macOS or desktop Linux, paste this into your terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/hetankevin/llm-ae/main/install.sh | sh
```

The installer sets up Python, PDF support, and keychain support in an isolated
app environment, then starts a three-step wizard:

1. Choose **OpenAI** or **Anthropic**. The wizard installs the official account
   CLI if needed and opens its sign-in flow, or reuses your existing login.
2. Connect your **email**. Gmail and Outlook use browser sign-in, with renewable
   access saved in your system keychain. The wizard tests incoming and outgoing
   mail before completing setup. See the activation status below.
3. Enter your **name, journal, and editor addresses**. You're ready to import submissions.

No API key, TOML editing, Node installation, or exported passwords are needed.
You need an account with access to Codex or Claude Code. The wizard asks whether
your journal allows remote manuscript processing before enabling assessments.
It sends no test emails. Subsequent editorial emails require explicit approval
and a send command.

```sh
llm-ae inbox sync
llm-ae tasks
```

If the command is not found immediately after installation, open a new terminal.
To change a connection or resume interrupted setup, run `llm-ae setup` again.
For OpenAI device login, use `llm-ae setup --device-auth`.
Existing data and unrelated configuration are preserved.

**Gmail/Outlook activation is pending.** Browser sign-in and renewal are implemented,
but this public build still needs LLM-AE's publisher-owned Google and Microsoft
app registrations before real users can sign in. The publisher completes this
once using the [activation guide](docs/oauth-publishing.md). End users do not
create app registrations or app passwords. Live mailbox verification is pending.

Once activated, choose **gmail** for Gmail/Google Workspace, or **outlook** for
Outlook.com/Microsoft 365, including custom journal or university domains. Your
organization may require approval or restrict IMAP/SMTP access.
[iCloud](https://support.apple.com/102525), [Yahoo](https://help.yahoo.com/kb/SLN4075.html),
and other custom IMAP/SMTP servers retain password-based setup. Linux needs an
unlocked desktop keychain (Secret Service or KWallet). Setup never falls back to
plaintext credential storage.

Already have [uv](https://docs.astral.sh/uv/guides/tools/)? Install directly:

```sh
uv tool install --python 3.13 --from 'llm-ae[setup] @ https://github.com/hetankevin/llm-ae/archive/refs/heads/main.zip' llm-ae
llm-ae setup
```

The installer is [short and inspectable](install.sh). It uses uv's official
installer when necessary and the selected provider's official installer:
[Codex](https://developers.openai.com/codex/cli/) or
[Claude Code](https://code.claude.com/docs/en/setup).
Rerun it to update LLM-AE and revisit setup.

## Try the offline demo

After installation, use a new directory. This is synthetic and makes no network calls:

```sh
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

## Advanced / manual configuration

```sh
llm-ae init
# Edit ~/.llm-ae/config.toml
llm-ae doctor
```

Use `--home PATH` or `LLM_AE_HOME` to select a different private runtime directory.
Put global options before the command. Noninteractive commands support `--json` for scripts.
Use `llm-ae setup` for guided setup instead of the manual steps below.

1. Set `[ae]` name, email, journal, scope, and the allowed editor addresses.
2. Configure IMAP/SMTP hostnames, username, and folder. SMTP supports implicit TLS
   (usually port 465) or required STARTTLS (usually port 587). IMAP uses TLS.
3. Supply mailbox credentials through the named environment variables.
   Clear `mail.password_keyring` when switching from wizard-managed credentials to
   an environment-variable password. Password/app-password
   authentication and externally supplied OAuth2 access tokens are supported.
   Browser-managed Gmail/Outlook credentials refresh automatically. Externally
   supplied OAuth access tokens do not. Do not put tokens or passwords in TOML.
   Clear `mail.oauth_provider` and `mail.oauth_keyring` before switching from a
   browser connection to externally managed credentials.
4. Leave `mail.transport = "file"` while inspecting drafts. Change it to `"smtp"`
   when ready to send from the AE account. Preview export never marks mail sent
   or advances assignments.
5. Choose a ChatGPT account, a Claude account, or an API key using the instructions below.
6. Before using a remote model with confidential manuscripts, confirm the journal
   permits it and set `allow_remote_manuscripts = true`. This transmits extracted
   text and, for synthesis, referee reports to the selected provider.

`doctor` reports configuration, environment-variable presence, and whether a keychain
reference is configured (it does not unlock or read the keychain). It
does not test account access or display secrets. No live mailbox or API
credentials are included in this repository.

### Sign in with ChatGPT or Claude

Install the official [Codex CLI](https://developers.openai.com/codex/cli/) for
ChatGPT, or [Claude Code](https://code.claude.com/docs/en/setup) for Claude. Use
an up-to-date release. The account must have access to that CLI, and its normal
usage limits apply. Browser login to the chat website alone is not a CLI login.

After `llm-ae init`, choose one:

```sh
llm-ae auth login openai
# Or:
llm-ae auth login anthropic
```

This opens the provider's official sign-in flow and selects it for assessments.
No API key is required. Credentials and refresh stay with Codex or Claude Code,
not in LLM-AE's configuration or database. On a headless machine, OpenAI login
also supports `llm-ae auth login openai --device-auth`.

If you already signed in through the official CLI, reuse that session:

```sh
llm-ae auth use openai
llm-ae auth status
```

`anthropic` selects Claude, and `oai`/`ant` are accepted aliases. Both `login` and
`use` accept an optional `--model NAME`; omitting it uses the CLI's default.
Changing provider resets any previous model override. To enable remote manuscript
processing after checking your journal's policy:

```sh
llm-ae auth use openai --allow-remote-manuscripts
# Or: llm-ae auth use anthropic --allow-remote-manuscripts
llm-ae submissions assess SUBMISSION
```

The saved providers are `codex` and `claude`. The account adapters exclude API-key
environment variables and check the official CLI's authentication method before
assessment. They do not fall back to API-key billing. To switch accounts or sign
out, use the official CLI's account commands; that also affects its other uses.

Assessment runs use temporary working directories, stdin for manuscript text,
structured output, and no persistent CLI conversation. Codex runs read-only with
user config, shell execution, web search, apps, hooks, and multi-agent features
disabled. Claude runs with customizations and model tools disabled. Organization
policies still apply. These are local execution controls, not a promise about
provider-side retention. Account data handling follows the selected provider and
workspace. `llm.cli_timeout_seconds` bounds each assessment (default 300 seconds).
Older CLI versions that lack the required flags fail rather than retrying with
fewer restrictions.

### Use an API key instead

Existing API-key configuration continues to work: set `llm.provider = "openai"`,
choose `llm.model`, and set the environment variable named by `llm.api_key_env`
(default `OPENAI_API_KEY`). This adapter uses the Responses API with strict
structured output and `store: false`. A Responses-compatible local service can
be configured through `base_url` and a dummy key if that service needs none.
`store: false` is not a guarantee of zero provider retention. Anthropic account
access uses Claude Code; a direct Anthropic API-key adapter is not included.

Authentication references: [Codex account login](https://developers.openai.com/codex/auth/),
[Claude Code authentication](https://code.claude.com/docs/en/authentication), and
[Claude Code structured output](https://code.claude.com/docs/en/headless).

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
pip install -e '.[setup,dev]'
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
