// SkillKorp Suite — focused application for automatic profile switching.
//
// GNOME Shell on Wayland gives the focused window only to its extensions.
// This one exports a single read-only D-Bus method on the session bus
// (inside org.gnome.Shell) returning the focused window's WM class and
// application id. Window titles and contents are never exposed.
//
//   gdbus call --session --dest org.gnome.Shell \
//       --object-path /io/github/skillkorp/Focus \
//       --method io.github.skillkorp.Focus.GetFocused

import Gio from 'gi://Gio';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const OBJECT_PATH = '/io/github/skillkorp/Focus';
const INTERFACE = `
<node>
  <interface name="io.github.skillkorp.Focus">
    <method name="GetFocused">
      <arg type="s" direction="out" name="wm_class"/>
      <arg type="s" direction="out" name="app_id"/>
    </method>
  </interface>
</node>`;

export default class SkillkorpFocusExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
    }

    disable() {
        this._dbus?.unexport();
        this._dbus = null;
    }

    GetFocused() {
        const win = global.display.get_focus_window();
        if (!win)
            return ['', ''];
        const appId = win.get_gtk_application_id() || win.get_sandboxed_app_id() || '';
        return [win.get_wm_class() || '', appId];
    }
}
