"""Camera source resolver supporting ONVIF discovery, dynamic RTSP URI extraction,
and environment variable substitution.
"""
from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlsplit, urlunsplit, quote
from typing import Any, Optional

import yaml

from ingestion.onvif.client import ONVIFCameraClient, ONVIFError
from ingestion.onvif.security import sanitize_url

logger = logging.getLogger("onvif.resolver")

ENV_VAR_REGEX = re.compile(r"\${([A-Za-z0-9_]+)}")

# In-memory cache for resolved ONVIF RTSP stream URIs: (host, port, username) -> (rtsp_url, metadata, timestamp)
_RESOLVER_CACHE: dict[str, tuple[str, dict[str, Any], float]] = {}


def clear_resolver_cache():
    """Clear in-memory cache of resolved ONVIF stream URIs."""
    _RESOLVER_CACHE.clear()


def expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR_NAME} placeholders in strings, dicts, or lists."""
    if isinstance(value, str):
        def repl(match):
            var_name = match.group(1)
            return os.getenv(var_name, "")
        return ENV_VAR_REGEX.sub(repl, value)

    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}

    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]

    return value


def inject_credentials_into_url(url: str, username: str, password: str) -> str:
    """Inject credentials into stream URL (RTSP, RTSPS, HTTP, HTTPS) if not already present, for cv2.VideoCapture."""
    if not username or not url:
        return url

    try:
        parts = urlsplit(url)
        if "@" in parts.netloc:
            # Credentials already present
            return url

        safe_user = quote(str(username), safe="")
        safe_pass = quote(str(password), safe="") if password else ""
        userinfo = f"{safe_user}:{safe_pass}" if safe_pass else safe_user
        new_netloc = f"{userinfo}@{parts.netloc}"
        return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return url


# Backward compatibility alias
inject_credentials_into_rtsp_url = inject_credentials_into_url


def resolve_camera_source(
    cam_config: dict[str, Any],
    default_rtsp_url: str = "0",
    cache_ttl: float = 300.0,
) -> tuple[str, dict[str, Any]]:
    """Resolve camera configuration into an active RTSP URI for OpenCV/FFmpeg ingestion.

    Supports:
    1. source.type == 'onvif' (probes ONVIF device/media service to extract RTSP URI)
    2. source.type == 'rtsp' (uses provided RTSP URL with env var substitution)
    3. legacy flat rtsp_url
    """
    if not cam_config or not isinstance(cam_config, dict):
        return default_rtsp_url, {}

    expanded = expand_env_vars(cam_config)
    source = expanded.get("source")

    # 1. Structured source definition
    if isinstance(source, dict):
        source_type = str(source.get("type", "")).lower()

        if source_type == "onvif":
            host = str(source.get("host", "")).strip()
            port = int(source.get("port", 80))
            username = str(source.get("username", ""))
            password = str(source.get("password", ""))
            profile_target = str(source.get("profile", "")).strip()

            if not host:
                logger.warning("ONVIF camera config missing host address: %s", cam_config.get("id"))
                return default_rtsp_url, {}

            cache_key = f"{host}:{port}:{username}:{profile_target}"
            now = time.time()
            if cache_key in _RESOLVER_CACHE:
                cached_url, cached_meta, ts = _RESOLVER_CACHE[cache_key]
                if (now - ts) < cache_ttl:
                    logger.debug("Using cached ONVIF RTSP URI for %s (%s)", host, sanitize_url(cached_url))
                    return cached_url, cached_meta

            logger.info("Resolving ONVIF RTSP stream from %s:%d (camera %s)...", host, port, cam_config.get("id"))
            client = ONVIFCameraClient(
                host=host,
                port=port,
                username=username,
                password=password,
                timeout=float(source.get("timeout", 4.0)),
            )

            probe = client.probe_camera()
            raw_rtsp = probe.get("rtsp_url", "")

            # If a specific profile was requested, query its stream URI
            if profile_target and probe.get("profiles"):
                for prof in probe["profiles"]:
                    if prof.get("token") == profile_target or prof.get("name") == profile_target:
                        try:
                            raw_rtsp = client.get_stream_uri(prof["token"])
                            probe["rtsp_url"] = raw_rtsp
                            probe["sanitized_rtsp_url"] = sanitize_url(raw_rtsp)
                        except Exception as exc:
                            logger.warning("Failed to get stream URI for profile %s: %s", profile_target, exc)
                        break

            if raw_rtsp:
                # Ensure RTSP URI has credentials for OpenCV VideoCapture
                effective_rtsp = inject_credentials_into_rtsp_url(raw_rtsp, username, password)
                _RESOLVER_CACHE[cache_key] = (effective_rtsp, probe, now)
                logger.info("Resolved ONVIF RTSP stream for %s -> %s", host, sanitize_url(effective_rtsp))
                return effective_rtsp, probe
            else:
                logger.warning("ONVIF camera at %s:%d returned no RTSP stream URI (status: %s, error: %s)",
                               host, port, probe.get("status"), probe.get("error"))
                fallback = str(expanded.get("rtsp_url", "")).strip() or default_rtsp_url
                return fallback, probe

        elif source_type in ("rtsp", "direct", "stream", "url", "http", "https"):
            raw_url = str(source.get("url", "") or source.get("stream_url", "")).strip()
            if not raw_url and source.get("host"):
                host = str(source.get("host", "")).strip()
                port = int(source.get("port", 554))
                path = str(source.get("path", "")).strip()
                if not path.startswith("/") and path:
                    path = f"/{path}"
                raw_url = f"rtsp://{host}:{port}{path}"
            username = str(source.get("username", "")).strip()
            password = str(source.get("password", "")).strip()
            effective_url = inject_credentials_into_url(raw_url, username, password)
            if effective_url:
                return effective_url, {"type": source_type, "sanitized_url": sanitize_url(effective_url)}

    # 2. Top-level stream_url or url
    for key in ("stream_url", "url"):
        if key in expanded and expanded[key]:
            raw_url = str(expanded[key]).strip()
            username = str(expanded.get("username", "")).strip()
            password = str(expanded.get("password", "")).strip()
            effective_url = inject_credentials_into_url(raw_url, username, password)
            if effective_url:
                stype = str(expanded.get("source_type", "direct")).lower()
                return effective_url, {"type": stype, "sanitized_url": sanitize_url(effective_url)}

    # 3. Top-level RTSP host/port/path
    if expanded.get("source_type") in ("rtsp", "direct") and expanded.get("host"):
        host = str(expanded.get("host", "")).strip()
        port = int(expanded.get("port", 554))
        path = str(expanded.get("path", "")).strip()
        if not path.startswith("/") and path:
            path = f"/{path}"
        raw_url = f"rtsp://{host}:{port}{path}"
        username = str(expanded.get("username", "")).strip()
        password = str(expanded.get("password", "")).strip()
        effective_url = inject_credentials_into_url(raw_url, username, password)
        if effective_url:
            return effective_url, {"type": str(expanded.get("source_type")), "sanitized_url": sanitize_url(effective_url)}

    # 4. Legacy top-level rtsp_url
    if "rtsp_url" in expanded:
        url_val = str(expanded["rtsp_url"]).strip()
        if url_val:
            return url_val, {"type": "legacy", "sanitized_url": sanitize_url(url_val)}

    return default_rtsp_url, {}


def find_camera_in_yaml(camera_id: str, yaml_path: str) -> Optional[dict[str, Any]]:
    """Look up a camera entry by ID inside a YAML configuration file."""
    if not os.path.exists(yaml_path):
        return None

    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            for cam in data.get("cameras", []):
                if cam.get("id") == camera_id or cam.get("camera_id") == camera_id:
                    return cam
    except Exception as exc:
        logger.warning("Error reading %s for camera %s: %s", yaml_path, camera_id, exc)
    return None
