import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from ks_includes.screen_panel import ScreenPanel


# (label, macro, description) — macros are defined in printer.cfg via
# gcode_shell_command wrappers around SkewCamera's CLI.
ACTIONS = [
    (
        _("Capture Calibration Images"),
        "SKEW_CAPTURE_IMAGES",
        _("One-time per camera. Moves the toolhead to capture views for camera intrinsics."),
    ),
    (
        _("Calibrate Skew"),
        "CALIBRATE_SKEW",
        _("Captures the ChArUco board across the bed, computes skew, applies SET_SKEW and saves."),
    ),
]


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        title = title or _("Skew Calibration")
        super().__init__(screen, title)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)

        intro = Gtk.Label(
            label=_("Tape the printed ChArUco board flat on the bed, then run the steps below. "
                    "Watch the Console for progress."),
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD,
            justify=Gtk.Justification.CENTER,
        )
        box.pack_start(intro, False, False, 0)

        for label, macro, desc in ACTIONS:
            btn = self._gtk.Button(label=label, style="color3")
            btn.set_vexpand(False)
            btn.connect("clicked", self._run_macro, macro)
            box.pack_start(btn, False, False, 0)

            hint = Gtk.Label(
                label=desc,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD,
                justify=Gtk.Justification.CENTER,
            )
            hint.get_style_context().add_class("print-info")
            box.pack_start(hint, False, False, 0)

        scroll = self._gtk.ScrolledWindow()
        scroll.add(box)
        self.content.add(scroll)
        self.content.show_all()

    def _run_macro(self, widget, macro):
        logging.info(f"Skew panel: running {macro}")
        self._screen.show_popup_message(f"{macro}", 1)
        self._screen._send_action(widget, "printer.gcode.script", {"script": macro})
