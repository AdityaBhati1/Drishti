"""ONVIF SOAP client for device information, media profiles, and RTSP stream extraction.

Implements WS-Security UsernameToken authentication (PasswordDigest & PasswordText),
device capabilities discovery, profile parsing, and RTSP stream URI retrieval.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import logging
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Optional

import requests

from ingestion.onvif.security import sanitize_url, validate_service_url

logger = logging.getLogger("onvif.client")


class ONVIFError(Exception):
    """Base exception for ONVIF operations."""
    pass


class ONVIFAuthError(ONVIFError):
    """Raised when authentication fails (HTTP 401 or WS-Security fault)."""
    pass


class ONVIFConnectionError(ONVIFError):
    """Raised when an ONVIF camera is unreachable or connection times out."""
    pass


class ONVIFParseError(ONVIFError):
    """Raised when ONVIF XML response is malformed or missing critical schema elements."""
    pass


def _strip_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def build_wsse_security_header(
    username: str,
    password: str,
    use_digest: bool = True,
) -> str:
    """Build WS-Security Header XML string using ONVIF PasswordDigest or PasswordText.

    PasswordDigest = Base64(SHA-1(nonce + created + password))
    """
    if not username:
        return ""

    created_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if use_digest:
        nonce_bytes = os.urandom(16)
        nonce_b64 = base64.b64encode(nonce_bytes).decode("ascii")
        created_bytes = created_iso.encode("utf-8")
        password_bytes = password.encode("utf-8")

        digest_raw = hashlib.sha1(nonce_bytes + created_bytes + password_bytes).digest()
        password_digest = base64.b64encode(digest_raw).decode("ascii")

        return f"""<s:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
                   xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{password_digest}</wsse:Password>
        <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
        <wsu:Created>{created_iso}</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>
  </s:Header>"""
    else:
        # PasswordText
        return f"""<s:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{password}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </s:Header>"""


def wrap_soap_envelope(body_content: str, header_content: str = "") -> str:
    """Wraps body XML in a standard SOAP 1.2 envelope."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  {header_content}
  <s:Body>
    {body_content}
  </s:Body>
