"""Tests for the RadioReference SOAP client.

We don't hit the live API; instead we monkeypatch the client's _call method
to return canned ElementTree responses that mirror the real API shapes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import pytest

from p25_survey.radioreference import (
    RRAuthError,
    RRClient,
    RRError,
    RRSite,
    RRSiteFreq,
    RRSysidEntry,
    RRSystem,
    RRUser,
    _xml_escape,
)


def _xml_to_body(xml: str) -> ET.Element:
    """Wrap a fragment in a fake SOAP Body element for tests that consume
    what _call would return."""
    return ET.fromstring(f"<Body>{xml}</Body>")


def _make_client(monkeypatch, response_map: dict[str, ET.Element]) -> RRClient:
    """Construct a client whose _call(op, ...) returns the canned response."""
    client = RRClient(username="u", password="p", appkey="bundled-appkey")

    def fake_call(op: str, params: list[tuple[str, str]] | None = None) -> ET.Element:
        if op not in response_map:
            raise AssertionError(f"unexpected op: {op}")
        return response_map[op]

    monkeypatch.setattr(client, "_call", fake_call)
    return client


# ---------------------------------------------------------------------------
# getUserData
# ---------------------------------------------------------------------------


class TestGetUserData:
    def test_parses_user_info(self, monkeypatch):
        response = _xml_to_body("""
            <getUserDataReturn>
              <username>radiouser</username>
              <subExpireDate>2027-01-01T00:00:00</subExpireDate>
            </getUserDataReturn>
        """)
        client = _make_client(monkeypatch, {"getUserData": response})
        user = client.get_user_data()
        assert isinstance(user, RRUser)
        assert user.username == "radiouser"
        assert user.sub_expire_date == "2027-01-01T00:00:00"

    def test_no_appkey_raises(self, monkeypatch):
        # Wipe bundled key + env var so the constructor sees nothing.
        monkeypatch.setattr("p25_survey.radioreference._BUNDLED_APP_KEY", "")
        monkeypatch.delenv("P25_SURVEY_APPKEY", raising=False)
        with pytest.raises(RRError):
            RRClient(username="u", password="p", appkey="")


# ---------------------------------------------------------------------------
# getTrsBySysid + getTrsDetails (composite: find_system_by_wacn_sysid)
# ---------------------------------------------------------------------------


class TestFindSystemByWacnSysid:
    def test_single_match(self, monkeypatch):
        by_sysid = _xml_to_body("""
            <getTrsBySysidReturn>
              <TrsListDef><sid>123</sid><sName>Test</sName></TrsListDef>
            </getTrsBySysidReturn>
        """)
        details = _xml_to_body("""
            <getTrsDetailsReturn>
              <sName>State Public Safety</sName>
              <sType>2</sType>
              <sFlavor>3</sFlavor>
              <sCity>Statewide</sCity>
              <sysid>
                <TrsSysid><wacn>BEE00</wacn><sysid>1A4</sysid></TrsSysid>
              </sysid>
            </getTrsDetailsReturn>
        """)
        client = _make_client(monkeypatch, {
            "getTrsBySysid": by_sysid,
            "getTrsDetails": details,
        })
        sys = client.find_system_by_wacn_sysid("BEE00", "1A4")
        assert isinstance(sys, RRSystem)
        assert sys.sid == 123
        assert sys.name == "State Public Safety"
        assert sys.has_wacn_sysid("BEE00", "1A4")

    def test_filters_by_wacn(self, monkeypatch):
        # Two SIDs with the same SYSID, only one with our WACN
        by_sysid = _xml_to_body("""
            <getTrsBySysidReturn>
              <TrsListDef><sid>100</sid></TrsListDef>
              <TrsListDef><sid>200</sid></TrsListDef>
            </getTrsBySysidReturn>
        """)
        # Two different details responses based on which SID is queried
        details_100 = _xml_to_body("""
            <getTrsDetailsReturn>
              <sName>Other System</sName>
              <sysid><TrsSysid><wacn>123AB</wacn><sysid>1A4</sysid></TrsSysid></sysid>
            </getTrsDetailsReturn>
        """)
        details_200 = _xml_to_body("""
            <getTrsDetailsReturn>
              <sName>Our System</sName>
              <sysid><TrsSysid><wacn>BEE00</wacn><sysid>1A4</sysid></TrsSysid></sysid>
            </getTrsDetailsReturn>
        """)

        client = RRClient(username="u", password="p", appkey="x")
        captured: dict[str, list] = {"calls": []}

        def fake_call(op: str, params: list[tuple[str, str]] | None = None):
            captured["calls"].append((op, params or []))
            if op == "getTrsBySysid":
                return by_sysid
            if op == "getTrsDetails":
                sid = dict(params or [])["sid"]
                return details_100 if sid == "100" else details_200
            raise AssertionError(op)

        import types
        client._call = types.MethodType(lambda self, *a, **k: fake_call(*a, **k), client)
        sys = client.find_system_by_wacn_sysid("BEE00", "1A4")
        assert sys is not None
        assert sys.sid == 200

    def test_no_match_returns_none(self, monkeypatch):
        by_sysid = _xml_to_body("""
            <getTrsBySysidReturn>
              <TrsListDef><sid>100</sid></TrsListDef>
            </getTrsBySysidReturn>
        """)
        details = _xml_to_body("""
            <getTrsDetailsReturn>
              <sName>Other</sName>
              <sysid><TrsSysid><wacn>123AB</wacn><sysid>1A4</sysid></TrsSysid></sysid>
            </getTrsDetailsReturn>
        """)
        client = _make_client(monkeypatch, {
            "getTrsBySysid": by_sysid,
            "getTrsDetails": details,
        })
        assert client.find_system_by_wacn_sysid("BEE00", "1A4") is None

    def test_caches_result(self, monkeypatch):
        by_sysid = _xml_to_body("""
            <getTrsBySysidReturn>
              <TrsListDef><sid>123</sid></TrsListDef>
            </getTrsBySysidReturn>
        """)
        details = _xml_to_body("""
            <getTrsDetailsReturn>
              <sName>X</sName>
              <sysid><TrsSysid><wacn>BEE00</wacn><sysid>1A4</sysid></TrsSysid></sysid>
            </getTrsDetailsReturn>
        """)
        call_count = {"n": 0}

        def fake_call(op: str, params=None):
            call_count["n"] += 1
            return {"getTrsBySysid": by_sysid, "getTrsDetails": details}[op]

        client = RRClient(username="u", password="p", appkey="x")
        import types
        client._call = types.MethodType(lambda self, *a, **k: fake_call(*a, **k), client)

        client.find_system_by_wacn_sysid("BEE00", "1A4")
        first = call_count["n"]
        # Second call hits cache
        client.find_system_by_wacn_sysid("BEE00", "1A4")
        assert call_count["n"] == first


# ---------------------------------------------------------------------------
# getTrsSites
# ---------------------------------------------------------------------------


class TestGetSites:
    def test_parses_sites_with_freqs(self, monkeypatch):
        body = _xml_to_body("""
            <getTrsSitesReturn>
              <TrsSite>
                <siteId>9001</siteId>
                <siteNumber>7</siteNumber>
                <rfss>1</rfss>
                <nac>293</nac>
                <siteDescr>Downtown</siteDescr>
                <siteLocation>City</siteLocation>
                <siteCt>Anywhere County</siteCt>
                <lat>40.123</lat>
                <lon>-83.456</lon>
                <siteFreqs>
                  <TrsSiteFreq><freq>851.00625</freq><lcn>1</lcn><use>d</use></TrsSiteFreq>
                  <TrsSiteFreq><freq>851.10625</freq><lcn>2</lcn><use>a</use></TrsSiteFreq>
                  <TrsSiteFreq><freq>851.20625</freq><lcn>3</lcn><use></use></TrsSiteFreq>
                </siteFreqs>
                <siteLicenses>
                  <TrsSiteLicense><license>WPRR123</license></TrsSiteLicense>
                </siteLicenses>
              </TrsSite>
              <TrsSite>
                <siteId>9002</siteId>
                <siteNumber>8</siteNumber>
                <rfss>1</rfss>
                <siteFreqs>
                  <TrsSiteFreq><freq>855.06250</freq><use>d</use></TrsSiteFreq>
                </siteFreqs>
              </TrsSite>
            </getTrsSitesReturn>
        """)
        client = _make_client(monkeypatch, {"getTrsSites": body})
        sites = client.get_sites(123)
        assert len(sites) == 2
        s1, s2 = sites
        assert s1.site_number == 7 and s1.rfss == 1
        assert s1.nac == "293"
        assert s1.lat == 40.123
        assert len(s1.frequencies) == 3
        assert s1.frequencies[0].freq_hz == 851_006_250
        assert s1.frequencies[0].is_primary_control
        assert s1.frequencies[1].is_control and not s1.frequencies[1].is_primary_control
        assert not s1.frequencies[2].is_control
        assert s1.licenses == ["WPRR123"]
        assert s1.control_freqs_hz() == [851_006_250, 851_106_250]
        assert s2.site_number == 8

    def test_no_sites_returns_empty(self, monkeypatch):
        body = _xml_to_body("<getTrsSitesReturn/>")
        client = _make_client(monkeypatch, {"getTrsSites": body})
        assert client.get_sites(123) == []

    def test_caches_per_sid(self, monkeypatch):
        body = _xml_to_body("""
            <getTrsSitesReturn>
              <TrsSite><siteNumber>1</siteNumber><rfss>1</rfss></TrsSite>
            </getTrsSitesReturn>
        """)
        n = {"calls": 0}

        def fake_call(op, params=None):
            n["calls"] += 1
            return body

        client = RRClient(username="u", password="p", appkey="x")
        import types
        client._call = types.MethodType(lambda self, *a, **k: fake_call(*a, **k), client)
        client.get_sites(42)
        client.get_sites(42)  # cached
        assert n["calls"] == 1
        client.get_sites(43)  # different sid → fresh call
        assert n["calls"] == 2


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_includes_authinfo(self):
        client = RRClient(username="alice", password="s3cret!", appkey="appk")
        env = client._build_envelope("getUserData", [])
        assert "<username>alice</username>" in env
        assert "<password>s3cret!</password>" in env
        assert "<appKey>appk</appKey>" in env
        assert "<rr:getUserData>" in env

    def test_includes_params(self):
        client = RRClient(username="u", password="p", appkey="k")
        env = client._build_envelope("getTrsDetails", [("sid", "123")])
        assert "<sid>123</sid>" in env

    def test_xml_escapes_special_chars(self):
        client = RRClient(username='a&b<c>"d', password="p", appkey="k")
        env = client._build_envelope("getUserData", [])
        # raw chars must not appear in element text
        assert "<username>a&amp;b&lt;c&gt;&quot;d</username>" in env

    def test_xml_escape_helper(self):
        assert _xml_escape("a&b") == "a&amp;b"
        assert _xml_escape("<x>") == "&lt;x&gt;"


# ---------------------------------------------------------------------------
# Auth + fault handling
# ---------------------------------------------------------------------------


class TestFaultHandling:
    def test_auth_fault_raises_auth_error(self):
        body = b"""<?xml version="1.0"?>
        <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
          <soap:Body>
            <soap:Fault>
              <faultcode>SOAP-ENV:Client</faultcode>
              <faultstring>Invalid login credentials</faultstring>
            </soap:Fault>
          </soap:Body>
        </soap:Envelope>"""
        client = RRClient(username="u", password="p", appkey="k")
        with pytest.raises(RRAuthError):
            client._parse_response("getUserData", body)

    def test_generic_fault_raises_rrerror(self):
        body = b"""<?xml version="1.0"?>
        <Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">
          <Body>
            <Fault>
              <faultstring>Service temporarily unavailable</faultstring>
            </Fault>
          </Body>
        </Envelope>"""
        client = RRClient(username="u", password="p", appkey="k")
        with pytest.raises(RRError) as exc:
            client._parse_response("anything", body)
        assert "temporarily unavailable" in str(exc.value)

    def test_invalid_xml_raises(self):
        client = RRClient(username="u", password="p", appkey="k")
        with pytest.raises(RRError):
            client._parse_response("op", b"<not valid xml")


# ---------------------------------------------------------------------------
# Dataclass helpers
# ---------------------------------------------------------------------------


class TestRRSystem:
    def test_has_wacn_sysid_strips_leading_zeros(self):
        sys = RRSystem(
            sid=1, name="x", sys_type=0, sys_flavor=0,
            sysid_entries=[RRSysidEntry(wacn="0BEE00", sysid="01A4")],
        )
        assert sys.has_wacn_sysid("BEE00", "1A4")
        assert sys.has_wacn_sysid("bee00", "1a4")  # case-insensitive

    def test_no_match_when_wacn_differs(self):
        sys = RRSystem(
            sid=1, name="x", sys_type=0, sys_flavor=0,
            sysid_entries=[RRSysidEntry(wacn="123AB", sysid="1A4")],
        )
        assert not sys.has_wacn_sysid("BEE00", "1A4")


class TestRRSiteFreqClassification:
    def test_use_classifications(self):
        assert RRSiteFreq(freq_hz=1, use="d").is_primary_control
        assert RRSiteFreq(freq_hz=1, use="d").is_control
        assert RRSiteFreq(freq_hz=1, use="a").is_control
        assert not RRSiteFreq(freq_hz=1, use="a").is_primary_control
        assert not RRSiteFreq(freq_hz=1, use="").is_control
