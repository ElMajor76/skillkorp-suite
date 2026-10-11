#!/usr/bin/env python3
"""
SkillKorp Suite - package builder.

    python3 packaging/build_packages.py                 # every format possible here
    python3 packaging/build_packages.py --formats deb,rpm,arch,src
    python3 packaging/build_packages.py --install /     # stage straight into a root (install.sh)
    python3 packaging/build_packages.py --install ~/.local --prefix ~/.local --user

Every format is produced from the same staged tree (stage_tree), so the
.deb, the .rpm files, the Arch package and install.sh install exactly the
same files:

    <prefix>/bin/skillkorpctl, skillkorp-gui, skillkorp-tray   (isolated launchers)
    <prefix>/share/skillkorp-suite/{skillkorp,assets}
    <prefix>/share/applications/io.github.skillkorp.suite.desktop
    <prefix>/share/metainfo/io.github.skillkorp.suite.metainfo.xml
    <prefix>/share/icons/hicolor/512x512/apps/skillkorp-suite.png
    <prefix>/lib/udev/rules.d/70-skillkorp-suite.rules        (system installs)
    <prefix>/lib/systemd/user/skillkorp-daemon.service        (system installs)
"""

import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
DIST_DIR = os.path.join(REPO_DIR, "dist")
PKG_NAME = "skillkorp-suite"
RELEASE = "1"
MAINTAINER = "nplacide <nplacide95@gmail.com>"
URL = "https://github.com/ElMajor76/skillkorp-suite"
SOURCE_DATE_EPOCH = int(os.environ.get("SOURCE_DATE_EPOCH", "1791676800"))  # 2026-10-11, reproducible builds


