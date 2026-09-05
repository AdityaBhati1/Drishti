"""ONVIF Camera Discovery and RTSP Stream Integration module."""

from ingestion.onvif.security import (
    sanitize_url,
    sanitize_camera_dict,
    validate_service_url,
    validate_host_and_port,
)
from ingestion.onvif.discovery import (
    WSDiscovery,
    build_probe_envelope,
    parse_probe_matches,
    parse_scopes,
)
from ingestion.onvif.client import (
    ONVIFCameraClient,
    ONVIFError,
    ONVIFAuthError,
    ONVIFConnectionError,
    ONVIFParseError,
    build_wsse_security_header,
)
from ingestion.onvif.resolver import (
    resolve_camera_source,
    expand_env_vars,
    find_camera_in_yaml,
)

__all__ = [
    "sanitize_url",
    "sanitize_camera_dict",
    "validate_service_url",
    "validate_host_and_port",
    "WSDiscovery",
    "build_probe_envelope",
    "parse_probe_matches",
    "parse_scopes",
    "ONVIFCameraClient",
    "ONVIFError",
    "ONVIFAuthError",
    "ONVIFConnectionError",
    "ONVIFParseError",
    "build_wsse_security_header",
    "resolve_camera_source",
    "expand_env_vars",
    "find_camera_in_yaml",
]
