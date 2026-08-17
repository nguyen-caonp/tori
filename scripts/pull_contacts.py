#!/usr/bin/env python3
"""Pull Simplero contacts and report which fields actually hold phone numbers.

Answers the question "the UI shows a phone number but my export doesn't":
dumps every field on every contact, including custom fields, then prints a
fill-rate report so you can see exactly which field your numbers live in.

    python3 scripts/pull_contacts.py            # diagnose + write contacts.csv
    python3 scripts/pull_contacts.py --fields   # just list field definitions
    python3 scripts/pull_contacts.py --limit 50 # sample instead of full pull

Zero dependencies: standard library only. Reads .env from the repo root.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "https://simplero.com/api/v2"

# Simplero's docs describe /customers.json (v1). v2 "supports many more
# resources" but its spec was unreachable when this was written, so try the
# plausible names and use whichever answers 200.
CONTACT_PATH_CANDIDATES = ("contacts.json", "customers.json")
FIELDS_PATH_CANDIDATES = ("account/fields.json",)

PHONE_KEY_HINT = re.compile(r"phone|mobil|tel|cell|sms|whats", re.I)
PHONE_VALUE_HINT = re.compile(r"^[+(]?[\d][\d\s()\-./]{5,}$")


# --------------------------------------------------------------------------
# config


def load_dotenv(path: Path) -> None:
    """Minimal .env reader. Does not overwrite real environment variables."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip one layer of matching quotes; .env values are often quoted
        # because bare parentheses break `source .env`.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # This sandbox routes HTTPS through a MITM proxy whose CA must be trusted.
    # Harmless (and skipped) anywhere that file does not exist.
    for candidate in (os.environ.get("SSL_CERT_FILE"), "/root/.ccr/ca-bundle.crt"):
        if candidate and Path(candidate).exists():
            try:
                ctx.load_verify_locations(candidate)
            except ssl.SSLError:
                pass
    return ctx


# --------------------------------------------------------------------------
# transport


