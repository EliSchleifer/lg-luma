"""authorized_keys lines and dev tokens. Pure functions, no Home Assistant or network.

Session keys carry a comment `luma-dev:<request id>:<expiry, Unix seconds>`. Only lines with
that tag are ever removed; every other line of the TV's authorized_keys is kept byte for byte.
Dropbear (the TV's SSH server) has no expiry option, so the tag is how expired keys are found.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

TAG = "luma-dev"
# Keys may run commands but not turn the TV into a gateway: Dropbear supports these options.
OPTIONS = "no-port-forwarding,no-agent-forwarding,no-X11-forwarding"
_TAG_RE = re.compile(rf"\s{TAG}:([A-Za-z0-9_-]+):(\d+)\s*$")


def new_token() -> str:
    """A 256-bit dev token."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str | None, expected_hash: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token_hash(token), expected_hash)


def key_line(public_key: str, request_id: str, expires_at: int, *, options: bool = True) -> str:
    """The authorized_keys line for a session key. `public_key` is `type base64` (a comment, if
    any, is replaced by the tag)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
        raise ValueError("bad request id")
    algorithm, blob = public_key.split()[:2]
    prefix = f"{OPTIONS} " if options else ""
    return f"{prefix}{algorithm} {blob} {TAG}:{request_id}:{int(expires_at)}"


def parse_tag(line: str) -> tuple[str, int] | None:
    """The request id and expiry of a session key line, or None for any other line."""
    match = _TAG_RE.search(line)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def rewrite(
    content: str,
    *,
    add: str | None = None,
    remove: frozenset[str] | set[str] = frozenset(),
    now: float | None = None,
) -> tuple[str, dict[str, int]]:
    """authorized_keys with session keys in `remove` (and, given `now`, expired ones) dropped and
    `add` appended. Returns the new content and the session keys left, id → expiry."""
    kept: list[str] = []
    remaining: dict[str, int] = {}
    for line in content.splitlines():
        tag = parse_tag(line)
        if tag is not None:
            request_id, expires_at = tag
            if request_id in remove or (now is not None and expires_at <= now):
                continue
            remaining[request_id] = expires_at
        kept.append(line)
    if add is not None:
        tag = parse_tag(add)
        if tag is None or "\n" in add:
            raise ValueError("only tagged single-line session keys can be added")
        kept.append(add)
        remaining[tag[0]] = tag[1]
    text = "\n".join(kept)
    return (text + "\n" if text else ""), remaining
