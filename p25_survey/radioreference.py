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
        # RPC-style responses often wrap the payload in <return> rather than
        # <getUserDataReturn>; sometimes neither, just the fields directly.
        # Find any element that has both <username> and <subExpireDate>
        # children; that's UserInfo regardless of the wrapper.
        info = _find_struct_with_children(body, ["username", "subExpireDate"])
        if info is None:
            info = body
        username = _text(info, "username")
        sub_expire = _text(info, "subExpireDate")
        if not username:
            raise RRError(f"getUserData: could not parse UserInfo from response. "
                          f"Set P25_SURVEY_RR_DEBUG=1 to dump raw XML.")
        return RRUser(username=username, sub_expire_date=sub_expire)

    def find_systems_by_wacn_sysid(self, wacn_hex: str, sysid_hex: str) -> list[RRSystem]:
        """Look up all systems carrying a given P25 WACN + SYSID.

        Returns every matching RRSystem (with sid populated), in the order
        getTrsBySysid returned them. Empty list when no RR system claims
        this WACN/SYSID.

        Plural rather than singular because RR sometimes lists more than
        one distinct system under the same WACN/SYSID — typically when the
        same identity is reused across neighboring counties (e.g. multiple
        Mississippi single-site county networks sharing 92448/00A). The
        caller has to disambiguate by RFSS/Site or NAC; we can't decide
        here.

        getTrsBySysid(sysid) returns a SOAP-ENC:Array (typed
        TrsListDef[N]). RR encodes array members as <item xsi:type="tns:TrsListDef">.
        We then fetch getTrsDetails for each candidate and keep those
        whose sysid array carries our WACN.
        """
        cache_key = ("wacn_sysid", wacn_hex.upper(), sysid_hex.upper())
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Step 1: fan out by SYSID
        body = self._call("getTrsBySysid", [("sysid", sysid_hex.upper())])
        candidates: list[int] = []
        for sid_elem in _array_items(body, ["sid"]):
            sid_text = _text(sid_elem, "sid")
            if sid_text:
                try:
                    candidates.append(int(sid_text))
                except ValueError:
                    pass

        # Step 2: filter by WACN via getTrsDetails on each candidate.
        matches: list[RRSystem] = []
        for sid in candidates:
            sys = self.get_system_details(sid)
            if sys.has_wacn_sysid(wacn_hex, sysid_hex):
                matches.append(sys)

        self._cache[cache_key] = matches
        return matches

    def get_system_details(self, sid: int) -> RRSystem:
        cache_key = ("trs_details", sid)
        if cache_key in self._cache:
            return self._cache[cache_key]
        body = self._call("getTrsDetails", [("sid", str(sid))])
        # The Trs structure lives inside <return xsi:type="tns:Trs">.
        trs = _find_child(body, "return")
        if trs is None:
            trs = _find_struct_with_children(body, ["sName"])
        if trs is None:
            trs = body
        # The <sysid> array in the Trs has <item xsi:type="tns:TrsSysid"> members.
        sysid_arr = _find_child(trs, "sysid")
        sysid_entries: list[RRSysidEntry] = []
        if sysid_arr is not None:
            for item in _array_items(sysid_arr, ["sysid"]):
                # Inside each item, fields are <wacn>, <sysid>, <nac>.
                wacn = _text(item, "wacn") or _text(item, "WACN") or ""
                sid_text = _text(item, "sysid") or ""
                nac = _text(item, "nac") or None
                if sid_text:
                    sysid_entries.append(RRSysidEntry(wacn=wacn, sysid=sid_text, nac=nac))
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
        # <return xsi:type="SOAP-ENC:Array" arrayType="TrsSite[N]">
        # Members are <item xsi:type="tns:TrsSite">.
        sites: list[RRSite] = []
        for site_elem in _array_items(body, ["rfss", "siteNumber"]):
            # The TrsSite contains a siteFreqs array of <item xsi:type="tns:TrsSiteFreq">.
            freq_arr = _find_child(site_elem, "siteFreqs")
            freqs: list[RRSiteFreq] = []
            if freq_arr is not None:
                for fe in _array_items(freq_arr, ["freq"]):
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
            lic_arr = _find_child(site_elem, "siteLicenses")
            if lic_arr is not None:
                for le in _array_items(lic_arr, ["license"]):
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
        if os.environ.get("P25_SURVEY_RR_DEBUG"):
            import sys
            sys.stderr.write(f"\n--- RR {op} raw response ---\n")
            sys.stderr.write(raw.decode("utf-8", errors="replace"))
            sys.stderr.write("\n--- end response ---\n")
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


def _find_struct_with_children(parent: ET.Element, required_child_names: list[str]) -> ET.Element | None:
    """Find any descendant element whose immediate children include ALL of the
    given local-names. Used to locate result structures by content rather
    than by RPC/document-style wrapper element names.
    """
    needed = set(required_child_names)
    for elem in parent.iter():
        local_kids = {_strip_ns(c.tag) for c in elem}
        if needed.issubset(local_kids):
            return elem
    return None


def _array_items(parent: ET.Element, required_field_names: list[str]) -> list[ET.Element]:
    """Find SOAP-ENC:Array members anywhere under `parent`.

    RR returns arrays as `<return xsi:type="SOAP-ENC:Array">` containing
    `<item xsi:type="tns:TypeName">` children. The element name `<item>`
    isn't a hard rule (some servers use the type name); we accept any
    element whose immediate children include all of `required_field_names`,
    which uniquely identifies the array members regardless of tag name.
    """
    needed = set(required_field_names)
    out: list[ET.Element] = []
    for elem in parent.iter():
        local_kids = {_strip_ns(c.tag) for c in elem}
        if needed.issubset(local_kids):
            out.append(elem)
    return out


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