def read_version() -> str:
    with open(os.path.join(REPO_DIR, "skillkorp", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
    if not m:
        raise SystemExit("Version introuvable dans skillkorp/__init__.py")
    return m.group(1)


VERSION = read_version()

LAUNCHER = """#!/bin/sh
# SkillKorp Suite launcher. -I: isolated mode, so neither the current
# directory nor PYTHONPATH can shadow the installed modules.
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, "{libdir}"); from skillkorp.{module} import main; sys.exit(main())' "$@"
"""
LAUNCHERS = {"skillkorpctl": "cli", "skillkorp-gui": "gui", "skillkorp-tray": "tray"}

SHORT_DESC = "driver and settings tools for SkillKorp K20 keyboard and M20 mouse"
LONG_DESC = (
    "Unofficial Linux driver, command line tool (skillkorpctl), GTK 4 / Libadwaita\n"
    "application (skillkorp-gui) and tray indicator (skillkorp-tray) for the\n"
    "SkillKorp K20 Ultimate keyboard and the SkillKorp M20 Ultimate mouse:\n"
    "lighting, polling rate, debounce, sleep timers, DPI stages, key and button\n"
    "remapping, profiles with per-application switching and battery levels."
)


def log(msg: str):
    print(msg, flush=True)


# =============================================================================
# Staging
# =============================================================================

def _copy(src: str, dst: str, mode: int = 0o644):
    os.makedirs(os.path.dirname(dst), mode=0o755, exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, mode)


AUTOSTART_NAME = "io.github.skillkorp.suite.tray.desktop"
GNOME_EXTENSION = "skillkorp-focus@skillkorp.github.io"
CONFFILES = ["/etc/xdg/autostart/" + AUTOSTART_NAME]


def stage_tree(root: str, prefix: str = "/usr", system: bool = True) -> None:
    """Install every file of the suite under root + prefix."""
    base = root.rstrip("/") + prefix
    libdir_installed = os.path.join(prefix, "share", PKG_NAME)
    libdir = os.path.join(base, "share", PKG_NAME)

    pkg_dst = os.path.join(libdir, "skillkorp")
    if os.path.isdir(pkg_dst):
        shutil.rmtree(pkg_dst)  # reinstall must not leave stale modules (or nest a second copy)
    for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_DIR, "skillkorp")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        rel = os.path.relpath(dirpath, os.path.join(REPO_DIR, "skillkorp"))
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                _copy(os.path.join(dirpath, fn), os.path.join(pkg_dst, rel, fn))

    for fn in sorted(os.listdir(os.path.join(REPO_DIR, "assets"))):
        _copy(os.path.join(REPO_DIR, "assets", fn), os.path.join(libdir, "assets", fn))

    for name, module in LAUNCHERS.items():
        path = os.path.join(base, "bin", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(LAUNCHER.format(libdir=libdir_installed, module=module))
        os.chmod(path, 0o755)

    _copy(os.path.join(REPO_DIR, "io.github.skillkorp.suite.desktop"),
          os.path.join(base, "share", "applications", "io.github.skillkorp.suite.desktop"))
    _copy(os.path.join(SCRIPT_DIR, "common", "io.github.skillkorp.suite.metainfo.xml"),
          os.path.join(base, "share", "metainfo", "io.github.skillkorp.suite.metainfo.xml"))
    _copy(os.path.join(REPO_DIR, "assets", "skillkorp-suite.png"),
          os.path.join(base, "share", "icons", "hicolor", "512x512", "apps", "skillkorp-suite.png"))
    # GNOME Shell extension giving the focused application (Wayland auto-switch).
    ext_src = os.path.join(REPO_DIR, "gnome-extension", GNOME_EXTENSION)
    for fn in sorted(os.listdir(ext_src)):
        _copy(os.path.join(ext_src, fn), os.path.join(base, "share", "gnome-shell", "extensions", GNOME_EXTENSION, fn))
    _copy(os.path.join(REPO_DIR, "LICENSE"), os.path.join(base, "share", "licenses", PKG_NAME, "LICENSE"))
    _copy(os.path.join(REPO_DIR, "README.md"), os.path.join(base, "share", "doc", PKG_NAME, "README.md"))
    _copy(os.path.join(REPO_DIR, "CHANGELOG.md"), os.path.join(base, "share", "doc", PKG_NAME, "CHANGELOG.md"))
    _copy(os.path.join(REPO_DIR, "AUDIT.md"), os.path.join(base, "share", "doc", PKG_NAME, "AUDIT.md"))
    for page in sorted(os.listdir(os.path.join(SCRIPT_DIR, "common", "man"))):
        dst = os.path.join(base, "share", "man", "man1", page + ".gz")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(os.path.join(SCRIPT_DIR, "common", "man", page), "rb") as fi, \
                gzip.GzipFile(dst, "wb", mtime=SOURCE_DATE_EPOCH) as fo:
            fo.write(fi.read())
        os.chmod(dst, 0o644)
    if system:
        # Session autostart of the tray for every user (a user can mask it from
        # the tray/GUI/CLI, which writes Hidden=true in ~/.config/autostart).
        _copy(os.path.join(SCRIPT_DIR, "common", AUTOSTART_NAME),
              os.path.join(root.rstrip("/") + "/etc", "xdg", "autostart", AUTOSTART_NAME))
        _copy(os.path.join(REPO_DIR, "udev", "70-skillkorp-suite.rules"),
              os.path.join(base, "lib", "udev", "rules.d", "70-skillkorp-suite.rules"))
        _copy(os.path.join(SCRIPT_DIR, "common", "skillkorp-daemon.service"),
              os.path.join(base, "lib", "systemd", "user", "skillkorp-daemon.service"))


def normalize_tree(root: str, skip: str = "") -> None:
    """chmod 755 dirs / executables and 644 other files, fixed mtimes (umask independent)."""
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        os.utime(dirpath, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH))
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if skip and full.startswith(skip):
                continue
            if not os.path.islink(full):
                os.chmod(full, 0o755 if os.stat(full).st_mode & stat.S_IXUSR else 0o644)
                os.utime(full, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH))


