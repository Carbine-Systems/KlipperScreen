import logging
from contextlib import suppress

import gi
import mpv

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ks_includes.screen_panel import ScreenPanel


# Action buttons: (label, kTAMV macro to fire)
ACTION_BUTTONS = [
    (_("Find Nozzle"), "SIMPLE_NOZZLE_POSITION_KTAMV"),
    (_("Set Origin"), "SET_ORIGIN_KTAMV"),
    (_("Center"), "MOVE_TO_ORIGIN_KTAMV"),
    (_("Calibrate"), "CALIB_CAMERA_KTAMV"),
    (_("Get Offset"), "GET_OFFSET_KTAMV"),
    (_("Show Last"), "PRINT_OFFSET_KTAMV"),
]


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        title = title or _("kTAMV")
        super().__init__(screen, title)
        self.mpv = None
        self.active_tool = None
        self.tool_buttons = {}

        # Camera fills the left/top, fixed-width controls anchor right/bottom.
        # Gtk.Grid distributes space by hexpand/vexpand more predictably than
        # a Box with set_size_request hints.
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_double_buffered(False)
        self.drawing_area.set_hexpand(True)
        self.drawing_area.set_vexpand(True)

        cam_frame = Gtk.Frame()
        cam_frame.add(self.drawing_area)
        cam_frame.set_hexpand(True)
        cam_frame.set_vexpand(True)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        controls.set_hexpand(False)
        controls.set_vexpand(True)

        # Tool selector — fires T0 / T1 so subsequent action buttons act on it
        tool_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        for idx, label in ((0, "T0"), (1, "T1")):
            btn = self._gtk.Button(label=label, style=f"color{idx + 1}")
            btn.connect("clicked", self._select_tool, idx)
            btn.set_hexpand(True)
            btn.set_vexpand(False)
            self.tool_buttons[idx] = btn
            tool_row.pack_start(btn, True, True, 0)
        controls.pack_start(tool_row, False, False, 0)

        # Action buttons
        for i, (label, gcode) in enumerate(ACTION_BUTTONS):
            btn = self._gtk.Button(label=label, style=f"color{(i % 4) + 1}")
            btn.set_hexpand(True)
            btn.set_vexpand(True)
            btn.connect("clicked", self._send_macro, gcode)
            controls.pack_start(btn, True, True, 0)

        # Lock controls width to ~25% of the content area
        content_width = self._screen.width - self._gtk.action_bar_width
        controls_width = int(content_width * 0.25)
        controls.set_size_request(controls_width, -1)

        if self._screen.vertical_mode:
            grid = Gtk.Grid(row_spacing=5)
            grid.attach(cam_frame, 0, 0, 1, 1)
            grid.attach(controls, 0, 1, 1, 1)
        else:
            grid = Gtk.Grid(column_spacing=5)
            grid.attach(cam_frame, 0, 0, 1, 1)
            grid.attach(controls, 1, 0, 1, 1)

        self.content.add(grid)
        self.content.show_all()

    def activate(self):
        # Defer mpv attach so the DrawingArea is realized with a valid XID
        GLib.idle_add(self._start_stream)
        # Sync tool highlight with whatever Klipper says is active
        current = self._printer.get_stat("toolhead", "extruder") if self._printer else None
        if current == "extruder":
            self._highlight_tool(0)
        elif current == "extruder1":
            self._highlight_tool(1)

    def deactivate(self):
        self._stop_stream()

    def _find_nozzle_cam(self):
        """Find a camera whose name contains 'nozzle' (case-insensitive)."""
        if not self._printer or not getattr(self._printer, "cameras", None):
            return None
        for cam in self._printer.cameras:
            if not cam.get("enabled"):
                continue
            if "nozzle" in cam.get("name", "").lower():
                return cam
        return None

    def _start_stream(self):
        cam = self._find_nozzle_cam()
        if cam is None:
            self._screen.show_popup_message(
                _("No camera named 'nozzle' is configured in Moonraker. "
                  "Add a [webcam nozzle] block pointing at port 8081.")
            )
            return False

        url = cam["stream_url"]
        if url.startswith("/"):
            endpoint = self._screen.apiclient.endpoint.split(":")
            url = f"{endpoint[0]}:{endpoint[1]}{url}"
        if "/webrtc" in url:
            url = url.replace("/webrtc", "/stream")

        self.drawing_area.realize()
        window = self.drawing_area.get_window()
        if window is None:
            logging.warning("kTAMV panel: drawing area not realized yet")
            return False
        xid = window.get_xid()

        if self.mpv:
            self.mpv.terminate()
        self.mpv = mpv.MPV(
            wid=str(xid),
            log_handler=self._mpv_log,
            vo="x11,xv,wlshm,gpu",
            keep_open="yes",
        )
        with suppress(Exception):
            self.mpv.profile = "low-latency"
        self.mpv.untimed = True
        self.mpv.audio = "no"

        logging.debug(f"kTAMV cam URL: {url}")
        self.mpv.play(url)
        return False

    def _stop_stream(self):
        if self.mpv is not None:
            with suppress(Exception):
                self.mpv.terminate()
            self.mpv = None

    def _select_tool(self, widget, tool_idx):
        self._send_gcode(f"T{tool_idx}")
        self._highlight_tool(tool_idx)

    def _highlight_tool(self, tool_idx):
        self.active_tool = tool_idx
        for idx, btn in self.tool_buttons.items():
            ctx = btn.get_style_context()
            if idx == tool_idx:
                ctx.add_class("button_active")
            else:
                ctx.remove_class("button_active")

    def _send_macro(self, widget, gcode):
        self._send_gcode(gcode)
        self._screen.show_popup_message(gcode, 1)

    def _send_gcode(self, gcode):
        self._screen._send_action(None, "printer.gcode.script", {"script": gcode})

    def _mpv_log(self, loglevel, component, message):
        if (
            "unable to decode" in message
            or "No Xvideo support found" in message
            or "GBM" in message
            or "open TTY for VT control" in message
        ):
            return
        if loglevel == "error":
            logging.warning(f"mpv: {message}")
        logging.debug(f"[{loglevel}] {component}: {message}")