</s:Envelope>"""


class ONVIFCameraClient:
    """Client for executing ONVIF SOAP requests against a specific camera."""

    def __init__(
        self,
        host: str,
        port: int = 80,
        username: str = "",
        password: str = "",
        timeout: float = 4.0,
        use_digest: bool = True,
        device_service_path: str = "/onvif/device_service",
    ):
        self.host = host.strip()
        self.port = int(port)
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.use_digest = use_digest
        self.device_service_url = f"http://{self.host}:{self.port}{device_service_path}"
        self.media_service_url = ""

    def _send_soap(self, endpoint_url: str, body_xml: str, action: str = "") -> ET.Element:
        """Send SOAP envelope to endpoint, validate response, and return root XML Element."""
        if not validate_service_url(endpoint_url):
            raise ONVIFConnectionError(f"Refusing connection to untrusted or invalid URL: {sanitize_url(endpoint_url)}")

        header_xml = build_wsse_security_header(self.username, self.password, use_digest=self.use_digest)
        envelope = wrap_soap_envelope(body_xml, header_xml)

        headers = {
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"' if action else "application/soap+xml; charset=utf-8",
            "User-Agent": "CCTV-ONVIF-Client/2.0",
        }

        try:
            resp = requests.post(
                endpoint_url,
                data=envelope.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise ONVIFConnectionError(f"Connection timed out connecting to {sanitize_url(endpoint_url)}") from exc
        except requests.exceptions.RequestException as exc:
            raise ONVIFConnectionError(f"Network error connecting to {sanitize_url(endpoint_url)}: {exc}") from exc

        # Handle HTTP auth failures
        if resp.status_code in (401, 403):
            raise ONVIFAuthError(f"Authentication failed on {sanitize_url(endpoint_url)} (HTTP {resp.status_code})")

        # Parse XML response
        try:
            root = ET.fromstring(resp.content)
        except Exception as exc:
            raise ONVIFParseError(f"Malformed XML response from {sanitize_url(endpoint_url)}: {exc}") from exc

        # Check for SOAP Faults
        fault = None
        for elem in root.iter():
            if _strip_tag(elem.tag).lower() == "fault":
                fault = elem
                break

        if fault is not None:
            fault_text = ET.tostring(fault, encoding="utf-8", method="text").decode("utf-8", errors="ignore").strip()
            fault_lower = fault_text.lower()
            if any(term in fault_lower for term in ("auth", "security", "unauthorized", "notauthorized", "failedauthentication")):
                raise ONVIFAuthError(f"ONVIF Authentication Fault: {fault_text}")
            raise ONVIFError(f"ONVIF SOAP Fault: {fault_text}")

        return root

    def get_device_information(self) -> dict[str, str]:
        """Query TDS GetDeviceInformation for manufacturer, model, firmware, serial."""
        body = '<tds:GetDeviceInformation xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
        action = "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation"

        root = self._send_soap(self.device_service_url, body, action)

        info = {
            "manufacturer": "",
            "model": "",
            "firmware_version": "",
            "serial_number": "",
            "hardware_id": "",
        }

        for elem in root.iter():
            tag = _strip_tag(elem.tag).lower()
            val = (elem.text or "").strip()
            if tag == "manufacturer":
                info["manufacturer"] = val
            elif tag == "model":
                info["model"] = val
            elif tag == "firmwareversion":
                info["firmware_version"] = val
            elif tag == "serialnumber":
                info["serial_number"] = val
            elif tag == "hardwareid":
                info["hardware_id"] = val

        return info

    def get_capabilities(self) -> dict[str, str]:
        """Query TDS GetCapabilities to discover Media and Events service XAddrs."""
        body = '<tds:GetCapabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:Category>All</tds:Category></tds:GetCapabilities>'
        action = "http://www.onvif.org/ver10/device/wsdl/GetCapabilities"

        root = self._send_soap(self.device_service_url, body, action)

        caps = {
            "media_xaddr": "",
            "events_xaddr": "",
            "ptz_xaddr": "",
        }

        # Look for Media / XAddr
        for elem in root.iter():
            tag = _strip_tag(elem.tag).lower()
            if tag == "media":
                for child in elem.iter():
                    if _strip_tag(child.tag).lower() == "xaddr":
                        caps["media_xaddr"] = (child.text or "").strip()
                        break
            elif tag == "events":
                for child in elem.iter():
                    if _strip_tag(child.tag).lower() == "xaddr":
                        caps["events_xaddr"] = (child.text or "").strip()
                        break
            elif tag == "ptz":
                for child in elem.iter():
                    if _strip_tag(child.tag).lower() == "xaddr":
                        caps["ptz_xaddr"] = (child.text or "").strip()
                        break

        # Cache media service URL if discovered
        if caps["media_xaddr"]:
            self.media_service_url = caps["media_xaddr"]
        elif not self.media_service_url:
            # Fallback convention
            self.media_service_url = f"http://{self.host}:{self.port}/onvif/media_service"

        return caps

    def get_profiles(self) -> list[dict[str, Any]]:
        """Query TRT GetProfiles to discover available video encoder profiles."""
        if not self.media_service_url:
            self.get_capabilities()

        body = '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'
        action = "http://www.onvif.org/ver10/media/wsdl/GetProfiles"

        root = self._send_soap(self.media_service_url, body, action)
        profiles: list[dict[str, Any]] = []

        for elem in root.iter():
            if _strip_tag(elem.tag).lower() == "profiles":
                token = elem.attrib.get("token", "")
                name = ""
                encoding = ""
                width = None
                height = None
                fps = None

                for child in elem.iter():
                    tag = _strip_tag(child.tag).lower()
                    if tag == "name" and not name:
                        name = (child.text or "").strip()
                    elif tag == "encoding":
                        encoding = (child.text or "").strip()
                    elif tag == "width":
                        try:
                            width = int(child.text or "0")
                        except ValueError:
                            pass
                    elif tag == "height":
                        try:
                            height = int(child.text or "0")
                        except ValueError:
                            pass
                    elif tag in ("framelimit", "frameratelimit"):
                        try:
                            fps = int(child.text or "0")
                        except ValueError:
                            pass

                profiles.append({
                    "token": token,
                    "name": name or token,
                    "encoding": encoding,
                    "width": width,
                    "height": height,
                    "fps": fps,
                })

        return profiles

    def get_stream_uri(self, profile_token: str, protocol: str = "RTSP") -> str:
        """Query TRT GetStreamUri to retrieve RTSP URI for a specific profile token."""
        if not self.media_service_url:
            self.get_capabilities()

        body = f"""<trt:GetStreamUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
  <trt:StreamSetup>
    <tt:Stream>RTP-Unicast</tt:Stream>
    <tt:Transport>
      <tt:Protocol>{protocol}</tt:Protocol>
    </tt:Transport>
  </trt:StreamSetup>
  <trt:ProfileToken>{profile_token}</trt:ProfileToken>
