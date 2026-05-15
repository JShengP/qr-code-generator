import ipaddress
import re
import unicodedata
from urllib.parse import urlparse, urlunparse

MAX_URL_LENGTH = 2048

# Two disjoint sets:
#   URL_SCHEMES — the scanner opens a browser, our `/r/{token}` 302
#                 returns Location, browser follows. Full host /
#                 SSRF / blocklist / normalization apply.
#   URI_SCHEMES — the OS handler (mail app, dialer, SMS, maps) picks
#                 up the redirect on the device side. There's no host
#                 in the http sense, so SSRF / blocklist don't apply;
#                 minimal per-scheme sanity checks only.
URL_SCHEMES = ("http", "https")
URI_SCHEMES = ("mailto", "tel", "sms", "geo")
ALLOWED_SCHEMES = URL_SCHEMES + URI_SCHEMES

# Minimal validators for the URI schemes. Goal is to catch obvious
# junk (`mailto:` with no `@`, `tel:` with no digits) without
# pretending to do RFC-perfect parsing — the device's handler does
# the real validation when it tries to act on the value.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d().\-\s*#]+$")
_GEO_RE = re.compile(r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?(?:,-?\d+(?:\.\d+)?)?$")

# Cyrillic + Greek glyphs that visually fold to ASCII letters. Sourced
# from the Unicode TR39 confusables data narrowed to the high-frequency
# attack subset; the broader confusables database is several thousand
# entries but the long tail is rarely used in practice. Production
# systems should pull from `confusable_homoglyphs` or a hosted feed.
#
# The map's key is the Unicode glyph; the value is the Latin letter it
# fools the eye into thinking it is. Hostnames are lowercased before
# the map is applied, so we only need lowercase entries.
_HOMOGRAPH_MAP = {
    # Cyrillic → Latin
    "а": "a",  # а
    "е": "e",  # е
    "о": "o",  # о
    "р": "p",  # р
    "с": "c",  # с
    "у": "y",  # у
    "х": "x",  # х
    "ѕ": "s",  # ѕ
    "і": "i",  # і
    "ј": "j",  # ј
    # Greek → Latin
    "α": "a",  # α
    "ε": "e",  # ε
    "ο": "o",  # ο
    "ρ": "p",  # ρ
    "τ": "t",  # τ
    "υ": "y",  # υ
    "χ": "x",  # χ
    # Latin lookalikes
    "ı": "i",  # ı (dotless i)
}

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


def _canonical_hostname(hostname: str) -> str:
    """Canonicalize a hostname for blocklist comparison.

    Two transformations are applied:

    1. **Punycode decode.** Labels starting with ``xn--`` are decoded
       back to their Unicode form via stdlib ``encodings.idna``. This
       collapses ``xn--vil-7ka.com`` (the punycode of ``еvil.com``,
       where ``е`` is Cyrillic U+0435) into ``еvil.com``, the form a
       human reading the QR-rendered short URL would see.

    2. **Homograph fold.** Each Cyrillic/Greek glyph that visually
       passes for an ASCII letter is replaced by that ASCII letter
       through ``_HOMOGRAPH_MAP``. So ``еvil.com`` (Cyrillic ``е``)
       collapses to ``evil.com``.

    The returned string is the "skeleton" used to compare against the
    blocklist. Two hostnames that look identical to a human eye should
    map to the same skeleton; a legitimate IDN like ``日本.jp`` passes
    through unchanged because none of its characters appear in the map.
    """
    if not hostname:
        return hostname
    # Step 1: decode any punycode labels.
    decoded_labels = []
    for label in hostname.split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except (UnicodeError, ValueError):
                # Malformed punycode — leave the raw form alone and let
                # the strict-match branch fail loudly.
                pass
        decoded_labels.append(label)
    decoded = ".".join(decoded_labels)
    # Step 2: NFKC normalization (folds compatibility characters) +
    # homograph map. NFKC collapses things like ﬂ → fl, full-width
    # digits, etc.; the manual map covers the cross-script lookalikes
    # NFKC doesn't touch.
    normalized = unicodedata.normalize("NFKC", decoded.lower())
    return "".join(_HOMOGRAPH_MAP.get(c, c) for c in normalized)


def is_blocked_domain(hostname: str | None) -> bool:
    """True if the hostname is on either blocklist.

    Checks both the literal hostname AND its canonical (homograph-
    folded, punycode-decoded) form, so neither ``еvil.com`` (Cyrillic)
    nor ``xn--vil-7ka.com`` (punycode of the same) bypass an entry like
    ``evil.com`` in the blocklist.
    """
    if hostname is None:
        return True
    # Build the set of forms to check: the raw hostname (lowercased)
    # and its canonical/skeleton form. Using a set means a hostname
    # that's already ASCII only costs one comparison pass.
    forms = {hostname.lower(), _canonical_hostname(hostname)}
    for h in forms:
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


def _validate_uri_payload(scheme: str, parsed) -> None:
    """Minimal sanity check for mailto / tel / sms / geo payloads.
    Raises ValueError on obvious junk.

    For these schemes the bulk of the data sits in `parsed.path`
    (e.g. `mailto:foo@bar.com` → path=`foo@bar.com`). `parsed.query`
    is the optional `?body=...` / `?subject=...` etc.
    """
    body = parsed.path
    if not body:
        raise ValueError(f"{scheme}: scheme requires a non-empty value after the colon")

    if scheme == "mailto":
        # mailto: can carry multiple comma-separated addresses; check
        # the first one looks like an email and trust the user for the
        # rest (the email client will reject malformed ones at send time).
        first = body.split(",", 1)[0].strip()
        if not _EMAIL_RE.match(first):
            raise ValueError(
                f"mailto: target {first!r} doesn't look like an email address"
            )
    elif scheme in ("tel", "sms"):
        if not _PHONE_RE.match(body):
            raise ValueError(
                f"{scheme}: target {body!r} doesn't look like a phone number "
                "(digits, +, -, spaces, parens only)"
            )
    elif scheme == "geo":
        # `geo:lat,lon` or `geo:lat,lon,alt`; optional `?z=zoom` query
        # is preserved separately.
        if not _GEO_RE.match(body):
            raise ValueError(
                f"geo: target {body!r} must be `lat,lon` (decimal degrees)"
            )


def validate_url(url: str) -> str:
    """Validate (length / scheme / blocklist / SSRF / control chars) and
    conservatively normalize. Returns the normalized form.

    Two scheme classes are accepted:

    **URL schemes** (`http`, `https`) — the scanner opens a browser
    that follows our redirect. Subject to:
      - hostname required + not on `BLOCKED_HOSTNAMES`/`BLOCKED_DOMAINS`
      - hostname not resolving to a private / loopback / metadata IP
      - userinfo (`user:pass@`) rejected
      - path/query case preserved; only scheme + host lowercased
      - bare-root trailing slash collapsed when no query/fragment

    **URI schemes** (`mailto`, `tel`, `sms`, `geo`) — the OS handler
    (mail app / dialer / SMS / maps) acts on the value device-side.
    These have no http-style host, so SSRF / blocklist don't apply;
    we only do a minimal per-scheme shape check (see
    `_validate_uri_payload`). The redirect handler still emits these
    as the Location header — modern browsers route them to the OS
    handler.

    Normalization scope (for URL schemes) is intentionally narrow:
    per RFC 3986, scheme and host are case-insensitive, but path,
    query, and fragment are case-sensitive at the protocol level
    (`/User` and `/user` can resolve to different resources on the
    same server). See DECISIONS.md Stage 3 for the full rationale.
    """
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"URL exceeds max length of {MAX_URL_LENGTH} characters")

    for ch in _FORBIDDEN_URL_CHARS:
        if ch in url:
            raise ValueError("URL contains forbidden control characters")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"Invalid scheme {parsed.scheme!r} — allowed: {', '.join(ALLOWED_SCHEMES)}"
        )

    # ---- URI schemes (mailto / tel / sms / geo) -----------------
    if scheme in URI_SCHEMES:
        _validate_uri_payload(scheme, parsed)
        # No netloc, no host normalization — preserve the rest as-is.
        return urlunparse(
            (scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )

    # ---- URL schemes (http / https) -----------------------------
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
        (scheme, netloc, path, parsed.params, parsed.query, parsed.fragment)
    )
