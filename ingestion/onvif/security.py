"""Security and credential sanitization utilities for ONVIF and RTSP camera streams.

Guards against credential leakage and Server-Side Request Forgery (SSRF).
"""
from __future__ import annotations

import copy
import ipaddress
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Known sensitive keys in camera config and API dictionaries
SENSITIVE_KEYS = {
    "password",
    "pass",
    "pwd",
    "secret",
    "token",
    "auth_token",
    "access_token",
    "credentials",
}

# Permitted camera stream URI schemes
ALLOWED_STREAM_SCHEMES = ("rtsp", "rtsps", "http", "https")

# Blocked IP addresses / domains for SSRF mitigation (cloud metadata endpoints, unspecified address)
BLOCKED_HOSTS = {
    "169.254.169.254",  # AWS/GCP/Azure link-local metadata
    "metadata.google.internal",
    "0.0.0.0",
}

URL_CREDENTIAL_REGEX = re.compile(r"://([^:/]+):([^@]+)@")


def sanitize_url(url: str | None, replacement: str = "***") -> str:
    """Mask credentials (username:password) in any URL (RTSP, HTTP, etc.).

    Example:
        rtsp://admin:secret123@192.168.1.50:554/live -> rtsp://admin:***@192.168.1.50:554/live
    """
    if not url:
        return "" if url is not None else ""

    url_str = str(url).strip()
    if not url_str:
        return ""

    try:
        parts = urlsplit(url_str)
        if not parts.netloc:
            # Fallback regex for non-standard URI formats
            return URL_CREDENTIAL_REGEX.sub(rf"://\1:{replacement}@", url_str)

        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostport = netloc.rsplit("@", 1)
            if ":" in userinfo:
                username, _ = userinfo.split(":", 1)
                new_userinfo = f"{username}:{replacement}"
            else:
                new_userinfo = f"{replacement}"
            netloc = f"{new_userinfo}@{hostport}"

        # Also sanitize sensitive query parameters if present
        query = parts.query
        if query:
            q_pairs = parse_qsl(query, keep_blank_values=True)
            sanitized_pairs = []
            for k, v in q_pairs:
                if k.lower() in SENSITIVE_KEYS:
                    sanitized_pairs.append((k, replacement))
                else:
                    sanitized_pairs.append((k, v))
            query = urlencode(sanitized_pairs)

        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except Exception:
        # Resilient regex fallback
        return URL_CREDENTIAL_REGEX.sub(rf"://\1:{replacement}@", url_str)


def sanitize_camera_dict(data: Any, replacement: str = "***") -> Any:
    """Deep-sanitizes dictionaries containing camera information.

    Masks passwords and sanitizes embedded URLs without mutating the original object.
    """
    if data is None:
        return None

    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(sens in k_lower for sens in SENSITIVE_KEYS):
                sanitized[k] = replacement
            elif isinstance(v, str) and ("://" in v or "rtsp:" in v):
                sanitized[k] = sanitize_url(v, replacement=replacement)
            elif isinstance(v, (dict, list)):
                sanitized[k] = sanitize_camera_dict(v, replacement=replacement)
            else:
                sanitized[k] = v
        return sanitized

    if isinstance(data, list):
        return [sanitize_camera_dict(item, replacement=replacement) for item in data]

    if isinstance(data, str) and ("://" in data or "rtsp:" in data):
        return sanitize_url(data, replacement=replacement)

    return copy.deepcopy(data)


def validate_service_url(url: str, allowed_schemes: tuple[str, ...] = ("http", "https")) -> bool:
    """Validate URL structure, scheme, and guards against SSRF attacks.

    Rejects non-allowed schemes, empty hosts, invalid ports, and cloud metadata addresses.
    """
    if not url or not isinstance(url, str):
        return False

    url_str = url.strip()
    try:
        parts = urlsplit(url_str)
        if not parts.scheme or parts.scheme.lower() not in allowed_schemes:
            return False

        hostname = parts.hostname
        if not hostname:
            return False

        hostname_lower = hostname.lower()
        if hostname_lower in BLOCKED_HOSTS:
            return False

        # Check for link-local metadata IP address (169.254.x.x)
        try:
            ip_obj = ipaddress.ip_address(hostname_lower)
            if ip_obj.is_link_local:
                return False
            if ip_obj.is_unspecified:  # 0.0.0.0
                return False
        except ValueError:
            # Hostname is not a raw IP address (e.g. 'cam-gate-01.local')
            pass

        # Validate port
        if parts.port is not None:
            if parts.port < 1 or parts.port > 65535:
                return False

        return True
    except Exception:
        return False


def validate_stream_url(url: str, allowed_schemes: tuple[str, ...] = ALLOWED_STREAM_SCHEMES) -> bool:
    """Validate a camera stream URL (RTSP, RTSPS, HTTP, HTTPS) with SSRF and link-local protection."""
    return validate_service_url(url, allowed_schemes=allowed_schemes)


def validate_host_and_port(host: str, port: int | str = 80) -> tuple[str, int]:
    """Validate host address and port number.

    Raises ValueError if invalid.
    """
    if not host or not isinstance(host, str):
        raise ValueError("Host cannot be empty")

    host_clean = host.strip()
    if not host_clean:
        raise ValueError("Host cannot be empty")

    host_lower = host_clean.lower()
    if host_lower in BLOCKED_HOSTS:
        raise ValueError(f"Host '{host_clean}' is not permitted (SSRF protection)")

    try:
        ip_obj = ipaddress.ip_address(host_lower)
        if ip_obj.is_link_local:
            raise ValueError(f"Host '{host_clean}' is link-local metadata (SSRF protection)")
        if ip_obj.is_unspecified:
            raise ValueError(f"Host '{host_clean}' is unspecified 0.0.0.0 (SSRF protection)")
    except ValueError as e:
        if "is link-local" in str(e) or "is unspecified" in str(e):
            raise
        # Not a raw IP; hostname format check
        if any(char in host_clean for char in " \t\r\n/\\:;?#@"):
            raise ValueError(f"Invalid characters in host '{host_clean}'")

    try:
        port_int = int(port)
        if port_int < 1 or port_int > 65535:
            raise ValueError(f"Port {port_int} out of valid range (1-65535)")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid port: {port}") from exc

    return host_clean, port_int