def _normalize(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = "root"
    tarinfo.mtime = SOURCE_DATE_EPOCH
    if tarinfo.isdir():
        tarinfo.mode = 0o755
    elif tarinfo.isfile():
        tarinfo.mode = 0o755 if tarinfo.mode & stat.S_IXUSR else 0o644
    return tarinfo


def _walk_sorted(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        yield dirpath, sorted(filenames)


# =============================================================================
# Source tarball (also the RPM/Arch source)
# =============================================================================

SOURCE_ITEMS = ["skillkorp", "bin", "assets", "udev", "packaging", "tests", "tools", "gnome-extension", "install.sh", "LICENSE", "README.md",
                "CHANGELOG.md", "AUDIT.md", "io.github.skillkorp.suite.desktop", "requirements-dev.txt"]


def build_source_tarball() -> str:
    log("\n📦 Archive source")
    tar_path = os.path.join(DIST_DIR, f"{PKG_NAME}-{VERSION}.tar.gz")
    prefix = f"{PKG_NAME}-{VERSION}"

    def flt(ti):
        if "__pycache__" in ti.name or ti.name.endswith((".pyc", ".pyo")):
            return None
        # The PKGBUILD carries the checksum of this archive: keeping it out
        # avoids a loop where every build changes both.
        if ti.name == f"{prefix}/packaging/arch/PKGBUILD":
            return None
        return _normalize(ti)

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for item in SOURCE_ITEMS:
            path = os.path.join(REPO_DIR, item)
            if not os.path.exists(path):
                continue
            if os.path.isdir(path):
                for dirpath, filenames in _walk_sorted(path):
                    for fn in filenames:
                        full = os.path.join(dirpath, fn)
                        tar.add(full, arcname=os.path.join(prefix, os.path.relpath(full, REPO_DIR)), filter=flt)
            else:
                tar.add(path, arcname=os.path.join(prefix, item), filter=flt)
    with open(tar_path, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=SOURCE_DATE_EPOCH) as gz:
            gz.write(raw.getvalue())
    log(f"  ✓ {tar_path}")
    return tar_path


# =============================================================================
# Debian / Ubuntu
# =============================================================================

DEB_DEPENDS = ("python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.2), udev")
DEB_RECOMMENDS = ("gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1, "
                  "gir1.2-notify-0.7, xdotool")


def build_deb() -> str:
    log("\n📦 Paquet DEB (Debian, Ubuntu, Linux Mint, Pop!_OS)")
    if not shutil.which("dpkg-deb"):
        log("  ⚠ dpkg-deb introuvable : paquet .deb non généré.")
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "root")
        stage_tree(root)
        # Debian layout: docs/licence go to /usr/share/doc/<pkg>/copyright
        lic_dir = os.path.join(root, "usr", "share", "licenses")
        doc_dir = os.path.join(root, "usr", "share", "doc", PKG_NAME)
        shutil.rmtree(lic_dir)
        with open(os.path.join(REPO_DIR, "LICENSE"), encoding="utf-8") as f:
            license_text = f.read()
        with open(os.path.join(doc_dir, "copyright"), "w", encoding="utf-8") as f:
            f.write("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
                    f"Upstream-Name: {PKG_NAME}\nSource: {URL}\n\nFiles: *\nCopyright: SkillKorp Linux project\n"
                    "License: MIT\n" + "".join(f" {line}\n" if line.strip() else " .\n"
                                                for line in license_text.splitlines()))
        os.replace(os.path.join(doc_dir, "CHANGELOG.md"), os.path.join(doc_dir, "changelog"))
        for name in ("changelog", "README.md", "AUDIT.md"):
            src = os.path.join(doc_dir, name)
            with open(src, "rb") as fi, gzip.GzipFile(src + ".gz", "wb", mtime=SOURCE_DATE_EPOCH) as fo:
                fo.write(fi.read())
            os.remove(src)
        changelog = (f"{PKG_NAME} ({VERSION}-{RELEASE}) unstable; urgency=medium\n\n"
                     f"  * Release {VERSION}, see /usr/share/doc/{PKG_NAME}/changelog.gz\n\n"
                     f" -- {MAINTAINER}  {time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(SOURCE_DATE_EPOCH))}\n")
        with gzip.GzipFile(os.path.join(doc_dir, "changelog.Debian.gz"), "wb", mtime=SOURCE_DATE_EPOCH) as fo:
            fo.write(changelog.encode())

        debian = os.path.join(root, "DEBIAN")
        os.makedirs(debian)
        size_kb = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(root) for f in fs) // 1024 + 1
        control = (f"Package: {PKG_NAME}\nVersion: {VERSION}-{RELEASE}\nSection: utils\nPriority: optional\n"
                   f"Architecture: all\nInstalled-Size: {size_kb}\nDepends: {DEB_DEPENDS}\n"
                   f"Recommends: {DEB_RECOMMENDS}\nMaintainer: {MAINTAINER}\nHomepage: {URL}\n"
                   f"Description: {SHORT_DESC}\n" + "".join(f" {line}\n" for line in LONG_DESC.splitlines()))
        with open(os.path.join(debian, "control"), "w", encoding="utf-8") as f:
            f.write(control)
        with open(os.path.join(debian, "conffiles"), "w", encoding="utf-8") as f:
            f.write("".join(c + "\n" for c in CONFFILES))
        for script in ("postinst", "postrm", "preinst"):
            src = os.path.join(SCRIPT_DIR, "deb", script)
            if os.path.exists(src):
                _copy(src, os.path.join(debian, script), 0o755)
        md5 = []
        for dirpath, filenames in _walk_sorted(root):
            if dirpath.startswith(debian):
                continue
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if not os.path.islink(full):
                    with open(full, "rb") as fh:
                        md5.append(f"{hashlib.md5(fh.read()).hexdigest()}  {os.path.relpath(full, root)}\n")
        with open(os.path.join(debian, "md5sums"), "w") as f:
            f.writelines(md5)
        normalize_tree(root, skip=debian)
        out = os.path.join(DIST_DIR, f"{PKG_NAME}_{VERSION}-{RELEASE}_all.deb")
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(SOURCE_DATE_EPOCH))
        res = subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", root, out],
                             capture_output=True, text=True, env=env)
        if res.returncode != 0:
            log(f"  ❌ dpkg-deb : {res.stderr.strip()}")
            return ""
    log(f"  ✓ {out}")
    return out


