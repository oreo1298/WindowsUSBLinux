"""Windows answer file generation (Rufus' "Windows User Experience" options)."""

from __future__ import annotations

import locale
import os
import re
import subprocess
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .tzmap import windows_zone

BYPASS_KEYS = ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck")
UNALLOWED_ACCOUNTS = {n.lower() for n in (
    "Administrator", "Järjestelmänvalvoja", "Administrateur", "Rendszergazda", "Administrador",
    "Администратор", "Administratör", "Guest", "DefaultAccount", "WDAGUtilityAccount",
    "HelpAssistant", "KRBTGT", "Local", "NONE", "SYSTEM")}
USERNAME_INVALID = re.compile(r'[\\/\[\]:;|=,+*?<>"@%\x00-\x1f]')
EMPTY_PASSWORD = "UABhAHMAcwB3AG8AcgBkAA=="  # base64 UTF-16LE "Password" suffix = empty password

KEYMAP_LOCALES = {
    "us": "en-US", "gb": "en-GB", "uk": "en-GB", "de": "de-DE", "fr": "fr-FR", "es": "es-ES",
    "it": "it-IT", "pt": "pt-PT", "br": "pt-BR", "nl": "nl-NL", "be": "nl-BE", "ch": "de-CH",
    "at": "de-AT", "se": "sv-SE", "no": "nb-NO", "dk": "da-DK", "fi": "fi-FI", "pl": "pl-PL",
    "cz": "cs-CZ", "sk": "sk-SK", "hu": "hu-HU", "ro": "ro-RO", "ru": "ru-RU", "ua": "uk-UA",
    "gr": "el-GR", "tr": "tr-TR", "jp": "ja-JP", "kr": "ko-KR", "cn": "zh-CN", "tw": "zh-TW",
    "latam": "es-MX", "ca": "fr-CA", "il": "he-IL", "ara": "ar-SA", "in": "hi-IN", "ie": "en-IE",
    "is": "is-IS", "ee": "et-EE", "lv": "lv-LV", "lt": "lt-LT", "si": "sl-SI", "hr": "hr-HR",
    "rs": "sr-Latn-RS", "bg": "bg-BG", "th": "th-TH", "vn": "vi-VN",
}


@dataclass
class WueOptions:
    bypass_requirements: bool = False  # TPM 2.0 / Secure Boot / 4GB RAM (Windows 11)
    no_online_account: bool = False
    local_account: str = ""
    duplicate_locale: bool = False
    no_data_collection: bool = False
    disable_bitlocker: bool = False

    def any(self) -> bool:
        return any((self.bypass_requirements, self.no_online_account, bool(self.local_account),
                    self.duplicate_locale, self.no_data_collection, self.disable_bitlocker))


@dataclass
class RegionalSettings:
    input_locale: str = ""
    system_locale: str = ""
    user_locale: str = ""
    ui_language: str = ""
    timezone: str = ""


def sanitize_username(name: str) -> str:
    name = USERNAME_INVALID.sub("_", name.strip()).strip(". ")
    return name[:20]


def username_problem(name: str) -> str | None:
    clean = sanitize_username(name)
    if not clean:
        return "The account name is empty"
    if clean.lower() in UNALLOWED_ACCOUNTS:
        return f"'{clean}' is a reserved Windows account name"
    return None


def _locale_name(value: str) -> str:
    """'en_US.UTF-8' -> 'en-US'."""
    value = value.split(".")[0].split("@")[0]
    if not value or value in ("C", "POSIX"):
        return ""
    return value.replace("_", "-")


