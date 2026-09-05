"""Comprehensive unit and contract tests for ONVIF camera discovery and RTSP integration.

Validates WS-Discovery parsing, SOAP client operations, WS-Security digest,
SSRF security guards, credential sanitization, configuration compatibility,
error handling (auth failures, timeouts, malformed XML), and Central API endpoints.
"""
from __future__ import annotations

import base64
import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"

from central import main as central
from ingestion.config import append_camera_config, load_config
from ingestion.onvif.client import (
    ONVIFAuthError,
    ONVIFCameraClient,
    ONVIFConnectionError,
    ONVIFError,
    ONVIFParseError,
    build_wsse_security_header,
)
from ingestion.onvif.discovery import (
    WSDiscovery,
    build_probe_envelope,
    parse_probe_matches,
    parse_scopes,
)
from ingestion.onvif.resolver import (
    expand_env_vars,
    find_camera_in_yaml,
    inject_credentials_into_rtsp_url,
    resolve_camera_source,
)
from ingestion.onvif.security import (
    sanitize_camera_dict,
    sanitize_url,
    validate_host_and_port,
    validate_service_url,
)

# Sample WS-Discovery ProbeMatches SOAP XML
SAMPLE_PROBEMATCH_XML = """<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                   xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
                   xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <SOAP-ENV:Header>
    <wsa:MessageID>urn:uuid:11112222-3333-4444-5555-666677778888</wsa:MessageID>
    <wsa:RelatesTo>urn:uuid:probe-request-id-12345</wsa:RelatesTo>
    <wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:44a1e944-77a2-4a48-8123-abcdef123456</wsa:Address>
        </wsa:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>
          onvif://www.onvif.org/type/video_encoder
          onvif://www.onvif.org/Profile/Streaming
          onvif://www.onvif.org/name/BorderThermalCam
          onvif://www.onvif.org/hardware/ITC-9000
          onvif://www.onvif.org/location/NorthPerimeter
        </d:Scopes>
        <d:XAddrs>http://192.168.1.120:80/onvif/device_service</d:XAddrs>
        <d:MetadataVersion>1</d:MetadataVersion>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

# Sample TDS GetDeviceInformationResponse XML
SAMPLE_DEVICE_INFO_XML = """<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <SOAP-ENV:Body>
    <tds:GetDeviceInformationResponse>
      <tds:Manufacturer>ApexSurveillance</tds:Manufacturer>
      <tds:Model>ITC-9000-HD</tds:Model>
      <tds:FirmwareVersion>V4.2.1-build2025</tds:FirmwareVersion>
      <tds:SerialNumber>APX-99887766</tds:SerialNumber>
      <tds:HardwareId>HW-001-REV-B</tds:HardwareId>
    </tds:GetDeviceInformationResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

# Sample TDS GetCapabilitiesResponse XML
SAMPLE_CAPABILITIES_XML = """<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <tds:GetCapabilitiesResponse>
      <tds:Capabilities>
        <tt:Device>
          <tt:XAddr>http://192.168.1.120:80/onvif/device_service</tt:XAddr>
        </tt:Device>
        <tt:Media>
          <tt:XAddr>http://192.168.1.120:80/onvif/media_service</tt:XAddr>
        </tt:Media>
        <tt:Events>
          <tt:XAddr>http://192.168.1.120:80/onvif/event_service</tt:XAddr>
        </tt:Events>
      </tds:Capabilities>
    </tds:GetCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

# Sample TRT GetProfilesResponse XML
SAMPLE_PROFILES_XML = """<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="Profile_MainStream" fixed="true">
        <tt:Name>MainStream</tt:Name>
        <tt:VideoEncoderConfiguration token="VEC_001">
          <tt:Name>VEC_Main</tt:Name>
          <tt:Encoding>H264</tt:Encoding>
          <tt:Resolution>
            <tt:Width>1920</tt:Width>
            <tt:Height>1080</tt:Height>
          </tt:Resolution>
          <tt:RateControl>
            <tt:FrameRateLimit>30</tt:FrameRateLimit>
          </tt:RateControl>
        </tt:VideoEncoderConfiguration>
      </trt:Profiles>
      <trt:Profiles token="Profile_SubStream" fixed="true">
        <tt:Name>SubStream</tt:Name>
        <tt:VideoEncoderConfiguration token="VEC_002">
          <tt:Name>VEC_Sub</tt:Name>
          <tt:Encoding>H264</tt:Encoding>
          <tt:Resolution>
            <tt:Width>640</tt:Width>
            <tt:Height>360</tt:Height>
          </tt:Resolution>
          <tt:RateControl>
            <tt:FrameRateLimit>15</tt:FrameRateLimit>
          </tt:RateControl>
        </tt:VideoEncoderConfiguration>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

