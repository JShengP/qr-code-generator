from urllib.parse import urlparse, urlunparse

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = ("http", "https")

BLOCKED_DOMAINS = {
    "evil.com",
    "malware.example.com",
    "phishing.example.com",
}


def is_blocked_domain(hostname: str | None) -> bool:
    if hostname is None:
        return True
    return hostname.lower() in BLOCKED_DOMAINS


def validate_url(url: str) -> str:
    """Validate (length / scheme / blocklist) and conservatively normalize.

    Normalization scope is intentionally narrow: per RFC 3986, scheme and
    host are case-insensitive (so we lowercase them), but path, query, and
    fragment are case-sensitive at the protocol level — `/User` and `/user`
    can resolve to different resources on the same server. The reference
    answer lowercases the entire URL, which can break case-sensitive paths
    (S3 keys, GitHub raw URLs, etc.); we deviate. See DECISIONS.md Stage 3.

    We also do NOT upgrade `http://` to `https://`: not every target speaks
    TLS on 443, and a forced upgrade would silently break those redirects.
    """
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"URL exceeds max length of {MAX_URL_LENGTH} characters")

    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(
            f"Invalid scheme {parsed.scheme!r} — only {ALLOWED_SCHEMES} are allowed"
        )

    if not parsed.hostname:
        raise ValueError("Invalid URL — missing hostname")

    if is_blocked_domain(parsed.hostname):
        raise ValueError(f"URL host {parsed.hostname!r} is on the blocklist")

    # Rebuild the netloc with host lowercased; preserve port and userinfo.
    netloc = parsed.hostname.lower()
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    # Collapse the bare-root path ("" or "/") to "" so that
    # "https://example.com" and "https://example.com/" produce the same
    # canonical string. The slash is syntactically required, though,
    # whenever a query or fragment follows — "https://x.com?q=1" is
    # technically valid per RFC 3986 but some servers reject it, so in
    # that case we keep the "/". Deeper paths are left untouched
    # because "/foo/" vs "/foo" can resolve differently on Apache and
    # similar servers (directory listing vs file).
    if parsed.path in ("", "/") and not parsed.query and not parsed.fragment:
        path = ""
    elif parsed.path == "":
        path = "/"
    else:
        path = parsed.path

    return urlunparse(
        (parsed.scheme.lower(), netloc, path, parsed.params, parsed.query, parsed.fragment)
    )
