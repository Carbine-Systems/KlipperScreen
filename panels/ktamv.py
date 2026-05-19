from panels.gcode_macros import Panel as MacrosPanel


class Panel(MacrosPanel):
    INCLUDE_KEYWORDS = ("KTAMV",)
    EXCLUDE_KEYWORDS = ()

    def __init__(self, screen, title):
        super().__init__(screen, title or _("kTAMV"))
