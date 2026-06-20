"""Outbound-URL safety checks (SSRF guardrail).

Source/action connect endpoints take a ``base_url`` from the request body and then make
server-side HTTP calls to it. Without a check, an authenticated caller could point those at the
cloud metadata endpoint (``169.254.169.254``) or a loopback service and exfiltrate the response.

Self-hosters legitimately point at LAN addresses (``192.168.x.x`` / ``10.x.x.x``), so the block
list is intentionally **narrow**: loopback, link-local (which covers the metadata IP), and
unspecified. Private LAN ranges are allowed on purpose. Note: only literal-IP and ``localhost``
hosts are inspected here — a hostname that *resolves* to a blocked range (DNS rebinding) is not
caught; that would need resolution at request time and is out of scope for this guard.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_BLOCKED_HOSTNAMES = {"localhost"}
_ALLOWED_SCHEMES = {"http", "https"}


def validate_external_url(url: str) -> str:
    """Return ``url`` unchanged if it's safe to fetch server-side, else raise ``ValueError``."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError("URL must use http or https")
    host = parsed.hostname
    if not host:
        raise ValueError("URL must include a host")
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError("URL host is not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # a hostname, not a literal IP — allowed (see module note)
    if ip is not None and (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
        raise ValueError("URL host points at a blocked address range")
    return url
