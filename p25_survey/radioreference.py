"""Minimal RadioReference SOAP client (stdlib only).

We only need 4 operations against api.radioreference.com:
  getUserData       — verify credentials work
  getTrsBySysid     — find the RR internal sid(s) given a P25 SYSID
  getTrsDetails     — fetch system details (sysid array carries WACN)
  getTrsSites       — fetch per-site frequencies + RFSS/site numbers

A full SOAP toolkit (zeep / suds) is overkill for this; we hand-roll the
envelopes with urllib + ElementTree to keep the project's "no bundled
runtime deps" property.

Authentication: every call carries an `authInfo` block with appKey,
username, password, version, style. The appKey is bundled with the tool;
username and password are prompted from the user at scan startup.

Caching: per-client in-memory cache keyed by `(operation, args)`. Lives
for the duration of the scan; nothing on disk.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_ENDPOINT = "https://api.radioreference.com/soap2/"
_NS = "http://api.radioreference.com/soap2"
_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_API_VERSION = "latest"
_API_STYLE = "rpc"
_DEFAULT_TIMEOUT_S = 30.0

# The appKey is bundled in the released binary. RadioReference issues these
# per-application; users should not need their own. If a user wants to
# override (e.g. testing against a private appKey), set P25_SURVEY_APPKEY.
_BUNDLED_APP_KEY = "18e7613e-40c2-11f1-bb32-0ef97433b5f9"


def app_key() -> str:
    return os.environ.get("P25_SURVEY_APPKEY", _BUNDLED_APP_KEY)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RRUser:
    username: str
    sub_expire_date: str  # ISO date string from RR


@dataclass(frozen=True)
class RRSysidEntry:
    """One <sysid> element inside a Trs system. Carries the WACN."""
    wacn: str       # uppercase hex string
    sysid: str      # uppercase hex string
    nac: str | None = None


@dataclass(frozen=True)
class RRSystem:
    sid: int          # RR internal system id (primary key)
    name: str
    sys_type: int     # numeric type code
    sys_flavor: int
    city: str = ""
    sysid_entries: list[RRSysidEntry] = field(default_factory=list)

    def has_wacn_sysid(self, wacn_hex: str, sysid_hex: str) -> bool:
        wacn_hex = wacn_hex.upper().lstrip("0") or "0"
        sysid_hex = sysid_hex.upper().lstrip("0") or "0"
        for e in self.sysid_entries:
            if e.wacn.upper().lstrip("0") in (wacn_hex, "") and \
                    e.sysid.upper().lstrip("0") == sysid_hex:
                return True
        return False


@dataclass(frozen=True)
class RRSiteFreq:
    """One frequency on an RR site."""
    freq_hz: int
    lcn: int | None = None
    use: str = ""        # "d" = primary control, "a" = alternate control, "" = traffic
    color_code: str = ""

    @property
    def is_control(self) -> bool:
        return self.use.lower() in ("d", "a")

    @property
    def is_primary_control(self) -> bool:
        return self.use.lower() == "d"


@dataclass(frozen=True)
class RRSite:
    sid: int                # parent system's RR sid
    site_db_id: int         # RR internal siteId (DB key)
    site_number: int        # P25 Site Number (this is what we decode)
    rfss: int
    nac: str = ""
    description: str = ""
    location: str = ""
    county: str = ""
    lat: float | None = None
    lon: float | None = None
    frequencies: list[RRSiteFreq] = field(default_factory=list)
    licenses: list[str] = field(default_factory=list)

    def control_freqs_hz(self) -> list[int]:
        return [f.freq_hz for f in self.frequencies if f.is_control]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RRError(Exception):
    """Generic RR API error."""


class RRAuthError(RRError):
    """Login/auth refused."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RRClient:
    """Minimal SOAP client for the four operations we need."""

    def __init__(self, username: str, password: str,
                 appkey: str | None = None,
                 endpoint: str = _ENDPOINT,
                 timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._username = username
        self._password = password
        self._appkey = appkey or app_key()
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._cache: dict[tuple, Any] = {}
        if not self._appkey:
            raise RRError(
                "No RadioReference appKey configured. Set P25_SURVEY_APPKEY "
                "or build with one bundled."
            )

    # ----- public operations ------------------------------------------------

    def get_user_data(self) -> RRUser:
        """Verify credentials by calling getUserData. Raises RRAuthError on
        invalid login. Used as the auth-validation step at startup."""
        body = self._call("getUserData")
        info = _find_child(body, "getUserDataReturn")
        if info is None:
            raise RRError("getUserData returned no result")
        return RRUser(
            username=_text(info, "username"),
            sub_expire_date=_text(info, "subExpireDate"),
        )

    def find_system_by_wacn_sysid(self, wacn_hex: str, sysid_hex: str) -> RRSystem | None:
        """Look up a system by P25 WACN + SYSID. Returns the matching RRSystem
        (with sid populated) or None.

        getTrsBySysid(sysid) returns all systems with that sysid (could be
        multiple — sysid is not unique across WACNs). We then fetch
        getTrsDetails for each to find the one whose sysid array carries
        our WACN.
        """
        cache_key = ("wacn_sysid", wacn_hex.upper(), sysid_hex.upper())
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Step 1: fan out by SYSID
        body = self._call("getTrsBySysid", [("sysid", sysid_hex.upper())])
        candidates: list[int] = []
        for trs in body.iter():
            tag = _strip_ns(trs.tag)
            if tag in ("getTrsBySysidReturn", "TrsListDef"):
                sid_text = _text(trs, "sid")
                if sid_text:
                    try:
                        candidates.append(int(sid_text))
                    except ValueError:
                        pass

        # Step 2: filter by WACN via getTrsDetails on each candidate.
        for sid in candidates:
            sys = self.get_system_details(sid)
            if sys.has_wacn_sysid(wacn_hex, sysid_hex):
                self._cache[cache_key] = sys
                return sys

        self._cache[cache_key] = None
        return None

    def get_system_details(self, sid: int) -> RRSystem:
        cache_key = ("trs_details", sid)
        if cache_key in self._cache:
            return self._cache[cache_key]
        body = self._call("getTrsDetails", [("sid", str(sid))])
        trs = _find_child(body, "getTrsDetailsReturn")
        if trs is None:
            raise RRError(f"getTrsDetails({sid}) returned no Trs")
        sysid_entries: list[RRSysidEntry] = []
        for sysid_elem in trs.iter():
            if _strip_ns(sysid_elem.tag) == "TrsSysid":
                wacn = _text(sysid_elem, "wacn") or _text(sysid_elem, "WACN") or ""
                sysid = _text(sysid_elem, "sysid") or ""
                nac = _text(sysid_elem, "nac") or None
                if sysid:
                    sysid_entries.append(RRSysidEntry(wacn=wacn, sysid=sysid, nac=nac))
        sys = RRSystem(
            sid=sid,
            name=_text(trs, "sName"),
            sys_type=_int(trs, "sType"),
            sys_flavor=_int(trs, "sFlavor"),
            city=_text(trs, "sCity"),
            sysid_entries=sysid_entries,
        )
        self._cache[cache_key] = sys
        return sys

    def get_sites(self, sid: int) -> list[RRSite]:
        cache_key = ("trs_sites", sid)
        if cache_key in self._cache:
            return self._cache[cache_key]
        body = self._call("getTrsSites", [("sid", str(sid))])
        sites: list[RRSite] = []
        for site_elem in body.iter():
            if _strip_ns(site_elem.tag) != "TrsSite":
                continue
            freqs: list[RRSiteFreq] = []
            for fe in site_elem.iter():
                if _strip_ns(fe.tag) != "TrsSiteFreq":
                    continue
                freq_mhz = _decimal(fe, "freq")
                if freq_mhz is None:
                    continue
                freqs.append(RRSiteFreq(
                    freq_hz=int(round(freq_mhz * 1_000_000)),
                    lcn=_int(fe, "lcn") or None,
                    use=_text(fe, "use"),
                    color_code=_text(fe, "colorCode"),
                ))
            licenses: list[str] = []
            for le in site_elem.iter():
                if _strip_ns(le.tag) == "TrsSiteLicense":
                    lic = _text(le, "license")
                    if lic:
                        licenses.append(lic)
            sites.append(RRSite(
                sid=sid,
                site_db_id=_int(site_elem, "siteId"),
                site_number=_int(site_elem, "siteNumber"),
                rfss=_int(site_elem, "rfss"),
                nac=_text(site_elem, "nac"),
                description=_text(site_elem, "siteDescr"),
                location=_text(site_elem, "siteLocation"),
                county=_text(site_elem, "siteCt"),
                lat=_decimal(site_elem, "lat"),
                lon=_decimal(site_elem, "lon"),
                frequencies=freqs,
                licenses=licenses,
            ))
        self._cache[cache_key] = sites
        return sites

    # ----- low-level SOAP transport ----------------------------------------

    def _call(self, op: str, params: list[tuple[str, str]] | None = None) -> ET.Element:
        envelope = self._build_envelope(op, params or [])
        req = Request(
            self._endpoint,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{op}"',
                "User-Agent": "p25-survey/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=self._timeout_s) as resp:
                raw = resp.read()
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401 or "auth" in body.lower():
                raise RRAuthError(f"RR {op} returned HTTP {e.code}: {_extract_fault(body)}") from e
            raise RRError(f"RR {op} HTTP {e.code}: {_extract_fault(body)}") from e
        except URLError as e:
            raise RRError(f"RR {op} network error: {e.reason}") from e

        return self._parse_response(op, raw)

    def _build_envelope(self, op: str, params: list[tuple[str, str]]) -> str:
        param_xml = "".join(
            f'      <{name}>{_xml_escape(value)}</{name}>'
            for name, value in params
        )
        if param_xml:
            param_xml = "\n" + param_xml + "\n      "
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{_SOAP_NS}"
               xmlns:rr="{_NS}"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <rr:{op}>{param_xml}<authInfo>
        <username>{_xml_escape(self._username)}</username>
        <password>{_xml_escape(self._password)}</password>
        <appKey>{_xml_escape(self._appkey)}</appKey>
        <version>{_API_VERSION}</version>
        <style>{_API_STYLE}</style>
      </authInfo>
    </rr:{op}>
  </soap:Body>
</soap:Envelope>
"""

    def _parse_response(self, op: str, raw: bytes) -> ET.Element:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise RRError(f"RR {op}: invalid XML response: {e}") from e
        # Look for SOAP fault first.
        for elem in root.iter():
            if _strip_ns(elem.tag) == "Fault":
                fault_str = _text(elem, "faultstring") or "(no faultstring)"
                if "auth" in fault_str.lower() or "login" in fault_str.lower():
                    raise RRAuthError(f"{op}: {fault_str}")
                raise RRError(f"{op}: SOAP fault: {fault_str}")
        body = None
        for elem in root.iter():
            if _strip_ns(elem.tag) == "Body":
                body = elem
                break
        if body is None:
            raise RRError(f"{op}: no SOAP Body in response")
        return body


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_child(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent.iter():
        if _strip_ns(child.tag) == name:
            return child
    return None


def _text(parent: ET.Element, name: str) -> str:
    """Return text of first child with given local name, or empty string."""
    for child in parent:
        if _strip_ns(child.tag) == name:
            return (child.text or "").strip()
    # fall back to deeper search
    deep = _find_child(parent, name)
    return (deep.text or "").strip() if deep is not None and deep.text else ""


def _int(parent: ET.Element, name: str) -> int:
    s = _text(parent, name)
    try:
        return int(s) if s else 0
    except ValueError:
        return 0


def _decimal(parent: ET.Element, name: str) -> float | None:
    s = _text(parent, name)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


def _extract_fault(body: str) -> str:
    """Best-effort extraction of <faultstring> from an HTTP error body."""
    try:
        root = ET.fromstring(body)
        for elem in root.iter():
            if _strip_ns(elem.tag) == "faultstring" and elem.text:
                return elem.text.strip()
    except ET.ParseError:
        pass
    return body[:200]
