# Activating Gmail and Outlook sign-in

This is a one-time **LLM-AE publisher task**. Users never create a cloud project,
register an app, paste a client ID, or create an app password for Gmail/Outlook.
They run `llm-ae setup`, choose their provider, and approve access in their browser.

**Current release status:** Google project `llm-ae`, its desktop OAuth client,
and Gmail API activation exist. The native client is bundled with explicit approval
for the two GitHub push-protection exceptions. Google is in production, with an
unverified-app warning and 100-user cap until verification is approved. Gmail uses
its read/send API rather than full-mailbox IMAP permissions. Public verification
and live mailbox tests are pending. Microsoft publisher registration remains pending.

## Google

1. Under an account controlled by the publisher, create/select the LLM-AE project
   in [Google Cloud](https://console.cloud.google.com/auth/overview).
2. Configure Google Auth Platform branding/audience and the consent screen for
   LLM-AE. Supply the publisher's support contact and public application/privacy
   URLs. Use the appropriate audience for the intended users.
3. Enable the Gmail API and request `https://www.googleapis.com/auth/gmail.readonly`
   and `https://www.googleapis.com/auth/gmail.send`. Do not request `mail.google.com`,
   modify, or delete permissions. Intake uses immutable message-ID checkpoints,
   bounded raw MIME retrieval, and read-only API calls.
4. Create a **Desktop app** OAuth client and download its JSON. This uses a
   loopback redirect on `127.0.0.1` with a random port, PKCE, and state validation.
5. Complete Google's applicable restricted-scope verification before a public
   launch. Testing-mode projects are limited to configured test users and may
   receive refresh tokens that expire after seven days. Verification and approval
   are provider-controlled, not something the CLI can bypass.

The native client JSON includes an optional `client_secret`. Google documents
that installed clients cannot keep this value confidential. Only bundle the
**Desktop app** registration. Never use a Web application secret, service-account
key, user access token, or refresh token here.

Sources: [native-app authorization](https://developers.google.com/identity/protocols/oauth2/native-app),
[Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
[OAuth verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

## Microsoft

1. In [Microsoft Entra App registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade),
   create LLM-AE under a publisher-controlled tenant.
2. Select accounts in any organizational directory **and personal Microsoft
   accounts**, so both Microsoft 365 and Outlook.com can sign in.
3. Add the **Mobile and desktop applications** platform with redirect URI
   `http://localhost/callback`. The login flow uses a random local port, which
   Microsoft's localhost matching rules permit. Use a public native client,
   not a Web or SPA registration. No client secret is needed.
4. Configure delegated Exchange Online permissions:
   `https://outlook.office.com/IMAP.AccessAsUser.All` and
   `https://outlook.office.com/SMTP.Send`. The flow also requests `offline_access`
   for renewable sign-in. It uses the `common` authorization endpoint.
5. Complete publisher verification as appropriate for public distribution.
   Organizations may require administrator approval and must allow IMAP and
   authenticated SMTP. OAuth does not override an organization's mail policy.

Sources: [mail OAuth permissions](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth),
[localhost redirect matching](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url),
[public-client authorization flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow).

## Bundle and test

Once the registrations exist, run this from the repository:

```sh
python3 scripts/configure_oauth_clients.py \
  --google-desktop-json /path/to/downloaded-desktop-client.json \
  --microsoft-client-id YOUR_APPLICATION_CLIENT_ID
```

This updates the package resource shipped with the installer. It accepts only
Google's `installed` JSON shape and a Microsoft public client ID, preserves the
other provider when updating one, and never prints credentials. Inspect the diff
before publishing. The original downloaded Google JSON does not belong in Git.

For testing a registration without modifying package defaults, the publisher can
point `LLM_AE_OAUTH_CLIENTS_FILE` at a JSON file with the same schema. This override
is for development and private distributions; normal users do not set it.

Test `llm-ae --home /path/to/new-test-home setup` with one Gmail and one Outlook
mailbox, including a Microsoft 365 mailbox if supporting organizations. Complete
the browser consent, verify mailbox identity and exercise renewal. Gmail setup
checks the profile and selected label. Other providers authenticate IMAP/SMTP and
select the inbox read-only. Setup fetches no messages and sends no test email.
After these live checks and required provider approvals, publish the configured
package and remove the pending-activation notice from README and this document.

## Credential lifetime

Access and refresh tokens are stored together in the OS keychain. Only an opaque
reference is saved in TOML. The CLI refreshes shortly before expiry, preserves a
Google refresh token when it is omitted from a refresh response, and saves rotated
Microsoft refresh tokens before using the new access token. Network/token errors
never trigger password fallback. Normal CLI commands hold the runtime-home lock
while refreshing, preventing concurrent refreshes from that home.

The selected Gmail mailbox must pass its API identity/label check; other providers
must authenticate against both IMAP and SMTP before setup replaces its previous configuration. A failed replacement removes
its new local credential and preserves the existing connection. Successful
replacement removes the old local credential. To revoke provider-side access,
use the Google/Microsoft account's authorized-app settings. Deleting local
credentials alone does not revoke consent at the provider.
