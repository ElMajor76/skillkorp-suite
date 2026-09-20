%{!?_udevrulesdir: %global _udevrulesdir %{_prefix}/lib/udev/rules.d}
%global pkgdir %{_datadir}/skillkorp-suite

Name:           skillkorp-suite
Version:        1.0.0
Release:        1%{?dist}
Summary:        Pilote et interface graphique Linux unifiés pour le clavier SkillKorp K20 Ultimate et la souris SkillKorp M20 Ultimate
License:        MIT
URL:            https://github.com/ElMajor76/skillkorp-suite
BuildArch:      noarch

Requires:       python3
Requires:       python3-gobject
Requires:       libadwaita
Requires:       systemd-udev
Recommends:     libayatana-appindicator-gtk3
Recommends:     libappindicator-gtk3

%description
Pilote natif sous Linux, utilitaire CLI unifié (skillkorpctl), application
GTK 4 / Libadwaita (skillkorp-gui) et applet systray unique (skillkorp-tray)
pour le clavier gaming compact 75%% SkillKorp K20 Ultimate (SoC Yichip YC3121)
et la souris gaming sans fil SkillKorp M20 Ultimate (capteur PixArt PAW3395).
Remplace les projets séparés skillkorp-k20 et skillkorp-m20.

%prep
# Pas d'étape de compilation requise

%build
# Scripts Python, rien à compiler

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}%{pkgdir}
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_udevrulesdir}
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/256x256/apps

# Paquet Python "skillkorp" complet
cp -r %{_sourcedir}/skillkorp %{buildroot}%{pkgdir}/skillkorp
find %{buildroot}%{pkgdir}/skillkorp -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

install -m 0755 %{_sourcedir}/bin/skillkorpctl %{buildroot}%{pkgdir}/skillkorpctl
cp -r %{_sourcedir}/assets %{buildroot}%{pkgdir}/

# Liens symboliques dans /usr/bin
ln -s %{pkgdir}/skillkorpctl %{buildroot}%{_bindir}/skillkorpctl
cat > %{buildroot}%{_bindir}/skillkorp-gui << EOF
#!/bin/sh
exec env PYTHONPATH="%{pkgdir}:\$PYTHONPATH" python3 -m skillkorp.gui "\$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/skillkorp-gui
cat > %{buildroot}%{_bindir}/skillkorp-tray << EOF
#!/bin/sh
exec env PYTHONPATH="%{pkgdir}:\$PYTHONPATH" python3 -m skillkorp.tray "\$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/skillkorp-tray

# Règle udev, lanceur .desktop et icône
install -m 0644 %{_sourcedir}/udev/99-skillkorp-suite.rules %{buildroot}%{_udevrulesdir}/99-skillkorp-suite.rules
install -m 0644 %{_sourcedir}/io.github.skillkorp.suite.desktop %{buildroot}%{_datadir}/applications/io.github.skillkorp.suite.desktop
install -m 0644 %{_sourcedir}/assets/skillkorp-suite.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/skillkorp-suite.png

%post
udevadm control --reload-rules >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=hidraw >/dev/null 2>&1 || :
update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :

%postun
udevadm control --reload-rules >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=hidraw >/dev/null 2>&1 || :
update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :

%files
%{pkgdir}
%{_bindir}/skillkorpctl
%{_bindir}/skillkorp-gui
%{_bindir}/skillkorp-tray
%{_udevrulesdir}/99-skillkorp-suite.rules
%{_datadir}/applications/io.github.skillkorp.suite.desktop
%{_datadir}/icons/hicolor/256x256/apps/skillkorp-suite.png

%changelog
* Sun Sep 20 2026 nplacide <nplacide95@gmail.com> - 1.0.0-1
- Fusion de skillkorp-k20 et skillkorp-m20 en une application unique
- CLI unifiée skillkorpctl (sous-commandes keyboard/mouse), GUI et tray uniques
- Migration automatique des profils existants au premier lancement
