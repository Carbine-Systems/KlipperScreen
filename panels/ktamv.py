import logging
from contextlib import suppress

import gi
import mpv

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango

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

# Retry budget for the realize race on first activate (idle_add ticks)
_MAX_REALIZE_RETRIES = 30


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        title = title or _("kTAMV")
        super().__init__(screen, title)
        self.mpv = None
        self.active_tool = None
        self.tool_buttons = {}
        self._realize_retries = 0

        # Camera DrawingArea + Stack so we can swap a "no nozzle cam" placeholder
        # inline (instead of nagging with a popup every visit).
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_double_buffered(False)
        self.drawing_area.set_hexpand(True)
        self.drawing_area.set_vexpand(True)

        self.placeholder = Gtk.Label(
            label=_("No camera named 'nozzle' is configured.\n"
                    "Add a [webcam nozzle] block in moonraker.conf."),
            justify=Gtk.Justification.CENTER,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD,
            hexpand=True,
            vexpand=True,
        )

        self.cam_stack = Gtk.Stack()
        self.cam_stack.add_named(self.drawing_area, "video")
        self.cam_stack.add_named(self.placeholder, "placeholder")
        self.cam_stack.set_hexpand(True)
        self.cam_stack.set_vexpand(True)

        cam_frame = Gtk.Frame()
        cam_frame.add(self.cam_stack)
        cam_frame.set_hexpand(True)
        cam_frame.set_vexpand(True)

        # Controls column — tool selector + action buttons
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        controls.set_hexpand(False)
        controls.set_vexpand(True)

        tool_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        for idx, label in ((0, "T0"), (1, "T1")):
            btn = self._gtk.Button(label=label, style=f"color{idx + 1}")
            btn.connect("clicked", self._select_tool, idx)
            btn.set_hexpand(True)
            btn.set_vexpand(False)
            self.tool_buttons[idx] = btn
            tool_row.pack_start(btn, True, True, 0)
        controls.pack_start(tool_row, False, False, 0)

        for i, (label, gcode) in enumerate(ACTION_BUTTONS):
            btn = self._gtk.Button(label=label, style=f"color{(i % 4) + 1}")
            btn.set_hexpand(True)
            btn.set_vexpand(True)
            btn.connect("clicked", self._send_macro, gcode)
            controls.pack_start(btn, True, True, 0)

        # Controls width: 14em ≈ 25% of content on 1280-wide screens, but
        # scales with font_size for other displays / DPI.
        controls.set_size_request(int(self._gtk.font_size * 14), -1)

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
        self._realize_retries = 0
        GLib.idle_add(self._start_stream)
        # Sync tool highlight from current Klipper state
        self._sync_tool_from_printer()

    def deactivate(self):
        self._stop_stream()

    def process_update(self, action, data):
        # Keep T0/T1 highlight live when tool changes happen from elsewhere
        if action != "notify_status_update" or not data:
            return
        toolhead = data.get("toolhead") if isinstance(data, dict) else None
        if isinstance(toolhead, dict) and "extruder" in toolhead:
            self._sync_tool_from_printer()

    def _sync_tool_from_printer(self):
        if not self._printer:
            return
        current = self._printer.get_stat("toolhead", "extruder")
        if current == "extruder":
            self._highlight_tool(0)
        elif current == "extruder1":
            self._highlight_tool(1)

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
            # Inline placeholder — no popup nag, no retry needed
            self.cam_stack.set_visible_child_name("placeholder")
            return False

        self.cam_stack.set_visible_child_name("video")

        # Retry the realize race a few times before giving up. The DrawingArea
        # may not have a window yet on first activate.
        window = self.drawing_area.get_window()
        if window is None:
            self._realize_retries += 1
            if self._realize_retries >= _MAX_REALIZE_RETRIES:
                logging.warning("kTAMV: DrawingArea never realized; stream not started")
                return False
            # Try again on the next idle tick
            GLib.timeout_add(50, self._start_stream)
            return False
        xid = window.get_xid()

        url = cam["stream_url"]
        if url.startswith("/"):
            endpoint = self._screen.apiclient.endpoint.split(":")
            url = f"{endpoint[0]}:{endpoint[1]}{url}"
        if "/webrtc" in url:
            url = url.replace("/webrtc", "/stream")

        # Apply per-cam flip/rotation — upward nozzle cams are typically mirrored
        vf_list = []
        if cam.get("flip_horizontal"):
            vf_list.append("hflip")
        if cam.get("flip_vertical"):
            vf_list.append("vflip")
        if cam.get("rotation"):
            vf_list.append(f"rotate:{cam['rotation'] * 3.14159 / 180}")

        if self.mpv:
            with suppress(Exception):
                self.mpv.terminate()
        self.mpv = mpv.MPV(
            wid=str(xid),
            log_handler=self._mpv_log,
            vo="x11,xv,wlshm,gpu",
            keep_open="yes",
        )
        if vf_list:
            self.mpv.vf = ",".join(vf_list)
        with suppress(Exception):
            self.mpv.profile = "low-latency"
        self.mpv.untimed = True
        self.mpv.audio = "no"

        logging.debug(f"kTAMV cam URL: {url} vf={vf_list}")
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
        if self.active_tool == tool_idx:
            return
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
