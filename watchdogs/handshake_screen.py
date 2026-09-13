"""Background-friendly progress screen for both existing HS Capture modes."""
import time
from .handshake_capture import COMMANDS
from .passive_screen import PassiveScreen


class HandshakeScreen(PassiveScreen):
    def __init__(self, app, owner):
        super().__init__(app, owner)
        self.storage = "sd"

    def show(self, storage):
        if storage != self.storage:
            self.selection = 0
        self.storage = storage
        self.open = True

    def update(self):
        import pyxel as px
        run = self.owner.capture.runs[self.storage]
        command = COMMANDS[self.storage]
        pending = self.app._pending_cmd == command
        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.open = False
            return
        if px.btnp(px.KEY_UP):
            self.selection = max(0, self.selection-1)
        if px.btnp(px.KEY_DOWN):
            self.selection = min(max(0, len(run.rows)-1), self.selection+1)
        if px.btnp(px.KEY_S) and (run.active or pending):
            self.app._send("stop")
        elif px.btnp(px.KEY_RETURN) and not run.active and not self.app._pending_cmd:
            # If the other capture variant is running, use the regular stop/start
            # transition rather than treating ENTER as a shared-state toggle.
            self.app._execute_item(command, "handshake_start", "HS Capture" + (" no SD" if self.storage == "serial" else ""), [])

    def draw(self):
        import pyxel as px
        run = self.owner.capture.runs[self.storage]
        state = run.state.upper()
        if self.app._pending_cmd == COMMANDS[self.storage]:
            state = "WAITING FOR PREVIOUS OPERATION TO STOP"
        px.cls(0)
        px.rect(0,0,640,18,9)
        destination = "ESP32 SD CARD" if self.storage == "sd" else "SERIAL TO uCONSOLE / NO SD"
        px.text(8,5,"HS CAPTURE / ACTIVE / " + destination,0)
        px.text(8,25,"Capture: " + state,11 if run.state == "running" else 9)
        pmkid = str(run.pmkids) if run.session else "N/A"
        px.text(8,40,f"EAPOL:{run.eapol}  PMKID:{pmkid}  Frames:{run.frames}  Drops:{run.drops}  Gaps:{run.gaps}  Incomplete:{run.lost}",7)
        px.text(8,55,"Packet counts only; matching handshake pairs are not checked.",13)
        self.draw_rows(run, active=True)
        destination = "Files: ESP32 SD /lab/handshakes/ (requires SD card on ESP32)." if self.storage == "sd" else "Files: uConsole loot/handshakes after capture stops; wait for transfer."
        px.text(8,293,destination,13)
        note = run.note
        messages = getattr(self.app, "msgs", [])
        if messages:
            note = messages[-1][0]
        if run.session and run.state == "running" and time.monotonic()-run.last_progress > 7:
            note = "Live progress is late; capture has not been stopped. Check the console."
        if run.session and run.state == "running" and run.drops and not run.frames:
            from .updates import release_version
            try:
                old = release_version(getattr(self.app, '_fw_version', '')) < (1, 7, 6)
            except ValueError:
                old = True
            note = ("ESP progress packets dropped: firmware 1.7.6 fixes the USB buffer limit." if old
                    else "Progress copies are dropping; check USB. Capture files may still be recording.")
        if run.invalid:
            note += f" Bad records:{run.invalid}"
        px.text(8,309,note[:120],9)
        px.line(8,323,631,323,5)
        px.text(8,331,"[ENTER] Start   [S] Stop   [UP/DOWN] Details   [ESC/TAB] Map",3)
        px.text(8,345,"Leaving this screen keeps capture running. Active capture uses deauth.",13)