# =============================================================================
# RPM (Fedora / RHEL / openSUSE)
# =============================================================================

def build_rpm(tarball: str, flavour: str) -> str:
    label = {"fedora": "Fedora, RHEL, AlmaLinux, Rocky", "suse": "openSUSE Tumbleweed / Leap 16"}[flavour]
    log(f"\n📦 Paquet RPM ({label})")
    if not shutil.which("rpmbuild"):
        log("  ⚠ rpmbuild introuvable : paquet .rpm non généré.")
        return ""
    with tempfile.TemporaryDirectory() as top:
        for d in ("SOURCES", "SPECS", "BUILD", "RPMS", "SRPMS", "BUILDROOT"):
            os.makedirs(os.path.join(top, d))
        shutil.copy2(tarball, os.path.join(top, "SOURCES"))
        spec = os.path.join(top, "SPECS", f"{PKG_NAME}.spec")
        with open(os.path.join(SCRIPT_DIR, "rpm", f"{PKG_NAME}.spec"), encoding="utf-8") as f:
            content = f.read().replace("@VERSION@", VERSION).replace("@RELEASE@", RELEASE)
        with open(spec, "w", encoding="utf-8") as f:
            f.write(content)
        # --nodeps: the build host may not be RPM based (empty rpmdb on Debian/Ubuntu CI);
        # the only build requirement, python3, is exercised by %check anyway.
        cmd = ["rpmbuild", "-bb", "--nodeps", "--define", f"_topdir {top}", "--define", "_binary_payload w9.xzdio",
               "--define", f"dist .{'suse' if flavour == 'suse' else 'fc'}",
               "--define", "_build_id_links none", "--define", "debug_package %{nil}",
               "--define", "source_date_epoch_from_changelog 0",
               "--define", "use_source_date_epoch_as_buildtime 1", spec]
        if flavour == "suse":
            cmd[2:2] = ["--define", "suse_version 1600"]
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(SOURCE_DATE_EPOCH))
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if res.returncode != 0:
            log(f"  ❌ rpmbuild :\n{res.stderr.strip()[-3000:]}")
            return ""
        out = ""
        for dirpath, _, files in os.walk(os.path.join(top, "RPMS")):
            for fn in files:
                if fn.endswith(".rpm"):
                    out = os.path.join(DIST_DIR, fn)
                    shutil.copy2(os.path.join(dirpath, fn), out)
    log(f"  ✓ {out}")
    return out


