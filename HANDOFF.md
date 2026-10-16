# Handoff

## Goal
Build a minimal CLI assistant for associate editors: email intake,
manuscript assessment, reviewer discovery, correspondence, deadlines, and rounds.

## State (2026-10-16)
Working v0.1 implementation on `main`. Public remote (made public at Kevin's request on 2026-10-15):
https://github.com/hetankevin/llm-ae . Git identity is configured locally as
`hetankevin <kevtan@umich.edu>`.

Latest request: Gmail and Outlook browser sign-in without app passwords.
Implemented native OAuth authorization-code flow with PKCE/state, loopback
callback, keychain access/refresh bundles, automatic renewal/rotation, and XOAUTH2
for existing IMAP/SMTP transports. Guided setup uses browser auth for Gmail and
Outlook/Microsoft 365. Other providers retain their existing password setup.

**Activation remains pending.** `llm_ae/oauth_clients.json` contains empty
publisher registrations. Kevin resumed activation on October 16 and asked us to
handle registration. Chrome has a signed-in personal Google Cloud session. A
dedicated LLM-AE project form is prepared, but has not been submitted. Microsoft
Entra is at sign-in and offers GitHub sign-in. Automatic approval review blocked
the combined project-creation / GitHub-sign-in action pending specific account
authorization; a bundled approval question is pending. No provider account,
project, or OAuth client has been created or changed. The two Chrome tabs are
marked for handoff. Do not claim public browser login is live.
`docs/oauth-publishing.md` and `scripts/configure_oauth_clients.py` prepare the
publisher work. End users are never asked to register apps, supply client IDs,
or fall back to Gmail/Outlook app passwords. When activated, registrations ship
inside the package so users only approve access in their browser.

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
- Browser-email CI passed all 115 tests, lint, and the offline demo on Python
  3.11–3.14 at `61ea420`: https://github.com/hetankevin/llm-ae/actions/runs/38862523342 .
- Wheel build passed and includes oauth.py and the publisher-registration resource.
- Browser email login: 115 tests pass locally. New coverage includes actual local
  callback requests for both providers, PKCE/state/host validation, browser
  cancellation/timeout, full wizard through mocked mail servers, refresh rotation,
  partial consent, wrong-mailbox rejection, no password fallback, and rollback.
- Synthetic credentials only. No real Google/Microsoft OAuth exchange or mailbox
  authentication has been tested because publisher activation is pending.
- Package registration helper rejects Google Web credentials and Microsoft client
  secrets; native client identifiers are shipped, user tokens never are.
- Guided setup: 96 tests pass with setup extras, including the complete wizard,
  retries, interruption, preserving existing state/config, no secret leakage,
  custom servers, readonly connection checks, keychain failures, rejecting plaintext
  keyring backends, official-installer download failures, and installer-to-terminal handoff.
- A real macOS Keychain save/read/delete passed with a synthetic temporary entry.
  No existing credentials were read and no real mailbox was accessed.
- `ruff check .` and shell syntax check pass.
- Fresh install from the public GitHub archive succeeded in isolated uv tool/bin
  directories, including PDF and keyring dependencies; the installed CLI demo passed.
- Interactive PTY walkthrough completed all three stages using synthetic identity
  and stubbed account/mail services. Real hidden getpass input did not echo.
- Piped installer handoff (`cat install.sh | sh`) reached interactive setup through
  /dev/tty using fixture executables, without changing shell profiles.
- GitHub Actions passed all 96 tests, lint, and demo on Python 3.11–3.14 at
  `0bf407f`: https://github.com/hetankevin/llm-ae/actions/runs/38850310784 .
- Account support: 78 tests pass locally, including login selection/failure,
  environment isolation, subprocess timeout cleanup, both structured-output
  adapters, remote-processing gates, and existing evidence validation.
- Live ChatGPT-account and Claude-account assessments both passed with a synthetic
  manuscript and no API key using the existing official CLI logins. Each returned
  a valid structured assessment through the normal Workflow.assess path. The
  Claude smoke test also exercised auth selection and reloaded the saved TOML.
- Official CLI login itself was not repeated or changed on Kevin's machine.
  Smoke tests use temporary runtime homes and send no email.
- Baseline before account support: 62 unittest cases passed with PDF extras on Python 3.13.9.
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
- Browser mail tokens are refreshed under the runtime-home lock. OAuth tokens are
  only sent to fixed provider endpoints and allowlisted TLS mail servers. Callback
  queries and provider error bodies are never logged.
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
- Run the guided setup with actual AE/journal/editor and mailbox details.
  Live mailbox operation remains untested; network transports are covered with fixtures.
- Confirm journal policy before enabling remote manuscript processing. Defaults are
  offline provider and file transport for manual initialization. Guided setup selects
  SMTP only after connection tests pass, with explicit approval still required for sends. API `store: false` is not a zero-retention claim.
- Publisher registration/verification and live Gmail/Outlook login remain pending.
  OAuth acquisition/refresh are implemented. OCR, DOCX, portal scraping, exhaustive
  conflict checks, automatic interpretation of reviewer replies, and push notifications
  remain outside v0.1. Scheduler integration is documented but not installed.
- Public searches use explicitly supplied queries, not manuscript text. Scholar
  supplies a browser URL. Reviewer email addresses are never invented.
- Local data is not application-encrypted; the audit trail is not tamper-evident.

## Next steps
Public repo. Publisher activation was explicitly deferred by Kevin. If he resumes,
follow `docs/oauth-publishing.md`, register the apps under accounts he controls,
complete required provider approvals, bundle registrations, and test real sign-in
and renewal before removing the pending-activation notice. Preserve the workflow
and delivery invariants above. Desktop Linux requires an unlocked system keychain.
