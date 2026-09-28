import xml.etree.ElementTree as ET

from rufux.core.unattend import (RegionalSettings, WueOptions, build_unattend, sanitize_username,
                                 username_problem)

NS = {"u": "urn:schemas-microsoft-com:unattend"}


def passes(xml: str) -> dict:
    root = ET.fromstring(xml)
    return {s.get("pass"): s for s in root.findall("u:settings", NS)}


def test_nothing_selected():
    assert build_unattend(WueOptions()) is None


def test_bypass_goes_to_windows_pe_and_root():
    xml, target = build_unattend(WueOptions(bypass_requirements=True), "x64")
    assert target == "root"
    p = passes(xml)
    assert set(p) == {"windowsPE"}
    comp = p["windowsPE"].find("u:component", NS)
    assert comp.get("name") == "Microsoft-Windows-Setup"
    assert comp.get("processorArchitecture") == "amd64"
    paths = [e.text for e in comp.iter("{urn:schemas-microsoft-com:unattend}Path")]
    assert len(paths) == 3 and all("LabConfig" in x for x in paths)
    assert "BypassTPMCheck" in paths[0] and "BypassRAMCheck" in paths[2]


def test_oobe_only_goes_to_oem():
    opts = WueOptions(no_online_account=True, local_account="Jane <Doe>", no_data_collection=True,
                      disable_bitlocker=True, duplicate_locale=True)
    reg = RegionalSettings("de-DE", "de-DE", "de-DE", "", "W. Europe Standard Time")
    xml, target = build_unattend(opts, "arm64", reg)
    assert target == "oem"
    p = passes(xml)
    assert set(p) == {"specialize", "oobeSystem"}
    text = ET.tostring(p["oobeSystem"], encoding="unicode")
    assert "Jane _Doe_" in text
    assert "ProtectYourPC" in text and "W. Europe Standard Time" in text
    assert "PreventDeviceEncryption" in text and "InputLocale" in text
    assert "UILanguage" not in text  # not an image language: leave it alone
    assert "BypassNRO" in ET.tostring(p["specialize"], encoding="unicode")
    assert 'processorArchitecture="arm64"' in xml


def test_usernames():
    assert sanitize_username(' bob:"x" ') == "bob__x_"
    assert username_problem("Administrator")
    assert username_problem("   ")
    assert username_problem("alice") is None
    assert len(sanitize_username("a" * 40)) == 20
