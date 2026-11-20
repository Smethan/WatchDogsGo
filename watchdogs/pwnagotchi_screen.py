"""Pwnagotchi status, controls, and recent local control-socket activity."""

from __future__ import annotations

import time

import pyxel


_FACES = {
    "( ⚆_⚆)": "( o_o)",
    "(☉_☉ )": "(o_o )",
    "( ◕‿◕)": "( o^o)",
    "(◕‿◕ )": "(o^o )",
    "(⇀‿‿↼)": "(-_- z)",
    "(≖‿‿≖)": "(-_- z)",
    "(◕‿‿◕)": "(o^o)",
    "(°▃▃°)": "(O_O)",
    "(⌐■_■)": "(B_B)",
    "(•‿‿•)": "(^_^)",
    "(ᵔ◡◡ᵔ)": "(^o^)",
    "(☼‿‿☼)": "(*_*)",
    "(≖__≖)": "(-_-)",
    "(✜‿‿✜)": "(*_*)",
    "(ب__ب)": "(;_;)",
    "(╥☁╥ )": "(;_;)",
    "(♥‿‿♥)": "(<3)",
    "(☓‿‿☓)": "(x_x)",
}


def face_ascii(face: object) -> str:
    """Keep the expressive face recognizable in WDG's ASCII-only bitmap font."""
    value = str(face or "").strip()
    if value in _FACES:
        return _FACES[value]
    if value and value.isascii() and len(value) <= 16:
        return value
    if not value:
        return "(-_-)"
    translated = value.translate(
        str.maketrans(
            {
                "◕": "o",
                "☉": "O",
                "⚆": "O",
                "•": "o",
                "‿": "_",
                "◡": "u",
                "ᵔ": "^",
                "≖": "-",
                "⇀": "-",
                "↼": "-",
                "☼": "*",
                "✜": "*",
                "╥": ";",
                "☁": "_",
                "♥": "*",
                "☓": "x",
                "■": "B",
                "⌐": "-",
                "°": "O",
                "▃": "_",
            }
        )
    )
    return "".join(c if 32 <= ord(c) <= 126 else "?" for c in translated)[:16]


def _text(value: object, limit: int = 80) -> str:
    raw = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    return "".join(c if 32 <= ord(c) <= 126 else "?" for c in raw)[:limit]


def network_rows(snapshot: object) -> list[dict]:
    """Return the bounded eligible AP set, or passive C5 identifiers."""
    if not isinstance(snapshot, dict):
        return []
    radio = snapshot.get("radio") or {}
    if not isinstance(radio, dict):
        return []
    aps = radio.get("aps")
    if isinstance(aps, list):
        return [ap for ap in aps[:64] if isinstance(ap, dict)]
    identifiers = radio.get("ap_bssids")
    if isinstance(identifiers, list):
        return [{"bssid": item} for item in identifiers[:64] if isinstance(item, str)]
    return []


def draw_map_card(px, state: dict) -> None:
    """Draw a small companion panel left of WDG's top-right radar."""
    if not (
        state.get("connected") or state.get("backend") or state.get("service_active")
    ):
        return
    snapshot = state.get("snapshot") or {}
    ui = snapshot.get("ui") or {}
    capture = snapshot.get("capture") or {}
    x, y, width, height = 445, 22, 138, 69
    online = bool(state.get("connected"))
    px.rect(x, y, width, height, 0)
    px.rectb(x, y, width, height, 11 if online else 5)
    px.text(x + 5, y + 4, "PWNAGOTCHI", 3 if online else 13)
    px.text(
        x + 7,
        y + 17,
        face_ascii(ui.get("face") if online else "(-_-)")[:15],
        11 if online else 13,
    )
    if online:
        aps = ui.get("aps", "--")
        shakes = capture.get("handshakes_run", ui.get("shakes", "--"))
        px.text(x + 5, y + 32, f"AP:{_text(aps, 5)}  HS:{_text(shakes, 5)}", 7)
        px.text(
            x + 5,
            y + 44,
            f"CH:{_text(ui.get('channel', '--'), 4)}  {_text(snapshot.get('mode', ''), 10)}",
            13,
        )
        px.text(x + 5, y + 56, _text(ui.get("status") or "Scanning", 25), 3)
    else:
        px.text(x + 5, y + 33, "OFFLINE", 9)
        px.text(x + 5, y + 49, "ADDONS > Pwnagotchi", 13)