def detect_regional_settings(image_languages: list[str] | None = None) -> RegionalSettings:
    lang = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if not lang:
        try:
            lang = locale.getlocale()[0] or ""
        except ValueError:
            lang = ""
    user_locale = _locale_name(os.environ.get("LC_TIME") or lang) or "en-US"
    system_locale = _locale_name(lang) or user_locale
    ui = system_locale if image_languages and system_locale in image_languages else ""
    input_locale = system_locale
    layout = _keyboard_layout()
    if layout and layout in KEYMAP_LOCALES:
        input_locale = KEYMAP_LOCALES[layout]
    return RegionalSettings(input_locale, system_locale, user_locale, ui, _windows_timezone() or "")


def _keyboard_layout() -> str:
    try:
        out = subprocess.run(["localectl", "status"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    for line in out.splitlines():
        if "X11 Layout:" in line:
            return line.split(":", 1)[1].strip().split(",")[0]
    try:
        with open("/etc/vconsole.conf") as f:
            for line in f:
                if line.startswith("KEYMAP="):
                    return line.split("=", 1)[1].strip().strip('"').split("-")[0]
    except OSError:
        pass
    return ""


def _windows_timezone() -> str | None:
    tz = os.environ.get("TZ", "").lstrip(":")
    if not tz:
        try:
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                tz = target.split("zoneinfo/", 1)[1]
        except OSError:
            tz = ""
    if not tz:
        try:
            with open("/etc/timezone") as f:
                tz = f.read().strip()
        except OSError:
            return None
    return windows_zone(tz)


def _component(name: str, arch: str, body: str) -> str:
    return (f'    <component name="{name}" processorArchitecture="{arch}" language="neutral" '
            'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">\n'
            f"{body}    </component>\n")


def needs_windows_pe(opts: WueOptions) -> bool:
    """Whether the answer file has a windowsPE pass (and so must go into boot.wim)."""
    return opts.bypass_requirements


def build_unattend(opts: WueOptions, arch: str = "amd64",
                   regional: RegionalSettings | None = None) -> tuple[str, str] | None:
    """Return (xml, placement).

    Placement is where the helper stores the answer file, following Rufus:
    'bootwim' adds it as \\Autounattend.xml to the setup image inside sources/boot.wim,
    so it only applies when the PC boots from the drive; running setup.exe from within
    Windows for an in-place upgrade never sees it (an Autounattend.xml at the root of
    the drive would turn the upgrade into a clean install).  'oem' is used when there
    is no windowsPE pass: sources/$OEM$/$$/Panther/unattend.xml.
    """
    if not opts.any():
        return None
    arch = {"x64": "amd64", "x86": "x86", "arm64": "arm64", "arm": "arm"}.get(arch, arch) or "amd64"
    parts = ['<?xml version="1.0" encoding="utf-8"?>\n',
             '<unattend xmlns="urn:schemas-microsoft-com:unattend">\n']
    has_pe = needs_windows_pe(opts)
    if has_pe:
        body = ("      <UserData>\n"
                "        <AcceptEula>true</AcceptEula>\n"
                "        <ProductKey>\n"
                "          <Key />\n"
                "        </ProductKey>\n"
                "      </UserData>\n"
                "      <RunSynchronous>\n")
        for order, key in enumerate(BYPASS_KEYS, 1):
            body += ("        <RunSynchronousCommand wcm:action=\"add\">\n"
                     f"          <Order>{order}</Order>\n"
                     f"          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {key} /t REG_DWORD /d 1 /f</Path>\n"
                     "        </RunSynchronousCommand>\n")
        body += "      </RunSynchronous>\n"
        parts.append('  <settings pass="windowsPE">\n')
        parts.append(_component("Microsoft-Windows-Setup", arch, body))
        parts.append("  </settings>\n")

    if opts.no_online_account:
        body = ("      <RunSynchronous>\n"
                "        <RunSynchronousCommand wcm:action=\"add\">\n"
                "          <Order>1</Order>\n"
                "          <Path>reg add \"HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\OOBE\" "
                "/v BypassNRO /t REG_DWORD /d 1 /f</Path>\n"
                "        </RunSynchronousCommand>\n"
                "      </RunSynchronous>\n")
        parts.append('  <settings pass="specialize">\n')
        parts.append(_component("Microsoft-Windows-Deployment", arch, body))
        parts.append("  </settings>\n")

    username = sanitize_username(opts.local_account) if opts.local_account else ""
    if username and username_problem(username):
        username = ""
    regional = regional if opts.duplicate_locale else None
    oobe = []
    shell = ""
    if opts.no_data_collection:
        shell += ("      <OOBE>\n"
                  "        <HideEULAPage>true</HideEULAPage>\n"
                  "        <ProtectYourPC>3</ProtectYourPC>\n"
                  "      </OOBE>\n")
    if regional and regional.timezone:
        shell += f"      <TimeZone>{escape(regional.timezone)}</TimeZone>\n"
    commands: list[str] = []
    if username:
        name = escape(username)
        shell += ("      <UserAccounts>\n"
                  "        <LocalAccounts>\n"
                  "          <LocalAccount wcm:action=\"add\">\n"
                  f"            <Name>{name}</Name>\n"
                  f"            <DisplayName>{name}</DisplayName>\n"
                  "            <Group>Administrators;Power Users</Group>\n"
                  "            <Password>\n"
                  f"              <Value>{EMPTY_PASSWORD}</Value>\n"
                  "              <PlainText>false</PlainText>\n"
                  "            </Password>\n"
                  "          </LocalAccount>\n"
                  "        </LocalAccounts>\n"
                  "      </UserAccounts>\n")
        # Blank password: ask for a new one at first logon, as Rufus does.
        commands.append(f'net user "{name}" /logonpasswordchg:yes')
        commands.append("net accounts /maxpwage:unlimited")
    if commands:
        shell += "      <FirstLogonCommands>\n"
        for order, cmd in enumerate(commands, 1):
            shell += ("        <SynchronousCommand wcm:action=\"add\">\n"
                      f"          <Order>{order}</Order>\n"
                      f"          <CommandLine>{cmd}</CommandLine>\n"
                      "        </SynchronousCommand>\n")
        shell += "      </FirstLogonCommands>\n"
    if shell:
        oobe.append(_component("Microsoft-Windows-Shell-Setup", arch, shell))
    if regional:
        intl = ""
        for tag, value in (("InputLocale", regional.input_locale), ("SystemLocale", regional.system_locale),
                           ("UserLocale", regional.user_locale), ("UILanguage", regional.ui_language)):
            if value:
                intl += f"      <{tag}>{escape(value)}</{tag}>\n"
        if intl:
            oobe.append(_component("Microsoft-Windows-International-Core", arch, intl))
    if opts.disable_bitlocker:
        oobe.append(_component("Microsoft-Windows-SecureStartup-FilterDriver", arch,
                               "      <PreventDeviceEncryption>true</PreventDeviceEncryption>\n"))
        oobe.append(_component("Microsoft-Windows-EnhancedStorage-Adm", arch,
                               "      <TCGSecurityActivationDisabled>1</TCGSecurityActivationDisabled>\n"))
    if oobe:
        parts.append('  <settings pass="oobeSystem">\n')
        parts.extend(oobe)
        parts.append("  </settings>\n")
    parts.append("</unattend>\n")
    return "".join(parts), ("bootwim" if has_pe else "oem")


def describe(opts: WueOptions) -> list[str]:
    out = []
    if opts.bypass_requirements:
        out.append("Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0")
    if opts.no_online_account:
        out.append("Remove requirement for an online Microsoft account")
    if opts.local_account:
        out.append(f"Create a local account with username '{sanitize_username(opts.local_account)}'")
    if opts.duplicate_locale:
        out.append("Set regional options to the same values as this user's")
    if opts.no_data_collection:
        out.append("Disable data collection (Skip privacy questions)")
    if opts.disable_bitlocker:
        out.append("Disable BitLocker automatic device encryption")
    return out