# Sample TRT GetStreamUriResponse XML
SAMPLE_STREAM_URI_XML = """<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetStreamUriResponse>
      <trt:MediaUri>
        <tt:Uri>rtsp://192.168.1.120:554/live/ch0</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
        <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
        <tt:Timeout>PT30S</tt:Timeout>
      </trt:MediaUri>
    </trt:GetStreamUriResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

# Sample SOAP Fault XML (Auth Failure)
SAMPLE_SOAP_AUTH_FAULT_XML = """<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
  <SOAP-ENV:Body>
    <SOAP-ENV:Fault>
      <SOAP-ENV:Code>
        <SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value>
        <SOAP-ENV:Subcode>
          <SOAP-ENV:Value>wsse:FailedAuthentication</SOAP-ENV:Value>
        </SOAP-ENV:Subcode>
      </SOAP-ENV:Code>
      <SOAP-ENV:Reason>
        <SOAP-ENV:Text xml:lang="en">The security token could not be authenticated or authorized</SOAP-ENV:Text>
      </SOAP-ENV:Reason>
    </SOAP-ENV:Fault>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


class TestONVIFSecurityAndSanitization(unittest.TestCase):
    """Test suite for credential sanitization and SSRF validation."""

    def test_sanitize_rtsp_url(self):
        url = "rtsp://admin:P@ssw0rd123!@192.168.1.100:554/live/ch0"
        sanitized = sanitize_url(url)
        self.assertEqual(sanitized, "rtsp://admin:***@192.168.1.100:554/live/ch0")
        self.assertNotIn("P@ssw0rd123!", sanitized)

    def test_sanitize_url_without_credentials(self):
        url = "rtsp://192.168.1.100:554/live/ch0"
        self.assertEqual(sanitize_url(url), url)

    def test_sanitize_url_with_query_params(self):
        url = "http://192.168.1.50/stream?token=SuperSecretToken&channel=1"
        sanitized = sanitize_url(url)
        self.assertNotIn("SuperSecretToken", sanitized)
        self.assertIn("token=%2A%2A%2A", sanitized)

    def test_sanitize_camera_dict_nested(self):
        cam_dict = {
            "id": "cam-test-01",
            "name": "Front Gate",
            "source": {
                "type": "onvif",
                "host": "192.168.1.100",
                "port": 80,
                "username": "admin",
                "password": "MySuperSecretPassword",
            },
            "rtsp_url": "rtsp://admin:MySuperSecretPassword@192.168.1.100:554/stream",
        }
        sanitized = sanitize_camera_dict(cam_dict)

        # Original untouched
        self.assertEqual(cam_dict["source"]["password"], "MySuperSecretPassword")

        # Sanitized output masks password
        self.assertEqual(sanitized["source"]["password"], "***")
        self.assertEqual(sanitized["source"]["username"], "admin")
        self.assertNotIn("MySuperSecretPassword", sanitized["rtsp_url"])
        self.assertIn("rtsp://admin:***@192.168.1.100:554/stream", sanitized["rtsp_url"])

    def test_ssrf_blocks_cloud_metadata(self):
        self.assertFalse(validate_service_url("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(validate_service_url("http://0.0.0.0:80/"))
        self.assertFalse(validate_service_url("http://metadata.google.internal/computeMetadata/v1/"))

    def test_ssrf_allows_local_ips(self):
        self.assertTrue(validate_service_url("http://192.168.1.50:80/onvif/device_service"))
        self.assertTrue(validate_service_url("http://10.0.0.5:8000/onvif/device_service"))
        self.assertTrue(validate_service_url("https://172.16.1.10/onvif/device_service"))

    def test_ssrf_blocks_dangerous_schemes(self):
        self.assertFalse(validate_service_url("file:///etc/passwd"))
        self.assertFalse(validate_service_url("gopher://127.0.0.1:6379/_flushall"))

    def test_validate_host_and_port(self):
        host, port = validate_host_and_port("192.168.1.50", 8080)
        self.assertEqual(host, "192.168.1.50")
        self.assertEqual(port, 8080)

        with self.assertRaises(ValueError):
            validate_host_and_port("169.254.169.254", 80)
        with self.assertRaises(ValueError):
            validate_host_and_port("192.168.1.50", 99999)
        with self.assertRaises(ValueError):
            validate_host_and_port("", 80)


class TestWSDiscoveryParsing(unittest.TestCase):
    """Test suite for WS-Discovery probe generation and response parsing."""

    def test_build_probe_envelope(self):
        probe = build_probe_envelope(message_id="test-uuid-1234")
        self.assertIn("urn:uuid:test-uuid-1234", probe)
        self.assertIn("dn:NetworkVideoTransmitter", probe)
        self.assertIn("http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe", probe)

    def test_parse_scopes(self):
        scopes = (
            "onvif://www.onvif.org/type/video_encoder "
            "onvif://www.onvif.org/name/BorderThermalCam "
            "onvif://www.onvif.org/hardware/ITC-9000 "
            "onvif://www.onvif.org/location/NorthPerimeter"
        )
        meta = parse_scopes(scopes)
        self.assertEqual(meta.get("name"), "BorderThermalCam")
        self.assertEqual(meta.get("hardware"), "ITC-9000")
        self.assertEqual(meta.get("location"), "NorthPerimeter")

    def test_parse_probe_matches_success(self):
        devices = parse_probe_matches(SAMPLE_PROBEMATCH_XML, sender_addr=("192.168.1.120", 3702))
        self.assertEqual(len(devices), 1)
        dev = devices[0]
        self.assertEqual(dev["endpoint_reference"], "urn:uuid:44a1e944-77a2-4a48-8123-abcdef123456")
        self.assertEqual(dev["device_ip"], "192.168.1.120")
        self.assertEqual(dev["device_port"], 80)
        self.assertEqual(dev["onvif_endpoint"], "http://192.168.1.120:80/onvif/device_service")
        self.assertEqual(dev["name"], "BorderThermalCam")
        self.assertEqual(dev["hardware"], "ITC-9000")

    def test_parse_probe_matches_malformed_xml(self):
        # Must not throw; returns empty list
        devices = parse_probe_matches("<incomplete_xml><probe", sender_addr=("192.168.1.1", 3702))
        self.assertEqual(devices, [])


class TestONVIFClientAndSOAP(unittest.TestCase):
    """Test suite for ONVIF SOAP requests, responses, and authentication."""

    def test_ws_security_password_digest(self):
        header_xml = build_wsse_security_header("admin", "secret", use_digest=True)
        self.assertIn("UsernameToken", header_xml)
        self.assertIn("<wsse:Username>admin</wsse:Username>", header_xml)
        self.assertIn("PasswordDigest", header_xml)
        self.assertIn("<wsse:Nonce", header_xml)
        self.assertIn("<wsu:Created", header_xml)

    @patch("requests.post")
    def test_get_device_information_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = SAMPLE_DEVICE_INFO_XML.encode("utf-8")
        mock_post.return_value = mock_resp

        client = ONVIFCameraClient("192.168.1.120", 80, "admin", "pass")
        info = client.get_device_information()

        self.assertEqual(info["manufacturer"], "ApexSurveillance")
        self.assertEqual(info["model"], "ITC-9000-HD")
        self.assertEqual(info["firmware_version"], "V4.2.1-build2025")
        self.assertEqual(info["serial_number"], "APX-99887766")
        self.assertEqual(info["hardware_id"], "HW-001-REV-B")

    @patch("requests.post")
    def test_get_capabilities_and_profiles(self, mock_post):
        # 1st call: GetCapabilities, 2nd call: GetProfiles
        resp_caps = MagicMock(status_code=200, content=SAMPLE_CAPABILITIES_XML.encode("utf-8"))
        resp_profiles = MagicMock(status_code=200, content=SAMPLE_PROFILES_XML.encode("utf-8"))
        mock_post.side_effect = [resp_caps, resp_profiles]

        client = ONVIFCameraClient("192.168.1.120", 80, "admin", "pass")
        profiles = client.get_profiles()

        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0]["token"], "Profile_MainStream")
        self.assertEqual(profiles[0]["name"], "MainStream")
        self.assertEqual(profiles[0]["encoding"], "H264")
        self.assertEqual(profiles[0]["width"], 1920)
        self.assertEqual(profiles[0]["height"], 1080)
        self.assertEqual(profiles[0]["fps"], 30)

        self.assertEqual(profiles[1]["token"], "Profile_SubStream")
        self.assertEqual(profiles[1]["width"], 640)

    @patch("requests.post")
    def test_get_stream_uri_success(self, mock_post):
        resp_caps = MagicMock(status_code=200, content=SAMPLE_CAPABILITIES_XML.encode("utf-8"))
        resp_uri = MagicMock(status_code=200, content=SAMPLE_STREAM_URI_XML.encode("utf-8"))
        mock_post.side_effect = [resp_caps, resp_uri]

        client = ONVIFCameraClient("192.168.1.120", 80, "admin", "pass")
        uri = client.get_stream_uri("Profile_MainStream")
        self.assertEqual(uri, "rtsp://192.168.1.120:554/live/ch0")

    @patch("requests.post")
    def test_authentication_failure_http_401(self, mock_post):
        mock_resp = MagicMock(status_code=401, content=b"Unauthorized")
        mock_post.return_value = mock_resp

        client = ONVIFCameraClient("192.168.1.120", 80, "admin", "wrong_pass")
        with self.assertRaises(ONVIFAuthError):
            client.get_device_information()

    @patch("requests.post")
    def test_authentication_failure_soap_fault(self, mock_post):
        mock_resp = MagicMock(status_code=200, content=SAMPLE_SOAP_AUTH_FAULT_XML.encode("utf-8"))
        mock_post.return_value = mock_resp

        client = ONVIFCameraClient("192.168.1.120", 80, "admin", "wrong_pass")
        with self.assertRaises(ONVIFAuthError):
            client.get_device_information()

    @patch("requests.post")
    def test_unreachable_camera_timeout(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out")

        client = ONVIFCameraClient("192.168.1.250", 80, "admin", "pass", timeout=1.0)
        with self.assertRaises(ONVIFConnectionError):
            client.get_device_information()

    @patch("requests.post")
    def test_malformed_soap_xml(self, mock_post):
        mock_resp = MagicMock(status_code=200, content=b"<not-xml-content>")
        mock_post.return_value = mock_resp

        client = ONVIFCameraClient("192.168.1.120", 80, "admin", "pass")
        with self.assertRaises(ONVIFParseError):
            client.get_device_information()


class TestCameraResolverAndConfig(unittest.TestCase):
    """Test camera source resolution (ONVIF, RTSP, legacy) and env var interpolation."""

    def test_expand_env_vars(self):
        os.environ["TEST_ONVIF_USER"] = "sec_officer"
        os.environ["TEST_ONVIF_PASS"] = "top_secret_99"

        template = {
            "user": "${TEST_ONVIF_USER}",
            "pass": "${TEST_ONVIF_PASS}",
            "url": "rtsp://${TEST_ONVIF_USER}:${TEST_ONVIF_PASS}@10.0.0.1/live",
        }
        resolved = expand_env_vars(template)
        self.assertEqual(resolved["user"], "sec_officer")
        self.assertEqual(resolved["pass"], "top_secret_99")
        self.assertEqual(resolved["url"], "rtsp://sec_officer:top_secret_99@10.0.0.1/live")

    def test_inject_credentials_into_rtsp_url(self):
        url = "rtsp://192.168.1.100:554/stream"
        with_creds = inject_credentials_into_rtsp_url(url, "admin", "pass123")
        self.assertEqual(with_creds, "rtsp://admin:pass123@192.168.1.100:554/stream")

        # Existing creds should not be overwritten
        already = "rtsp://existing:cred@192.168.1.100:554/stream"
        self.assertEqual(inject_credentials_into_rtsp_url(already, "new", "pass"), already)

    @patch.object(ONVIFCameraClient, "probe_camera")
    def test_resolve_onvif_camera_source(self, mock_probe):
        mock_probe.return_value = {
            "status": "online",
            "rtsp_url": "rtsp://192.168.1.120:554/live/ch0",
            "sanitized_rtsp_url": "rtsp://192.168.1.120:554/live/ch0",
            "profiles": [{"token": "Profile_MainStream", "name": "MainStream"}],
        }

        cam_config = {
            "id": "cam-border-onvif",
            "source": {
                "type": "onvif",
                "host": "192.168.1.120",
                "port": 80,
                "username": "admin",
                "password": "camera_secret_password",
            }
        }

        rtsp_url, meta = resolve_camera_source(cam_config)
        # Verify credentials were authenticated and injected for OpenCV ingestion
        self.assertEqual(rtsp_url, "rtsp://admin:camera_secret_password@192.168.1.120:554/live/ch0")
        self.assertEqual(meta["status"], "online")

    def test_resolve_manual_rtsp_source(self):
        cam_config = {
            "id": "cam-manual-rtsp",
            "source": {
                "type": "rtsp",
                "url": "rtsp://192.168.1.55:554/cam/realmonitor",
            }
        }
        rtsp_url, meta = resolve_camera_source(cam_config)
        self.assertEqual(rtsp_url, "rtsp://192.168.1.55:554/cam/realmonitor")
        self.assertEqual(meta["type"], "rtsp")

    def test_resolve_legacy_rtsp_url_compatibility(self):
        cam_config = {
            "id": "cam-main-entrance",
            "rtsp_url": "0",
        }
        rtsp_url, meta = resolve_camera_source(cam_config)
        self.assertEqual(rtsp_url, "0")
        self.assertEqual(meta["type"], "legacy")


class TestCentralCameraEndpoints(unittest.TestCase):
    """Test suite for Central REST endpoints: discovery and sanitized cameras."""

    def test_get_cameras_sanitizes_passwords(self):
        data = central.get_configured_cameras()
        self.assertIn("cameras", data)
        # Ensure no camera has sensitive passwords exposed
        for cam in data["cameras"]:
            if "source" in cam and "password" in cam["source"]:
                self.assertEqual(cam["source"]["password"], "***")

    @patch.object(ONVIFCameraClient, "probe_camera")
    def test_discover_targeted_host_probe(self, mock_probe):
        mock_probe.return_value = {
            "status": "online",
            "manufacturer": "ApexSurveillance",
            "model": "ITC-9000-HD",
            "firmware_version": "V4.2.1",
            "serial_number": "APX-12345",
            "hardware_id": "HW-001",
            "profiles": [{"token": "Profile_1", "name": "Main", "encoding": "H264", "width": 1920, "height": 1080}],
            "rtsp_url": "rtsp://admin:secret@192.168.1.120:554/ch0",
            "sanitized_rtsp_url": "rtsp://admin:***@192.168.1.120:554/ch0",
            "requires_auth": False,
        }

        data = central.discover_onvif_cameras(
            probe_host="192.168.1.120",
            probe_port=80,
            username="admin",
            password="secret",
        )
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["method"], "targeted_probe")
        self.assertEqual(data["count"], 1)

        cam = data["cameras"][0]
        self.assertEqual(cam["device_ip"], "192.168.1.120")
        self.assertEqual(cam["manufacturer"], "ApexSurveillance")
        self.assertEqual(cam["model"], "ITC-9000-HD")
        # Password must NEVER appear in returned RTSP URL or JSON body
        self.assertNotIn("secret", str(data))
        self.assertIn("***", cam["rtsp_url"])

    def test_discover_rejects_ssrf_host(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            central.discover_onvif_cameras(probe_host="169.254.169.254", probe_port=80)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("SSRF protection", ctx.exception.detail)



if __name__ == "__main__":
    unittest.main()
