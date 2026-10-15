#!/usr/bin/env python3
"""Download and vendor the browser libraries used by the UI.

Standalone (standard library only). For every pinned npm package it:

1. reads the version metadata from https://registry.npmjs.org,
2. downloads the tarball and verifies ``dist.integrity`` (sha512, SRI format),
3. extracts only the files the app needs into ``src/planificador/static/vendor/<package>/``,
4. copies the package licence (or writes a notice when the tarball ships none),
5. writes ``static/vendor/VERSIONS.txt`` with versions, licences and checksums.

The vendored files are committed to the repository, so end users never need Node or network access.

Usage::

    python scripts/vendor_assets.py            # download + extract everything
    python scripts/vendor_assets.py --check    # verify the vendored tree matches the pins (no network)
    python scripts/vendor_assets.py --latest   # show the registry "latest" tag next to each pin

Proxies and certificates: ``urllib`` honours ``HTTPS_PROXY``/``NO_PROXY``; a CA bundle is taken from
``SSL_CERT_FILE``, ``REQUESTS_CA_BUNDLE`` or ``CURL_CA_BUNDLE`` when set.

To upgrade a library, change its ``version`` below, run the script and review the diff.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import sys
import tarfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY = "https://registry.npmjs.org"
ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "src" / "planificador" / "static" / "vendor"
USER_AGENT = "planificador-redes-vendor/1.0 (+stdlib urllib)"
TIMEOUT = 120

MIT_TEXT = """MIT License

