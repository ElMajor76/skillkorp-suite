%global pkgdir %{_datadir}/skillkorp-suite
%{!?_udevrulesdir: %global _udevrulesdir %{_prefix}/lib/udev/rules.d}
%{!?_userunitdir: %global _userunitdir %{_prefix}/lib/systemd/user}

Name:           skillkorp-suite
Version:        @VERSION@
Release:        @RELEASE@%{?dist}
Summary:        Driver and settings tools for SkillKorp K20 keyboard and M20 mouse
License:        MIT
URL:            https://github.com/ElMajor76/skillkorp-suite
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3

Requires:       python3 >= 3.10
%if 0%{?suse_version}
Requires:       python3-gobject
Requires:       typelib(Gtk) = 4.0
Requires:       typelib(Adw) = 1
Requires:       udev
Recommends:     typelib(Gtk) = 3.0
Recommends:     typelib(AyatanaAppIndicator3) = 0.1
Recommends:     typelib(Notify) = 0.7
Recommends:     xdotool
%else
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       systemd-udev
Recommends:     gtk3
Recommends:     libayatana-appindicator-gtk3
Recommends:     libnotify
Recommends:     xdotool
%endif

%description
Unofficial Linux driver, command line tool (skillkorpctl), GTK 4 / Libadwaita
application (skillkorp-gui) and tray indicator (skillkorp-tray) for the
SkillKorp K20 Ultimate keyboard and the SkillKorp M20 Ultimate mouse:
lighting, polling rate, debounce, sleep timers, DPI stages, key and button
remapping, profiles with per-application switching and battery levels.

%prep
%autosetup -n %{name}-%{version}

%build
# Pure Python: nothing to build.

%check
HOME="$PWD/.home" python3 -m unittest discover -s tests -t . -q

%install
python3 packaging/build_packages.py --install %{buildroot} --prefix %{_prefix}
# Docs and licence are installed by %%doc / %%license below.
rm -rf %{buildroot}%{_datadir}/doc/%{name} %{buildroot}%{_datadir}/licenses/%{name}

%post
rm -f %{_sysconfdir}/udev/rules.d/99-skillkorp-suite.rules
udevadm control --reload-rules >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=hidraw --action=change >/dev/null 2>&1 || :
# 1.2: only the vendor interface stays open; drop the ACLs that older rules
# gave on the keystroke/pointer interfaces (they would last until replug).
# The uaccess tag stays in the udev database ("sticky") until the device is
# removed, and logind re-applies the ACL from it: replay remove + add for
# those interfaces so the database matches the new rule, then drop the ACL.
stale=""
for n in /sys/class/hidraw/hidraw*; do
    m=$(cat "$n/device/../modalias" 2>/dev/null) || continue
    case "$m" in
        usb:v3151p401[15]*in02|usb:v1D57pFA6[01]*in02) ;;
        usb:v3151p401[15]*|usb:v1D57pFA6[01]*)
            if command -v udevadm >/dev/null 2>&1; then
                udevadm trigger --action=remove "$n" 2>/dev/null || true
                udevadm trigger --action=add "$n" 2>/dev/null || true
            fi
            stale="$stale /dev/${n##*/}" ;;
    esac
done
if [ -n "$stale" ]; then
    command -v udevadm >/dev/null 2>&1 && udevadm settle --timeout=10 2>/dev/null || true
    for d in $stale; do
        command -v setfacl >/dev/null 2>&1 && setfacl -b "$d" 2>/dev/null || true
    done
fi

%postun
udevadm control --reload-rules >/dev/null 2>&1 || :

%files
%license LICENSE
%doc README.md CHANGELOG.md AUDIT.md
%{_bindir}/skillkorpctl
%{_bindir}/skillkorp-gui
%{_bindir}/skillkorp-tray
%{pkgdir}
%config(noreplace) %{_sysconfdir}/xdg/autostart/io.github.skillkorp.suite.tray.desktop
%{_udevrulesdir}/70-skillkorp-suite.rules
%{_userunitdir}/skillkorp-daemon.service
%{_datadir}/applications/io.github.skillkorp.suite.desktop
%{_datadir}/metainfo/io.github.skillkorp.suite.metainfo.xml
%{_datadir}/icons/hicolor/512x512/apps/skillkorp-suite.png
%{_mandir}/man1/skillkorpctl.1*
%{_mandir}/man1/skillkorp-gui.1*
%{_mandir}/man1/skillkorp-tray.1*

%changelog
* Sun Oct 11 2026 nplacide <nplacide95@gmail.com> - 1.2.0-1
- K20 over the 2.4 GHz receiver (receiver protocol captured from the vendor
  software); settings for a sleeping keyboard applied when it wakes up
- udev access limited to the vendor configuration interface
- Tray started with the session and the GUI, its Quit closes both
- Guided hardware test tool (tools/hwtest.py)

* Sun Oct 11 2026 nplacide <nplacide95@gmail.com> - 1.1.0-1
- Protocol fixes verified against the vendor software (keyboard multicolour
  flag, key remap indices, option bits; mouse lighting/power/debounce report)
- Robust hidraw transport, crash-safe configuration, non-blocking GUI and tray
- udev rule renamed 70-skillkorp-suite.rules (uaccess ordering)

* Sun Sep 20 2026 nplacide <nplacide95@gmail.com> - 1.0.1-1
- Keyboard protocol corrections (checksums, command codes)

* Sun Sep 20 2026 nplacide <nplacide95@gmail.com> - 1.0.0-1
- Merge of skillkorp-k20 and skillkorp-m20
