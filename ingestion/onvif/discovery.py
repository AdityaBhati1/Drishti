"""WS-Discovery (Web Services Dynamic Discovery) client for ONVIF cameras.

Implements multicast UDP probing and unicast response parsing to discover
ONVIF-compliant NetworkVideoTransmitters on local network segments.
"""
from __future__ import annotations

import logging
import re
import socket
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from typing import Any

from ingestion.onvif.security import validate_service_url

logger = logging.getLogger("onvif.discovery")

WS_DISCOVERY_MULTICAST_GROUP = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

WS_DISCOVERY_PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>urn:uuid:{message_id}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


def build_probe_envelope(message_id: str | None = None) -> str:
    """Construct a standard WS-Discovery Probe SOAP envelope."""
    msg_id = message_id or str(uuid.uuid4())
    return WS_DISCOVERY_PROBE_TEMPLATE.format(message_id=msg_id)


def _strip_tag(tag: str) -> str:
    """Return local XML tag without namespace."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_scopes(scopes_str: str) -> dict[str, str]:
    """Extract metadata (name, hardware, location, profile) from ONVIF scopes string."""
    metadata: dict[str, str] = {}
    if not scopes_str:
        return metadata

    tokens = scopes_str.strip().split()
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        # Format usually: onvif://www.onvif.org/{category}/{value}
        if "onvif.org/" in token:
            category_part = token.split("onvif.org/", 1)[-1]
            if "/" in category_part:
                category, value = category_part.split("/", 1)
                metadata[category] = value
        elif "/" in token:
            # Fallback for manufacturer-specific scopes
            parts = token.rsplit("/", 1)
            metadata[parts[0]] = parts[1]
    return metadata


def parse_probe_matches(xml_content: str | bytes, sender_addr: tuple[str, int] | None = None) -> list[dict[str, Any]]:
    """Parse WS-Discovery ProbeMatches SOAP XML response.

    Returns a list of device discovery dictionaries.
    Gracefully tolerates malformed XML, missing elements, and namespace variations.
    """
    devices: list[dict[str, Any]] = []
    if not xml_content:
        return devices

    try:
        if isinstance(xml_content, str):
            xml_bytes = xml_content.encode("utf-8")
        else:
            xml_bytes = xml_content

        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        logger.warning("Failed to parse WS-Discovery XML packet from %s: %s", sender_addr, exc)
        return devices

    # Locate all ProbeMatch elements across namespaces
    match_elements = []
    for elem in root.iter():
        if _strip_tag(elem.tag).lower() == "probematch":
            match_elements.append(elem)

    for match in match_elements:
        endpoint_ref = ""
        xaddrs_raw = ""
        scopes_raw = ""
        types_raw = ""

        for child in match.iter():
            tag_name = _strip_tag(child.tag).lower()
            if tag_name == "address":
                endpoint_ref = (child.text or "").strip()
            elif tag_name == "xaddrs":
                xaddrs_raw = (child.text or "").strip()
            elif tag_name == "scopes":
                scopes_raw = (child.text or "").strip()
            elif tag_name == "types":
                types_raw = (child.text or "").strip()

        # Parse XAddrs list
        xaddrs_list = [addr.strip() for addr in xaddrs_raw.split() if addr.strip()]
        valid_xaddrs = [addr for addr in xaddrs_list if validate_service_url(addr)]

        # Determine primary device service URL
        primary_endpoint = valid_xaddrs[0] if valid_xaddrs else (xaddrs_list[0] if xaddrs_list else "")

        device_ip = sender_addr[0] if sender_addr else ""
        device_port = 80
        if primary_endpoint:
            try:
                parsed_url = urlsplit(primary_endpoint)
                if parsed_url.hostname:
                    device_ip = parsed_url.hostname
                if parsed_url.port:
                    device_port = parsed_url.port
                elif parsed_url.scheme.lower() == "https":
                    device_port = 443
            except Exception:
                pass

        scope_meta = parse_scopes(scopes_raw)
        name = scope_meta.get("name", "")
        hardware = scope_meta.get("hardware", "")
        location = scope_meta.get("location", "")

        devices.append({
            "endpoint_reference": endpoint_ref,
            "device_ip": device_ip,
            "device_port": device_port,
            "onvif_endpoint": primary_endpoint,
            "all_endpoints": valid_xaddrs or xaddrs_list,
            "types": types_raw,
            "scopes": scopes_raw.split() if scopes_raw else [],
            "name": name,
            "hardware": hardware,
            "location": location,
            "source_address": f"{sender_addr[0]}:{sender_addr[1]}" if sender_addr else "",
        })

    return devices


class WSDiscovery:
    """Client for discovering ONVIF devices via WS-Discovery over UDP multicast."""

    def __init__(
        self,
        multicast_group: str = WS_DISCOVERY_MULTICAST_GROUP,
        port: int = WS_DISCOVERY_PORT,
    ):
        self.multicast_group = multicast_group
        self.port = port

    def discover(self, timeout: float = 2.5) -> list[dict[str, Any]]:
        """Broadcast WS-Discovery probe packet and gather responses until timeout."""
        discovered: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Multicast TTL: 2 hops for local subnet
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(timeout)

            # Bind to all interfaces on an ephemeral port
            try:
                sock.bind(("", 0))
            except Exception as exc:
                logger.warning("Could not bind UDP socket for discovery: %s", exc)
                return []

            probe_msg = build_probe_envelope().encode("utf-8")
            logger.info("Broadcasting WS-Discovery Probe to %s:%d (timeout=%.1fs)...", self.multicast_group, self.port, timeout)

            sock.sendto(probe_msg, (self.multicast_group, self.port))

            # Listen loop
            while True:
                try:
                    data, addr = sock.recvfrom(65535)
                    matches = parse_probe_matches(data, sender_addr=addr)
                    for dev in matches:
                        # Deduplicate by endpoint_reference or IP:port
                        key = dev.get("endpoint_reference") or f"{dev.get('device_ip')}:{dev.get('device_port')}"
                        if key and key not in seen_keys:
                            seen_keys.add(key)
                            discovered.append(dev)
                            logger.info("Discovered ONVIF device at %s:%d (%s)", dev.get("device_ip"), dev.get("device_port"), dev.get("name") or "Unnamed")
                except socket.timeout:
                    break
                except Exception as exc:
                    logger.debug("Exception receiving discovery datagram: %s", exc)
                    break

        except Exception as exc:
            logger.warning("WS-Discovery network error: %s", exc)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        return discovered