</trt:GetStreamUri>"""
        action = "http://www.onvif.org/ver10/media/wsdl/GetStreamUri"

        root = self._send_soap(self.media_service_url, body, action)
        uri = ""

        for elem in root.iter():
            if _strip_tag(elem.tag).lower() == "uri":
                uri = (elem.text or "").strip()
                if uri:
                    break

        return uri

    def probe_camera(self) -> dict[str, Any]:
        """Execute full ONVIF metadata & RTSP stream URI discovery pipeline.

        Returns structured camera metadata. Never leaks plain-text passwords.
        """
        result: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "device_service_url": self.device_service_url,
            "manufacturer": "",
            "model": "",
            "firmware_version": "",
            "serial_number": "",
            "hardware_id": "",
            "media_service_url": "",
            "profiles": [],
            "rtsp_url": "",
            "sanitized_rtsp_url": "",
            "requires_auth": False,
            "status": "online",
            "error": None,
        }

        # 1. Device Information
        try:
            dev_info = self.get_device_information()
            result.update(dev_info)
        except ONVIFAuthError as exc:
            result["requires_auth"] = True
            result["status"] = "auth_failed"
            result["error"] = "Authentication failed. Provide valid ONVIF credentials."
            logger.info("ONVIF camera at %s:%d requires authentication", self.host, self.port)
            return result
        except ONVIFConnectionError as exc:
            result["status"] = "unreachable"
            result["error"] = str(exc)
            logger.warning("ONVIF camera at %s:%d unreachable: %s", self.host, self.port, exc)
            return result
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"Device info error: {exc}"
            logger.warning("ONVIF error querying %s:%d: %s", self.host, self.port, exc)
            return result

        # 2. Capabilities
        try:
            caps = self.get_capabilities()
            result["media_service_url"] = caps.get("media_xaddr", "")
        except Exception as exc:
            logger.debug("GetCapabilities non-fatal error on %s:%d: %s", self.host, self.port, exc)

        # 3. Profiles
        try:
            profiles = self.get_profiles()
            result["profiles"] = profiles
        except Exception as exc:
            logger.warning("Could not fetch media profiles from %s:%d: %s", self.host, self.port, exc)
            profiles = []

        # 4. Stream URI (primary profile)
        if profiles:
            primary_token = profiles[0].get("token", "")
            if primary_token:
                try:
                    raw_uri = self.get_stream_uri(primary_token)
                    result["rtsp_url"] = raw_uri
                    result["sanitized_rtsp_url"] = sanitize_url(raw_uri)
                except Exception as exc:
                    logger.warning("GetStreamUri failed for profile %s on %s:%d: %s", primary_token, self.host, self.port, exc)

        return result