class Simplero:
    def __init__(self, api_key: str, user_agent: str, base_url: str) -> None:
        if not api_key:
            sys.exit("SIMPLERO_API_KEY is not set. Copy .env.example to .env.")
        if not user_agent:
            # Simplero returns 400 {"error": "Incorrect User-Agent"} without one.
            sys.exit('SIMPLERO_USER_AGENT is not set, e.g. "tori (you@example.com)".')
        self.api_key = api_key
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.ssl_context = build_ssl_context()

    def get(self, path: str, params: dict | None = None, *, attempts: int = 5):
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, method="GET")
            request.add_header("User-Agent", self.user_agent)
            request.add_header("X-API-Key", self.api_key)
            request.add_header("Accept", "application/json")
            try:
                with urllib.request.urlopen(
                    request, timeout=60, context=self.ssl_context
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 429 and 5xx are worth retrying; other 4xx are not. A 401 is a
                # bad key and will never fix itself.
                if exc.code != 429 and exc.code < 500:
                    raise
                if attempt == attempts:
                    raise
                delay = float(exc.headers.get("Retry-After") or min(2**attempt, 60))
                print(f"  {exc.code} on {path}, retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
            except urllib.error.URLError as exc:
                if attempt == attempts:
                    raise
                print(f"  {exc.reason}, retrying", file=sys.stderr)
                time.sleep(min(2**attempt, 60))
        raise RuntimeError("unreachable")

    def resolve(self, candidates: tuple[str, ...]) -> str | None:
        """Return the first candidate path that responds without a 404."""
        for path in candidates:
            try:
                self.get(path, {"per_page": 1}, attempts=1)
                return path
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
        return None

    def paginate(self, path: str, limit: int | None = None):
        """Yield records. Simplero pages are 0-indexed, max 100 per page."""
        page = 0
        seen = 0
        while True:
            batch = self.get(path, {"page": page, "per_page": 100})
            if isinstance(batch, dict):
                # Tolerate an envelope even though v1 documents a bare array.
                for key in ("contacts", "customers", "data", "results"):
                    if isinstance(batch.get(key), list):
                        batch = batch[key]
                        break
            if not isinstance(batch, list) or not batch:
                return
            for record in batch:
                yield record
                seen += 1
                if limit and seen >= limit:
                    return
            print(f"  page {page}: {len(batch)} records", file=sys.stderr)
            if len(batch) < 100:
                return
            page += 1


# --------------------------------------------------------------------------
# analysis


def flatten(obj, prefix: str = "") -> dict:
    """Flatten nested dicts to dotted keys so custom fields become columns."""
    flat: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        if obj and all(not isinstance(i, (dict, list)) for i in obj):
            flat[prefix] = ", ".join(str(i) for i in obj)
        else:
            for index, value in enumerate(obj):
                flat.update(flatten(value, f"{prefix}[{index}]"))
    else:
        flat[prefix] = "" if obj is None else str(obj)
    return flat


def report(rows: list[dict]) -> None:
    """Print fill rates, phone-bearing fields first. This is the answer."""
    total = len(rows)
    filled: Counter = Counter()
    phone_like: Counter = Counter()
    for row in rows:
        for key, value in row.items():
            if value != "":
                filled[key] += 1
                if PHONE_VALUE_HINT.match(value.strip()):
                    phone_like[key] += 1

    columns = sorted({key for row in rows for key in row})
    suspects = [
        c for c in columns
        if PHONE_KEY_HINT.search(c) or phone_like[c] > max(1, total * 0.05)
    ]

    print(f"\n{'=' * 68}\n{total} contacts pulled\n{'=' * 68}")
    print("\nPHONE-BEARING FIELDS  (this is where your numbers actually are)\n")
    if suspects:
        width = max(len(c) for c in suspects)
        for column in sorted(suspects, key=lambda c: -phone_like[c]):
            pct = 100 * filled[column] / total if total else 0
            print(
                f"  {column:<{width}}  filled {filled[column]:>5}/{total} "
                f"({pct:5.1f}%)   phone-shaped {phone_like[column]:>5}"
            )
    else:
        print("  none detected — check the field list above for an unusual name")

    print(f"\nALL FIELDS ({len(columns)} columns)\n")
    width = max((len(c) for c in columns), default=10)
    for column in columns:
        pct = 100 * filled[column] / total if total else 0
        print(f"  {column:<{width}}  {filled[column]:>5}/{total} ({pct:5.1f}%)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fields", action="store_true",
                        help="only print custom field definitions")
    parser.add_argument("--limit", type=int,
                        help="stop after N contacts (sampling)")
    parser.add_argument("--out", default="contacts.csv",
                        help="CSV output path (gitignored by default)")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    client = Simplero(
        os.environ.get("SIMPLERO_API_KEY", ""),
        os.environ.get("SIMPLERO_USER_AGENT", ""),
        os.environ.get("SIMPLERO_BASE_URL", DEFAULT_BASE),
    )
    print(f"base url: {client.base_url}", file=sys.stderr)

    # Custom field definitions first: they name the columns the CSV export drops.
    fields_path = client.resolve(FIELDS_PATH_CANDIDATES)
    if fields_path:
        definitions = client.get(fields_path)
        print("\nCUSTOM CONTACT FIELDS\n")
        print(json.dumps(definitions, indent=2, ensure_ascii=False))
    else:
        print("could not locate the account fields endpoint", file=sys.stderr)
    if args.fields:
        return 0

    contacts_path = client.resolve(CONTACT_PATH_CANDIDATES)
    if not contacts_path:
        print(
            f"none of {CONTACT_PATH_CANDIDATES} responded; check the v2 spec",
            file=sys.stderr,
        )
        return 1
    print(f"\nreading {contacts_path}", file=sys.stderr)

    rows = [flatten(c) for c in client.paginate(contacts_path, args.limit)]
    if not rows:
        print("no contacts returned", file=sys.stderr)
        return 1

    columns = sorted({key for row in rows for key in row})
    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)

    report(rows)
    # Contains personal data. .gitignore excludes *.csv so it stays local.
    print(f"\nwrote {len(rows)} rows x {len(columns)} columns to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