# =============================================================================
# Arch Linux
# =============================================================================

ARCH_DEPENDS = ["python", "python-gobject", "gtk4", "libadwaita", "systemd"]
ARCH_OPTDEPENDS = [
    "gtk3: indicateur de la barre système (skillkorp-tray)",
    "libayatana-appindicator: indicateur de la barre système (skillkorp-tray)",
    "libnotify: notifications de batterie et de bascule de profil",
    "xdotool: bascule automatique des profils sous X11",
]


def write_pkgbuild(tarball: str) -> str:
    with open(tarball, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    deps = " ".join(f"'{d}'" for d in ARCH_DEPENDS)
    opt = "\n".join(f"    '{d}'" for d in ARCH_OPTDEPENDS)
    backup = " ".join(f"'{c.lstrip('/')}'" for c in CONFFILES)
    content = f"""# Maintainer: {MAINTAINER}
pkgname={PKG_NAME}
pkgver={VERSION}
pkgrel={RELEASE}
pkgdesc="{SHORT_DESC}"
arch=('any')
url="{URL}"
license=('MIT')
depends=({deps})
optdepends=(
{opt}
)
checkdepends=('python')
backup=({backup})
source=("$pkgname-$pkgver.tar.gz")
sha256sums=('{sha}')

check() {{
    cd "$srcdir/$pkgname-$pkgver"
    HOME="$srcdir/home" python -m unittest discover -s tests -t . -q
}}

package() {{
    cd "$srcdir/$pkgname-$pkgver"
    python packaging/build_packages.py --install "$pkgdir"
}}
"""
    path = os.path.join(DIST_DIR, "PKGBUILD")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(os.path.join(SCRIPT_DIR, "arch", "PKGBUILD"), "w", encoding="utf-8") as f:
        f.write(content)
    return path


def build_arch_package() -> str:
    """Build a pacman package without makepkg (needs bsdtar for the .MTREE)."""
    log("\n📦 Paquet Arch Linux / Manjaro / EndeavourOS")
    bsdtar = shutil.which("bsdtar")
    zstd = shutil.which("zstd")
    if not (bsdtar and zstd):
        log("  ⚠ bsdtar/zstd introuvables : seul le PKGBUILD est fourni (makepkg -si).")
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "pkg")
        stage_tree(root)
        size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(root) for f in fs)
        lines = [f"pkgname = {PKG_NAME}", f"pkgbase = {PKG_NAME}", f"pkgver = {VERSION}-{RELEASE}",
                 f"pkgdesc = {SHORT_DESC}", f"url = {URL}", f"builddate = {SOURCE_DATE_EPOCH}",
                 f"packager = {MAINTAINER}", f"size = {size}", "arch = any", "license = MIT"]
        lines += [f"depend = {d}" for d in ARCH_DEPENDS]
        lines += [f"optdepend = {d}" for d in ARCH_OPTDEPENDS]
        lines += [f"backup = {c.lstrip('/')}" for c in CONFFILES]
        with open(os.path.join(root, ".PKGINFO"), "w", encoding="utf-8") as f:
            f.write("# Generated by SkillKorp Suite build_packages.py\n" + "\n".join(lines) + "\n")
        with open(os.path.join(root, ".INSTALL"), "w", encoding="utf-8") as f:
            with open(os.path.join(SCRIPT_DIR, "common", "restrict-access.sh"), encoding="utf-8") as snip:
                restrict = "".join("  " + line for line in snip.read().splitlines(True)[2:])
            f.write("post_install() {\n  udevadm control --reload-rules 2>/dev/null || true\n"
                    "  udevadm trigger --subsystem-match=hidraw --action=change 2>/dev/null || true\n"
                    + restrict + "}\n"
                    "post_upgrade() {\n  post_install\n}\npost_remove() {\n  post_install\n}\n")
        normalize_tree(root)
        entries = [".PKGINFO", ".INSTALL"] + sorted(e for e in os.listdir(root) if not e.startswith("."))
        env = dict(os.environ, LANG="C")
        subprocess.run([bsdtar, "-czf", ".MTREE", "--format=mtree", "--uid", "0", "--gid", "0",
                        "--options=!all,use-set,type,uid,gid,mode,time,size,md5,sha256,link"] + entries,
                       cwd=root, check=True, env=env)
        os.chmod(os.path.join(root, ".MTREE"), 0o644)
        os.utime(os.path.join(root, ".MTREE"), (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH))
        out = os.path.join(DIST_DIR, f"{PKG_NAME}-{VERSION}-{RELEASE}-any.pkg.tar.zst")
        tar_tmp = os.path.join(tmp, "pkg.tar")
        subprocess.run([bsdtar, "-cf", tar_tmp, "--uid", "0", "--gid", "0", "--uname", "root", "--gname", "root",
                        ".MTREE"] + entries, cwd=root, check=True, env=env)
        subprocess.run([zstd, "-q", "-19", "-f", tar_tmp, "-o", out], check=True)
    log(f"  ✓ {out}")
    return out