class PwnagotchiScreen:
    TABS = ("CONTROL", "ACTIVITY", "NETWORKS")

    def __init__(self, app, bridge):
        self.app = app
        self.bridge = bridge
        self.open = False
        self.tab = 0
        self.selection = 0
        self.adapter_selection = 0
        self.note = ""
        self._state = {}
        self._state_at = 0.0

    def state(self, *, refresh: bool = False) -> dict:
        now = time.monotonic()
        if refresh or now - self._state_at >= 0.25:
            self._state = self.bridge.state()
            self._state_at = now
        return self._state

    def show(self) -> None:
        self.open = True
        self.tab = 0
        self.selection = 0
        state = self.state(refresh=True)
        current = state.get("interface")
        self.adapter_selection = next(
            (
                i
                for i, row in enumerate(state.get("adapters") or [])
                if row.get("name") == current
            ),
            0,
        )
        self.bridge.mark_read()

    def _submit(self, action: str, value=None) -> None:
        if action in ("start", "activate", "adapter"):
            drain = getattr(self.app, "_dragon_drain", None)
            if drain is not None and drain.running:
                self.note = "Stop Dragon Drain before changing Pwnagotchi's radio."
                return
        if not self.bridge.submit(action, value):
            self.note = (
                "A Pwnagotchi change is already running, or this action is unavailable."
            )
            return
        self.note = "Applying Pwnagotchi change..."
        self._state_at = 0.0

    def update(self) -> None:
        px = pyxel
        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.open = False
            return
        if px.btnp(px.KEY_LEFT):
            self.tab = (self.tab - 1) % len(self.TABS)
            self.selection = 0
        elif px.btnp(px.KEY_RIGHT):
            self.tab = (self.tab + 1) % len(self.TABS)
            self.selection = 0

        state = self.state()
        if self.tab == 0:
            adapters = state.get("adapters") or []
            if px.btnp(px.KEY_UP):
                self.adapter_selection = max(0, self.adapter_selection - 1)
            if px.btnp(px.KEY_DOWN):
                self.adapter_selection = min(
                    max(0, len(adapters) - 1), self.adapter_selection + 1
                )
            if px.btnp(px.KEY_A):
                self._submit("stop" if (state.get("service_active") or state.get("connected")) else "start")
            if px.btnp(px.KEY_B):
                self._submit(
                    "deactivate" if state.get("service_enabled") else "activate"
                )
            if px.btnp(px.KEY_D):
                controls = (state.get("snapshot") or {}).get("controls") or {}
                if "deauth" not in (controls.get("supported") or []):
                    self.note = "This backend does not offer deauth control."
                else:
                    self._submit("deauth", not bool(controls.get("deauth")))
            if px.btnp(px.KEY_RETURN) and adapters:
                selected = adapters[self.adapter_selection].get("name")
                if state.get("backend") != "bettercap":
                    self.note = "Adapter selection needs the Bettercap backend."
                elif selected == state.get("interface"):
                    self.note = f"{selected} is already selected."
                else:
                    self._submit("adapter", selected)
        else:
            rows = (
                state.get("events") or []
                if self.tab == 1
                else network_rows(state.get("snapshot"))
            )
            if px.btnp(px.KEY_UP):
                self.selection = max(0, self.selection - 1)
            if px.btnp(px.KEY_DOWN):
                self.selection = min(max(0, len(rows) - 1), self.selection + 1)
        self.bridge.mark_read()

    def draw(self) -> None:
        px = pyxel
        state = self.state()
        snapshot = state.get("snapshot") or {}
        ui = snapshot.get("ui") or {}
        capture = snapshot.get("capture") or {}
        epoch = snapshot.get("epoch") or {}
        ai = snapshot.get("ai") or {}
        online = bool(state.get("connected"))

        px.cls(0)
        px.rect(0, 0, 640, 18, 3)
        px.text(8, 5, "PWNAGOTCHI / LOCAL COMPANION", 0)
        px.text(520, 5, "ONLINE" if online else "OFFLINE", 0)
        px.rectb(8, 26, 116, 90, 11 if online else 5)
        px.text(
            18,
            56,
            face_ascii(ui.get("face") if online else "(-_-)")[:16],
            11 if online else 13,
            getattr(self.app, "_big_font", None),
        )
        px.text(140, 28, f"BACKEND  {_text(state.get('backend') or '--', 18)}", 13)
        px.text(140, 42, f"MODE     {_text(snapshot.get('mode') or '--', 18)}", 7)
        px.text(140, 56, f"ADAPTER  {_text(state.get('interface') or '--', 18)}", 7)
        px.text(
            140,
            70,
            f"CH:{_text(ui.get('channel', '--'), 4)}  AP:{_text(ui.get('aps', '--'), 5)}  "
            f"HS:{_text(capture.get('handshakes_run', ui.get('shakes', '--')), 5)}",
            11,
        )
        px.text(
            140,
            84,
            f"AI:{'ON' if ai.get('enabled') else 'OFF'}  EPOCH:{_text(epoch.get('number', '--'), 8)}",
            13,
        )
        px.text(140, 101, _text(ui.get("status") or "No live Pwnagotchi status", 90), 3)

        for index, label in enumerate(self.TABS):
            x = 8 + index * 116
            active = index == self.tab
            px.rect(x, 128, 112, 15, 3 if active else 1)
            px.text(x + 5, 131, label, 0 if active else 13)
        if self.tab == 0:
            self._draw_control(px, state)
        elif self.tab == 1:
            self._draw_activity(px, state)
        else:
            self._draw_networks(px, state)

        px.line(8, 320, 631, 320, 5)
        result = state.get("last_result") or state.get("error") or self.note
        px.text(8, 326, _text(result, 120), 9 if state.get("error") else 13)
        px.text(8, 344, "[LEFT/RIGHT] Tab  [ESC/TAB] Map", 3)

    def _draw_control(self, px, state: dict) -> None:
        snapshot = state.get("snapshot") or {}
        controls = snapshot.get("controls") or {}
        running = bool(state.get("service_active") or state.get("connected"))
        px.text(
            12,
            155,
            f"[A] Active now: {'ON' if running else 'OFF'}",
            11 if running else 9,
        )
        px.text(
            12,
            175,
            f"[B] Start at boot: {'YES' if state.get('service_enabled') else 'NO'}",
            7,
        )
        supported = "deauth" in (controls.get("supported") or [])
        deauth = "ON" if controls.get("deauth") else "OFF"
        px.text(
            12,
            195,
            f"[D] Deauth: {deauth if supported else 'N/A'}",
            9 if supported and controls.get("deauth") else 13,
        )
        px.text(
            12,
            215,
            f"Association: {'ON' if controls.get('associate') else 'OFF' if supported else 'N/A'}",
            13,
        )
        px.text(12, 246, "[ENTER] Select adapter", 3)
        px.text(12, 262, "Active adapter changes restart", 13)
        px.text(12, 275, "Bettercap and Pwnagotchi.", 13)
        px.text(12, 288, "Connected Wi-Fi may drop.", 9)
        px.text(12, 299, "[UP/DOWN] Adapter", 3)
        px.line(310, 151, 310, 308, 5)
        px.text(323, 155, "Wi-Fi adapters", 3)
        adapters = state.get("adapters") or []
        if not adapters:
            px.text(323, 177, "No adapters found. Check iw dev.", 9)
            return
        self.adapter_selection = min(self.adapter_selection, len(adapters) - 1)
        offset = max(0, min(self.adapter_selection - 4, len(adapters) - 8))
        for index, row in enumerate(adapters[offset : offset + 8], offset):
            y = 176 + (index - offset) * 15
            chosen = index == self.adapter_selection
            if chosen:
                px.rect(319, y - 2, 307, 13, 1)
            name = _text(row.get("name"), 16)
            mode = _text(row.get("mode") or "?", 12)
            mark = "*" if row.get("name") == state.get("interface") else " "
            px.text(324, y, f"{mark} {name:<16} {mode}", 11 if chosen else 7)

    def _draw_activity(self, px, state: dict) -> None:
        events = state.get("events") or []
        px.text(12, 152, "Recent decisions, training, captures and status", 13)
        if not events:
            px.text(16, 180, "Waiting for Pwnagotchi activity...", 13)
            return
        self.selection = min(self.selection, len(events) - 1)
        offset = max(0, min(self.selection - 9, len(events) - 12))
        for index, row in enumerate(events[offset : offset + 12], offset):
            y = 169 + (index - offset) * 11
            selected = index == self.selection
            if selected:
                px.rect(8, y - 1, 624, 10, 1)
            stamp = row.get("ts")
            try:
                when = time.strftime("%H:%M:%S", time.localtime(float(stamp)))
            except (TypeError, ValueError, OverflowError):
                when = "--:--:--"
            px.text(13, y, when, 13)
            px.text(65, y, _text(row.get("kind") or "event", 12).upper(), 3)
            px.text(135, y, _text(row.get("text"), 95), 7)
        detail = events[self.selection].get("detail")
        if detail:
            px.text(12, 305, _text(detail, 120), 9)

    def _draw_networks(self, px, state: dict) -> None:
        snapshot = state.get("snapshot") or {}
        radio = snapshot.get("radio") or {}
        rows = network_rows(snapshot)
        px.text(12, 152, "Current eligible APs seen by Pwnagotchi", 13)
        px.text(16, 169, "SSID / IDENTIFIER", 3)
        px.text(270, 169, "BSSID", 3)
        px.text(375, 169, "CH", 3)
        px.text(415, 169, "RSSI", 3)
        px.text(470, 169, "CLIENTS", 3)
        if not rows:
            px.text(16, 190, "No eligible APs in the current snapshot.", 13)
            return
        self.selection = min(self.selection, len(rows) - 1)
        offset = max(0, min(self.selection - 8, len(rows) - 10))
        for index, row in enumerate(rows[offset : offset + 10], offset):
            y = 187 + (index - offset) * 11
            if index == self.selection:
                px.rect(8, y - 1, 624, 10, 1)
            px.text(16, y, _text(row.get("ssid") or "<hidden>", 46), 7)
            px.text(270, y, _text(row.get("bssid") or "--", 17), 13)
            px.text(375, y, _text(row.get("channel", "--"), 4), 7)
            px.text(415, y, _text(row.get("rssi", "--"), 6), 7)
            px.text(470, y, _text(row.get("client_count", 0), 8), 7)
        if radio.get("aps_truncated"):
            px.text(
                12, 306, "Some APs were omitted by Pwnagotchi's bounded snapshot.", 9
            )
