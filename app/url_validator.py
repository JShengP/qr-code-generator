import ipaddress
from urllib.parse import urlparse, urlunparse

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = ("http", "https")

# Hostnames forbidden by name. The IP-literal checks below cover the
# common SSRF surface (loopback, RFC 1918, link-local, cloud metadata,
# etc.), but a name like "localhost" resolves to 127.0.0.1 only at
# fetch time — we can't see it through the parsed URL string, so we
# reject by name as well.
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",  # GCP metadata service
}

# Hostnames forbidden because they're known phishing/malware vectors.
# Kept tiny on purpose — production would source this from a feed.
BLOCKED_DOMAINS = {
    "evil.com",
    "malware.example.com",
    "phishing.example.com",
}

# Characters that have no business appearing in a URL and that, if
# accepted, would let a caller smuggle a CRLF-injected header into the
# Location response by way of urlunparse round-tripping the bytes
# through the path/query. Reject before parsing.
_FORBIDDEN_URL_CHARS = ("\r", "\n", "\t", "\x00")


def is_blocked_domain(hostname: str | None) -> bool:
    """True if the hostname is on either blocklist (by name or by suffix)."""
    if hostname is None:
        return True
    h = hostname.lower()
    if h in BLOCKED_HOSTNAMES:
        return True
    if h in BLOCKED_DOMAINS:
        return True
    # Match parent suffixes too: `login.evil.com` and `a.b.evil.com`
    # are both blocked if `evil.com` is. This prevents the trivial
    # subdomain bypass the original blocklist permitted.
    for blocked in BLOCKED_DOMAINS:
        if h.endswith("." + blocked):
            return True
    return False


def is_internal_ip(hostname: str) -> bool:
    """True if `hostname` parses as an IP that points at our own network.

    Rejects the standard SSRF surface: loopback, RFC 1918 private,
    link-local (including the 169.254.169.254 cloud-metadata IPs),
    IPv6 ULAs, multicast, and anything `ipaddress` flags as reserved.
    """
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # Not an IP literal — let DNS resolve it at fetch time. We can't
        # do a name lookup here without trusting the lookup result.
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url(url: str) -> str:
    """Validate (length / scheme / blocklist / SSRF / control chars) and
    conservatively normalize.

    Normalization scope is intentionally narrow: per RFC 3986, scheme and
    host are case-insensitive (so we lowercase them), but path, query, and
    fragment are case-sensitive at the protocol level — `/User` and `/user`
    can resolve to different resources on the same server. The reference
    answer lowercases the entire URL, which can break case-sensitive paths
    (S3 keys, GitHub raw URLs, etc.); we deviate. See DECISIONS.md Stage 3.

    We also do NOT upgrade `http://` to `https://`: not every target speaks
    TLS on 443, and a forced upgrade would silently break those redirects.

    Post-review hardening (DECISIONS.md "Post-review fixes"):

    - Control chars rejected before parsing so they can't reach the
      Location header.
    - Userinfo (`user:pass@`) rejected — short links must not double as
      credential carriers, and an embedded credential makes the visible
      hostname misleading (`https://google.com@attacker.com`).
    - IP-literal hosts checked against the private/loopback/link-local
      ranges to block SSRF against the LAN and cloud metadata services.
    - Blocklist matches both the exact hostname and any subdomain of a
      blocked registrable domain.
    """
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"URL exceeds max length of {MAX_URL_LENGTH} characters")

    for ch in _FORBIDDEN_URL_CHARS:
        if ch in url:
            raise ValueError("URL contains forbidden control characters")

    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(
            f"Invalid scheme {parsed.scheme!r} — only {ALLOWED_SCHEMES} are allowed"
        )

    if not parsed.hostname:
        raise ValueError("Invalid URL — missing hostname")

    # Userinfo in URLs ("https://user:pass@host/") is technically valid
    # but is a phishing primitive — the visible hostname is hidden by
    # the @-prefix. Reject outright.
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded userinfo (user:pass@) are not allowed")

    if is_blocked_domain(parsed.hostname):
        raise ValueError(f"URL host {parsed.hostname!r} is on the blocklist")

    if is_internal_ip(parsed.hostname):
        raise ValueError(
            f"URL host {parsed.hostname!r} resolves to an internal / private network"
        )

    # Rebuild the netloc with host lowercased; preserve port (userinfo
    # was rejected above, so we don't need to re-emit it).
    netloc = parsed.hostname.lower()
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

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