# =============================================================================
# Entry point
# =============================================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--formats", default="src,deb,rpm,arch", help="liste parmi src,deb,rpm,arch")
    parser.add_argument("--install", metavar="ROOT", help="installer l'arborescence dans ROOT au lieu de créer des paquets")
    parser.add_argument("--prefix", default="/usr")
    parser.add_argument("--user", action="store_true", help="installation utilisateur (sans udev ni service)")
    args = parser.parse_args(argv)

    if args.install:
        root = os.path.abspath(os.path.expanduser(args.install))
        prefix = os.path.expanduser(args.prefix)
        if args.user:
            # user installs: root is "/" and prefix is ~/.local
            stage_tree("/", prefix=os.path.abspath(prefix), system=False)
        else:
            stage_tree(root, prefix=prefix, system=True)
        return 0

    formats = {f.strip() for f in args.formats.split(",") if f.strip()}
    os.makedirs(DIST_DIR, exist_ok=True)
    log("=" * 60)
    log(f"  Paquets SkillKorp Suite {VERSION}-{RELEASE}")
    log("=" * 60)
    built = []
    tarball = build_source_tarball()
    if "src" in formats:
        built.append(tarball)
    if "deb" in formats:
        built.append(build_deb())
    if "rpm" in formats:
        built.append(build_rpm(tarball, "fedora"))
        built.append(build_rpm(tarball, "suse"))
    if "arch" in formats:
        built.append(write_pkgbuild(tarball))
        built.append(build_arch_package())
    built = [b for b in built if b]
    log("\n" + "=" * 60)
    for path in built:
        log(f"  • {os.path.relpath(path, REPO_DIR)} ({os.path.getsize(path) / 1024:.1f} Kio)")
    with open(os.path.join(DIST_DIR, "SHA256SUMS"), "w") as f:
        for path in sorted(built):
            with open(path, "rb") as fh:
                f.write(f"{hashlib.sha256(fh.read()).hexdigest()}  {os.path.basename(path)}\n")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
