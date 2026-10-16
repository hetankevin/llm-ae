#!/usr/bin/env python3
"""Publisher tool: bundle LLM-AE's native-app registrations, never user tokens."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_ae.config import AEError  # noqa: E402
from llm_ae.oauth import validate_client  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--google-desktop-json", type=Path)
    parser.add_argument("--microsoft-client-id")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "llm_ae/oauth_clients.json",
    )
    args = parser.parse_args(argv)
    if not args.google_desktop_json and not args.microsoft_client_id:
        parser.error("provide at least one publisher application registration")
    try:
        data = json.loads(args.output.read_text()) if args.output.exists() else {}
        if args.google_desktop_json:
            raw = json.loads(args.google_desktop_json.read_text())
            if "installed" not in raw or "web" in raw:
                raise AEError("Use a Google Desktop app JSON, never a Web application credential.")
            data["google"] = validate_client("google", raw["installed"])
        if args.microsoft_client_id:
            data["microsoft"] = validate_client(
                "microsoft", {"client_id": args.microsoft_client_id}
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(json.dumps(data, indent=2) + "\n")
                stream.flush()
                os.replace(temporary, args.output)
            finally:
                temporary.unlink(missing_ok=True)
        print("Publisher registrations saved. Test browser login before publishing the release.")
        return 0
    except (OSError, ValueError, TypeError, AEError) as exc:
        print(
            str(exc)
            if isinstance(exc, AEError)
            else "Could not read or save publisher registrations.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
