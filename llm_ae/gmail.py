"""Gmail read/send transport. No mailbox mutation or deletion permissions."""

import base64
import binascii
import json
import re
from datetime import UTC, datetime, timedelta
from email import policy
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from .config import AEError
from .http import urlopen
from .oauth import access_token

ROOT = "https://gmail.googleapis.com/gmail/v1/users/me/"


class MessageTooLarge(AEError):
    pass


def request(config, path, *, query=None, body=None, limit=1024 * 1024):
    # Keep bearer tokens on one fixed origin, including on redirects.
    if not re.fullmatch(r"profile|labels|messages(?:/[A-Za-z0-9_-]+)?", path):
        raise AEError("Invalid Gmail API path")
    if body is not None and path != "messages/send":
        raise AEError("Only Gmail message sending may write to the API")
    url = ROOT + path + ("?" + urlencode(query) if query else "")
    req = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + access_token(config),
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(req, timeout=30) as response:
            data = response.read(limit + 1)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise AEError(
            f"Gmail API request failed (HTTP {code}). Check API activation and reconnect email."
        ) from None
    except (URLError, OSError) as exc:
        raise AEError("Gmail API connection failed") from exc
    if len(data) > limit:
        raise MessageTooLarge("Gmail response exceeds the configured size limit")
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise AEError("Gmail returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise AEError("Gmail returned an invalid response")
    return result


def check(config):
    """Verify mailbox identity without fetching messages or sending a test email."""
    profile = request(config, "profile")
    email = profile.get("emailAddress")
    if not isinstance(email, str) or email.lower() != config["mail"]["username"].lower():
        raise AEError("Google signed into a different mailbox; reconnect the requested email")
    label_id(config, config["mail"]["imap_folder"])


def label_id(config, folder):
    aliases = {"INBOX": "INBOX", "SENT": "SENT", "[GMAIL]/SENT MAIL": "SENT"}
    if folder.upper() in aliases:
        return aliases[folder.upper()]
    labels = request(config, "labels").get("labels", [])
    if not isinstance(labels, list):
        raise AEError("Gmail returned an invalid label list")
    matches = [x.get("id") for x in labels if isinstance(x, dict) and x.get("name") == folder]
    if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
        raise AEError("Gmail label not found; use INBOX, SENT, or an exact label name")
    return matches[0]


def fetch_messages(config, folder=None, unseen_uids=None):
    """Checkpoint each immutable Gmail ID, never assume numeric arrival ordering."""
    mail = config["mail"]
    label = label_id(config, folder or mail["imap_folder"])
    since = int((datetime.now(UTC) - timedelta(days=mail["lookback_days"])).timestamp())
    query = {"labelIds": label, "q": f"after:{since}", "maxResults": 100}
    seen = set(unseen_uids or {})
    pages = set()
    count = 0
    for _ in range(100):
        batch = request(config, "messages", query=query)
        messages = batch.get("messages", [])
        if not isinstance(messages, list):
            raise AEError("Gmail returned an invalid message list")
        for message in messages:
            mid = message.get("id") if isinstance(message, dict) else None
            if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", mid):
                raise AEError("Gmail returned an invalid message ID")
            key = "gmail:" + mid
            if key in seen:
                continue
            raw = None
            metadata = request(config, "messages/" + mid, query={"format": "minimal"})
            size = metadata.get("sizeEstimate")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise AEError("Gmail did not report a valid message size; retained for retry")
            if size <= mail["max_message_bytes"]:
                try:
                    result = request(
                        config,
                        "messages/" + mid,
                        query={"format": "raw"},
                        limit=((mail["max_message_bytes"] + 2) // 3) * 4 + 65536,
                    )
                    encoded = result.get("raw")
                    if not isinstance(encoded, str) or not re.fullmatch(
                        r"[A-Za-z0-9_=-]*", encoded
                    ):
                        raise AEError("Gmail returned an invalid raw message; retained for retry")
                    raw = base64.b64decode(
                        encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
                    )
                    if len(raw) > mail["max_message_bytes"]:
                        raw = None
                except MessageTooLarge:
                    raw = None
                except (ValueError, binascii.Error) as exc:
                    raise AEError(
                        "Gmail returned invalid message encoding; retained for retry"
                    ) from exc
            yield key, "1", raw
            seen.add(key)
            count += 1
            if count >= mail["max_messages"]:
                return
        token = batch.get("nextPageToken")
        if not token:
            return
        if not isinstance(token, str) or len(token) > 8192 or token in pages:
            raise AEError("Gmail returned an invalid pagination token")
        pages.add(token)
        query["pageToken"] = token
    raise AEError(
        "Gmail scan exceeded 100 pages; reduce mail.lookback_days or select a smaller label"
    )


def send(config, message):
    raw = message.as_bytes(policy=policy.SMTP)
    if len(raw) > config["mail"]["max_message_bytes"]:
        raise AEError("Outgoing message exceeds mail.max_message_bytes")
    result = request(config, "messages/send", body={"raw": base64.urlsafe_b64encode(raw).decode()})
    if not isinstance(result.get("id"), str) or not result["id"]:
        raise AEError("Gmail did not confirm delivery; inspect Sent mail before reconciling")
