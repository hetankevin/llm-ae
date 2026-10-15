# Handoff

## Goal
Build a private, minimal CLI assistant for associate editors: email intake,
manuscript assessment, reviewer discovery, correspondence, deadlines, and rounds.

## State
Initial implementation in progress. Private remote: `hetankevin/llm-ae`.
Python standard library core, optional pypdf extraction, SQLite local state.
Live integrations: IMAP/SMTP, OpenAI Responses, arXiv, OpenAlex.

## Design
Model output is advisory and cannot invoke tools. Editorial emails are drafts
until approved. Routine reminders can be delegated explicitly. Manuscripts,
email, credentials, and the SQLite database stay outside version control.

## Verification / next steps
Implement and test the complete offline lifecycle, then verify packaging and
private remote. Live mailbox/model operation needs the AE's configuration.

