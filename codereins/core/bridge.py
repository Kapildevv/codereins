"""The bridge extension: package it as a .vsix and install it with VS Code's own CLI.

The extension is plain JavaScript with no build step, so the .vsix (a zip with a
manifest) is assembled here and no Node toolchain is needed.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from codereins.core.project import list_profiles
from codereins.core.windows import for_app
from codereins.utils import vscode_backend as backend
from codereins.utils.bridge_client import home, live_windows

FILES = ("package.json", "extension.js", "README.md")

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension=".json" ContentType="application/json"/>'
    '<Default Extension=".vsixmanifest" ContentType="text/xml"/>'
    '<Default Extension=".js" ContentType="application/javascript"/>'
    '<Default Extension=".md" ContentType="text/markdown"/>'
    "</Types>\n"
)

VSIX_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Tags></Tags>
    <Categories>Other</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="{kind}"/>
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value=""/>
      <Property Id="Microsoft.VisualStudio.Services.GitHubFlavoredMarkdown" Value="true"/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def source_dir() -> Path:
    """Folder holding the extension source shipped with this package."""
    return Path(__file__).resolve().parent.parent / "bridge"


def manifest() -> dict:
    """The extension's package.json."""
    return json.loads((source_dir() / "package.json").read_text(encoding="utf-8"))


def extension_id() -> str:
    """``publisher.name`` of the bridge extension."""
    m = manifest()
    return f"{m['publisher']}.{m['name']}"


def build_vsix(out_dir: Optional[str] = None) -> Path:
    """Assemble the .vsix and return its path."""
    m = manifest()
    folder = Path(out_dir) if out_dir else home() / "build"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{m['name']}-{m['version']}.vsix"
    xml = VSIX_MANIFEST.format(
        name=escape(m["name"]), version=escape(m["version"]), publisher=escape(m["publisher"]),
        display=escape(m["displayName"]), description=escape(m["description"]),
        engine=escape(m["engines"]["vscode"]), kind=escape(",".join(m.get("extensionKind", []))),
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", xml)
        for name in FILES:
            z.write(source_dir() / name, f"extension/{name}")
    return out


def installed_version(user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None,
                      profile: Optional[str] = None) -> Optional[str]:
    """Version of the bridge the editor has installed (in ``profile``, if given), or None."""
    want = extension_id().lower()
    for e in backend.list_extensions(user_data_dir, extensions_dir, profile):
        if e["id"].lower() == want:
            return e["version"]
    return None


def _profile_targets(user_data_dir: Optional[str], extensions_dir: Optional[str]) -> list[Optional[str]]:
    """None (the Default profile, which every profile borrowing extensions shares) plus each
    profile that keeps its own extension set. Names come from storage.json only: ``--profile``
    with an unknown name would make the editor create an empty profile."""
    return [None] + [p["name"] for p in list_profiles(user_data_dir, extensions_dir)[1:]
                     if p["own_extensions"] and p["name"]]


def install(user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None) -> dict:
    """Build the .vsix and install it into the editor: the Default profile and every profile
    with its own extensions."""
    vsix = build_vsix()
    done = []
    for prof in _profile_targets(user_data_dir, extensions_dir):
        backend.install_extension(str(vsix), force=True, user_data_dir=user_data_dir,
                                  extensions_dir=extensions_dir, profile=prof)
        version = installed_version(user_data_dir, extensions_dir, prof)
        if version is None:
            raise RuntimeError(f"The editor accepted {vsix} but does not list {extension_id()} as installed"
                               + (f" in profile {prof!r}" if prof else ""))
        done.append({"profile": prof or "Default", "version": version})
    return {
        "app": backend.current_app(), "name": backend.product().get("nameLong"),
        "installed": extension_id(), "version": done[0]["version"], "profiles": done, "vsix": str(vsix),
        "next": "New windows start the bridge on their own. An already-open window may need "
                "'Developer: Reload Window' before `window list` shows it.",
    }


def uninstall(user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None) -> dict:
    """Remove the bridge from the editor (Default profile and profiles with their own extensions)."""
    for prof in _profile_targets(user_data_dir, extensions_dir):
        if installed_version(user_data_dir, extensions_dir, prof) is not None:
            backend.uninstall_extension(extension_id(), user_data_dir=user_data_dir,
                                        extensions_dir=extensions_dir, profile=prof)
    return {"app": backend.current_app(), "uninstalled": extension_id()}


def status(user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None) -> dict:
    """Whether the bridge is installed (per profile that has its own extensions) and how many
    of the editor's windows it serves right now."""
    profiles = [{"profile": prof or "Default", "installed_version": installed_version(user_data_dir, extensions_dir, prof)}
                for prof in _profile_targets(user_data_dir, extensions_dir)]
    windows = live_windows()
    return {
        "app": backend.current_app(), "name": backend.product().get("nameLong"),
        "extension": extension_id(),
        "packaged_version": manifest()["version"],
        "installed_version": profiles[0]["installed_version"],
        "profiles": profiles,
        "live_windows": len(for_app(windows, strict=False)),
        "live_windows_all_apps": len(windows),
        "home": str(home()),
    }
