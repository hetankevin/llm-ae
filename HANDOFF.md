# Handoff

## Goal
Build a minimal CLI assistant for associate editors: email intake,
manuscript assessment, reviewer discovery, correspondence, deadlines, and rounds.

## State (2026-10-15)
Working v0.1 implementation on `main`. Public remote (made public at Kevin's request on 2026-10-15):
https://github.com/hetankevin/llm-ae . Git identity is configured locally as
`hetankevin <kevtan@umich.edu>`.

Latest request: allow users to use their OpenAI or Anthropic account, not only API
keys. Added `auth login`, `auth use`, and `auth status` commands. They delegate
sign-in to official Codex/Claude Code CLIs and select `llm.provider = "codex"`
or `"claude"`. Aliases `oai`/`ant` work. `openai` still denotes the existing
Responses API-key provider in TOML. Login selection preserves unrelated config,
resets a previous model override, and enables remote processing only with an
explicit flag. Credentials are never read/copied by this app.

Python 3.11+ standard-library core, optional pypdf extraction, SQLite local state.
CLI modules live in `llm_ae/`. Run with `python3 -m llm_ae` or the installed
`llm-ae` command. Local `.venv` has an editable install, pypdf, and ruff.

Implemented: IMAP/.eml/manual intake, PDF/text extraction, structured Responses
assessments, local contacts/arXiv/OpenAlex reviewer leads, Scholar search links,
conflict clearance, approved SMTP correspondence, report/deadline tracking,
bounded reminders, decision recommendations, editor outcomes, revision history,
task lists, JSON dossiers, audit records, repeatable work cycles, and a synthetic
offline lifecycle demo. See README for setup and complete command examples.

## Design
Model output is advisory. API assessments expose no tools; account CLI runs use
restricted capabilities and return validated structured output. Editorial emails are drafts
until approved. Routine reminders can be delegated explicitly. Manuscripts,
email, credentials, and the SQLite database stay outside version control.

## Verification
- Account support: 78 tests pass locally, including login selection/failure,
  environment isolation, subprocess timeout cleanup, both structured-output
  adapters, remote-processing gates, and existing evidence validation.
- Live ChatGPT-account assessment passed with a synthetic manuscript and no API
  key using the existing Codex login. Claude account sign-in was confirmed through
  its official CLI; live synthetic assessment is being checked before handoff.
- Official CLI login itself was not repeated or changed on Kevin's machine.
  Smoke tests use temporary runtime homes and send no email.
- 62 unittest cases pass with PDF extras on Python 3.13.9.
- `ruff check .` passes and source has been formatted.
- Core tests also ran on the host Python 3.14.3 before PDF extras were installed.
- Editable installation and installed `llm-ae` entry point verified.
- Synthetic full workflow (review -> revision and desk rejection -> closed)
  completed using the installed command, with zero network calls or real emails.
- Demo runtime: `/private/tmp/llm-ae-packaged-demo`; output:
  `/private/tmp/llm-ae-packaged-demo.json`. These are not committed.
- IMAP, SMTP, model APIs, and discovery APIs have fixture-based integration tests.
- Live public discovery smoke tests passed for arXiv (`all:statistics`, one work,
  three candidate authors) and OpenAlex (`mean estimation`, one work, two candidate
  authors). Only public query terms were transmitted, using the synthetic demo home.
- GitHub Actions passed all 62 tests and the demo on Python 3.11, 3.12, 3.13,
  and 3.14 at `f6d6f7d`:
  https://github.com/hetankevin/llm-ae/actions/runs/38801635593 .
- CI uses Ubuntu 24.04 and the verified current v7 checkout/setup-python actions
  to avoid deprecated Node action runtimes and an imminent ubuntu-latest change.
- Follow-up robustness fixes sort unordered IMAP search results before cursoring,
  require UIDVALIDITY, normalize malformed Message-IDs, and handle malformed API
  payloads and network failures cleanly. Local tests/lint pass after these fixes.

## Important invariants
- File transport exports previews without advancing workflow. Only SMTP acceptance
  or an explicit uncertain-delivery reconciliation applies send effects.
- Sending is durably claimed before SMTP. `sending`/`uncertain` mail never retries
  automatically. Reconcile using actual server/mailbox evidence.
- Approved content is hashed with sender identity. Editing resets approval.
- CLI processes lock the runtime home. SQLite transactions protect state changes.
- Intake uses Message-ID plus a source-message uniqueness index for crash recovery.
- IMAP cursors include account, folder, and UIDVALIDITY. Contact scans use a separate
  cursor and deduplicate observed messages while retaining topic history.
- Assessments cannot choose recipients or execute editorial workflow actions.
  Account CLIs run in temporary directories with API-key environment variables
  removed, account authentication checked, and no persistent conversation. Codex
  uses read-only mode with shell/apps/hooks/agents/web disabled and user config
  ignored. Claude uses safe mode, empty tool/MCP sets, and no session persistence.
  Both retain organization-managed policy and provider-side account data policies.
  Unverified evidence quotes force manual assessment. Inputs are never truncated.
- Reviewer conflicts require evidence and a verified address. Identity/coauthor
  checks are manual beyond blocking exact author names and AE/editor addresses.
- Runtime data and secrets are excluded from Git. No actual manuscript or mailbox
  was accessed. The asserted tripling of journal submissions was not verified and
  is not represented as an established fact in the product.

## Remaining deployment work / limits
- Supply AE/journal/editor details and IMAP/SMTP credentials. Choose an official
  CLI account login or a model/API key. Live mailbox operation remains untested.
- Confirm journal policy before enabling remote manuscript processing. Defaults are
  offline provider and file transport. API `store: false` is not a zero-retention claim.
- Mailbox OAuth token acquisition/refresh, OCR, DOCX, portal scraping, exhaustive conflict
  checks, automatic interpretation of reviewer replies, and push notifications are
  outside v0.1. Scheduler integration is documented but not installed.
- Public searches use explicitly supplied queries, not manuscript text. Scholar
  supplies a browser URL. Reviewer email addresses are never invented.
- Local data is not application-encrypted; the audit trail is not tamper-evident.

## Next steps
The repo's PUBLIC visibility has been verified via GitHub. Account setup is the
next deployment step for Kevin. Future extensions should preserve the workflow
and delivery invariants above. Check the latest CI run when resuming work.