Copyright (c) {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


@dataclass(frozen=True)
class Package:
    """One npm package to vendor.

    ``files`` maps a path inside the tarball (relative to ``package/``) to a destination path relative to
    ``static/vendor/<dest>/``. ``licenses`` lists candidate licence files (first existing ones are copied).
    ``license_fallback`` is used when the tarball contains no licence file (holder name for the MIT text).
    """

    name: str
    version: str
    dest: str
    files: dict[str, str]
    licenses: tuple[str, ...] = ("LICENSE", "LICENSE.md", "LICENSE.txt", "license")
    license_dest_prefix: str = ""
    license_fallback: str | None = None
    extra_notices: tuple[str, ...] = field(default_factory=tuple)
    # Optional hook (tarfile, destination dir) -> list of extra files written.
    post: Callable[[tarfile.TarFile, Path], list[str]] | None = None


def _kebab(pascal: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", pascal)
    s = re.sub(r"([A-Za-z])([0-9])", r"\1-\2", s)
    return s.lower()


def _lucide_aliases(tar: tarfile.TarFile, dest_dir: Path) -> list[str]:
    """Write aliases.json mapping legacy/alternative icon names (e.g. "trash-2") to sprite ids ("trash").

    Built from the ESM index of the package, whose lines look like
    ``export { default as Trash, default as Trash2 } from './icons/trash.mjs';``.
    """
    content = _read_member(tar, "dist/esm/lucide-static.mjs")
    if content is None:
        return []
    aliases: dict[str, str] = {}
    pattern = re.compile(r"export \{([^}]*)\} from '\./icons/([a-z0-9-]+)\.mjs'")
    for names, canonical in pattern.findall(content.decode("utf-8")):
        for alias in re.findall(r"default as (\w+)", names):
            if alias.endswith("Icon") or alias.startswith("Lucide"):
                continue
            for candidate in {_kebab(alias), re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", alias).lower()}:
                if candidate != canonical:
                    aliases.setdefault(candidate, canonical)
    (dest_dir / "aliases.json").write_text(
        json.dumps(dict(sorted(aliases.items())), indent=0, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return ["aliases.json"]


PACKAGES: tuple[Package, ...] = (
    Package(
        name="fullcalendar",
        version="6.1.21",
        dest="fullcalendar",
        files={"index.global.min.js": "index.global.min.js"},
    ),
    # The Spanish locale lives in @fullcalendar/core; it is stored next to the bundle.
    Package(
        name="@fullcalendar/core",
        version="6.1.21",
        dest="fullcalendar",
        files={"locales/es.global.min.js": "locales/es.global.min.js"},
        license_dest_prefix="core-",
    ),
    Package(
        name="alpinejs",
        version="3.17.4",
        dest="alpinejs",
        files={"dist/cdn.min.js": "cdn.min.js"},
        # The npm tarball ships no licence file; package.json declares MIT (author Caleb Porzio).
        license_fallback="2019-present Caleb Porzio and contributors",
    ),
    Package(
        name="htmx.org",
        version="2.0.11",
        dest="htmx.org",
        files={"dist/htmx.min.js": "htmx.min.js"},
    ),
    Package(
        name="chart.js",
        version="4.5.1",
        dest="chart.js",
        files={"dist/chart.umd.min.js": "chart.umd.min.js"},
    ),
    # Latest stable lucide-static as of 2026-10-15. The sprite holds every icon as a <symbol>; the server
    # inlines icons from it (templating.icon) and client code may use <use href="...sprite.svg#name">.
    Package(
        name="lucide-static",
        version="1.57.0",
        dest="lucide-static",
        files={"sprite.svg": "sprite.svg"},
        post=_lucide_aliases,
    ),
    # Latest stable simple-icons as of 2026-10-15. Only the network logos are kept.
    Package(
        name="simple-icons",
        version="16.35.0",
        dest="simple-icons",
        files={
            "icons/facebook.svg": "facebook.svg",
            "icons/instagram.svg": "instagram.svg",
            "icons/tiktok.svg": "tiktok.svg",
            "icons/youtube.svg": "youtube.svg",
        },
        extra_notices=("DISCLAIMER.md",),
    ),
)


# --------------------------------------------------------------------------------------------- network


def _ssl_context() -> ssl.SSLContext:
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        cafile = os.environ.get(var)
        if cafile and Path(cafile).is_file():
            return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


_OPENER: urllib.request.OpenerDirector | None = None


def _opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is None:
        # ProxyHandler() without arguments reads HTTPS_PROXY / NO_PROXY from the environment.
        _OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )
    return _OPENER


def _get(url: str, accept: str = "*/*") -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org":
        raise RuntimeError(f"refusing to download from unexpected URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with _opener().open(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _metadata(name: str, version: str) -> dict:
    # Scoped names are sent as "@scope%2Fname", which the registry accepts.
    url = f"{REGISTRY}/{urllib.parse.quote(name, safe='@')}/{version}"
    return json.loads(_get(url, accept="application/json"))


def _dist_tags(name: str) -> dict:
    url = f"{REGISTRY}/{urllib.parse.quote(name, safe='@')}"
    data = json.loads(_get(url, accept="application/vnd.npm.install-v1+json"))
    return data.get("dist-tags", {})


def _verify_integrity(data: bytes, integrity: str) -> str:
    """Check an SRI string (``sha512-<base64>``). Returns the matching SRI entry."""
    for entry in integrity.split():
        algo, _, expected = entry.partition("-")
        if algo != "sha512":
            continue
        actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
        if actual != expected:
            raise RuntimeError(f"integrity mismatch: expected {expected[:24]}..., got {actual[:24]}...")
        return entry
    raise RuntimeError(f"no sha512 integrity in registry metadata: {integrity!r}")


# --------------------------------------------------------------------------------------------- extract


def _read_member(tar: tarfile.TarFile, inner: str) -> bytes | None:
    try:
        member = tar.getmember(f"package/{inner}")
    except KeyError:
        return None
    if not member.isfile():
        return None
    fh = tar.extractfile(member)
    return fh.read() if fh else None


def _safe_dest(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if base.resolve() not in target.parents:
        raise RuntimeError(f"unsafe destination path: {rel}")
    return target


def vendor_package(pkg: Package, cleaned: set[str]) -> dict:
    meta = _metadata(pkg.name, pkg.version)
    dist = meta.get("dist") or {}
    tarball_url = dist.get("tarball", "")
    integrity = dist.get("integrity", "")
    print(f"  {pkg.name}@{pkg.version}: downloading {tarball_url}")
    data = _get(tarball_url, accept="application/octet-stream")
    sri = _verify_integrity(data, integrity)
    print(f"    integrity OK ({sri[:26]}...)")

    dest_dir = VENDOR_DIR / pkg.dest
    if pkg.dest not in cleaned:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        cleaned.add(pkg.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for inner, rel in pkg.files.items():
            content = _read_member(tar, inner)
            if content is None:
                raise RuntimeError(f"{pkg.name}@{pkg.version}: file not found in tarball: {inner}")
            out = _safe_dest(dest_dir, rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)
            written.append(rel)
        if pkg.post is not None:
            written.extend(pkg.post(tar, dest_dir))

        license_files: list[str] = []
        for candidate in pkg.licenses:
            content = _read_member(tar, candidate)
            if content is not None:
                rel = f"{pkg.license_dest_prefix}{candidate}"
                _safe_dest(dest_dir, rel).write_bytes(content)
                license_files.append(rel)
                break
        for notice in pkg.extra_notices:
            content = _read_member(tar, notice)
            if content is not None:
                rel = f"{pkg.license_dest_prefix}{notice}"
                _safe_dest(dest_dir, rel).write_bytes(content)
                license_files.append(rel)

    declared = meta.get("license") or "UNKNOWN"
    if not license_files:
        if pkg.license_fallback and declared == "MIT":
            rel = f"{pkg.license_dest_prefix}LICENSE"
            text = (
                f"Licence notice generated by scripts/vendor_assets.py: the npm tarball of {pkg.name}@"
                f'{pkg.version} ships no licence file; its package.json declares "MIT".\n\n'
                + MIT_TEXT.format(holder=pkg.license_fallback)
            )
            _safe_dest(dest_dir, rel).write_text(text, encoding="utf-8")
            license_files.append(rel)
        else:
            raise RuntimeError(f"{pkg.name}@{pkg.version}: no licence file found")

    return {
        "name": pkg.name,
        "version": pkg.version,
        "license": declared,
        "integrity": sri,
        "tarball": tarball_url,
        "dest": pkg.dest,
        "files": written,
        "license_files": license_files,
    }


def write_versions(entries: list[dict]) -> None:
    lines = [
        "# Vendored browser libraries - generated by scripts/vendor_assets.py. Do not edit by hand.",
        "#",
        "# package@version | licence | directory | files | licence files | integrity (sha512, verified)",
        "",
    ]
    for e in entries:
        lines.append(f"{e['name']}@{e['version']}")
        lines.append(f"  licence:   {e['license']}")
        lines.append(f"  directory: static/vendor/{e['dest']}/")
        lines.append(f"  files:     {', '.join(e['files'])}")
        lines.append(f"  licences:  {', '.join(e['license_files'])}")
        lines.append(f"  tarball:   {e['tarball']}")
        lines.append(f"  integrity: {e['integrity']}")
        lines.append("")
    (VENDOR_DIR / "VERSIONS.txt").write_text("\n".join(lines), encoding="utf-8")


def check() -> int:
    """Offline check: every pinned file exists and VERSIONS.txt lists the pinned versions."""
    problems: list[str] = []
    versions_file = VENDOR_DIR / "VERSIONS.txt"
    versions = versions_file.read_text(encoding="utf-8") if versions_file.exists() else ""
    for pkg in PACKAGES:
        if f"{pkg.name}@{pkg.version}\n" not in versions:
            problems.append(f"VERSIONS.txt does not list {pkg.name}@{pkg.version}")
        for rel in pkg.files.values():
            if not (VENDOR_DIR / pkg.dest / rel).is_file():
                problems.append(f"missing file: static/vendor/{pkg.dest}/{rel}")
    for p in problems:
        print(f"  ERROR {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def show_latest() -> int:
    for pkg in PACKAGES:
        tags = _dist_tags(pkg.name)
        latest = tags.get("latest", "?")
        flag = "" if latest == pkg.version else "   <- newer tag available (check compatibility first)"
        print(f"  {pkg.name:22} pinned {pkg.version:10} latest {latest}{flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify vendored files offline")
    parser.add_argument("--latest", action="store_true", help="show registry 'latest' tags")
    args = parser.parse_args(argv)

    if args.check:
        return check()
    if args.latest:
        return show_latest()

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Vendoring into {VENDOR_DIR}")
    entries: list[dict] = []
    cleaned: set[str] = set()
    try:
        for pkg in PACKAGES:
            entries.append(vendor_package(pkg, cleaned))
    except Exception as exc:  # noqa: BLE001 - report any failure clearly and exit non-zero
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    write_versions(entries)
    print(f"Wrote {VENDOR_DIR / 'VERSIONS.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
