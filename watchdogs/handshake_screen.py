"""Background-friendly progress screen for both existing HS Capture modes."""
import time
from .handshake_capture import COMMANDS, capture_storage
from .passive_screen import PassiveScreen


class HandshakeScreen(PassiveScreen):
    def __init__(self, app, owner):
        super().__init__(app, owner)
        self.storage = "sd"
        self.picker = False
        self.picker_selection = 0
        self.editing_filter = False
        self.picker_note = ""

    def show(self, storage):
        if storage != self.storage:
            self.selection = 0
        self.storage = storage
        self.picker = False
        self.open = True

    def update(self):
        import pyxel as px
        if self.picker:
            self.update_picker()
            return
        run = self.owner.capture.runs[self.storage]
        pending = capture_storage(self.app._pending_cmd) == self.storage
        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.open = False
            return
        if px.btnp(px.KEY_N):
            self.picker = True
            self.editing_filter = False
            choice = self.owner.targets.choices[self.storage]
            self.owner.targets.draft = set(choice[1]) if choice and choice[0] == self.owner.targets.token else set()
            return
        if px.btnp(px.KEY_A) and not run.active and not self.app._pending_cmd:
            self.owner.targets.choices[self.storage] = None
        if px.btnp(px.KEY_UP):
            self.selection = max(0, self.selection-1)
        if px.btnp(px.KEY_DOWN):
            self.selection = min(max(0, len(run.rows)-1), self.selection+1)
        if px.btnp(px.KEY_S) and (run.active or pending):
            self.app._send("stop")
        elif px.btnp(px.KEY_RETURN) and not run.active and not self.app._pending_cmd:
            # If the other capture variant is running, use the regular stop/start
            # transition rather than treating ENTER as a shared-state toggle.
            try:
                command = self.owner.targets.command(self.storage, self.owner.scan.capture_targets_supported, self.app._whitelist.is_blocked)
            except ValueError as exc:
                self.app.msg(str(exc), 9)
                return
            self.app._execute_item(command, "handshake_start", "HS Capture" + (" no SD" if self.storage == "serial" else ""), [])

    def draw(self):
        import pyxel as px
        if self.picker:
            self.draw_picker()
            return
        run = self.owner.capture.runs[self.storage]
        state = run.state.upper()
        if capture_storage(self.app._pending_cmd) == self.storage:
            state = "WAITING FOR PREVIOUS OPERATION TO STOP"
        px.cls(0)
        px.rect(0,0,640,18,9)
        destination = "ESP32 SD CARD" if self.storage == "sd" else "SERIAL TO uCONSOLE / NO SD"
        px.text(8,5,"HS CAPTURE / ACTIVE / " + destination,0)
        px.text(8,25,"Capture: " + state,11 if run.state == "running" else 9)
        pmkid = str(run.pmkids) if run.session else "N/A"
        px.text(8,40,f"EAPOL:{run.eapol}  PMKID:{pmkid}  Frames:{run.frames}  Drops:{run.drops}  Gaps:{run.gaps}  Incomplete:{run.lost}",7)
        scope = run.scope if run.active else self.owner.targets.label(self.storage)
        px.text(8,55,"Targets: " + scope + " | Packet counts; matching pairs are not checked.",13)
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
        px.text(8,331,"[ENTER] Start  [N] Networks  [A] All nearby  [S] Stop  [ESC/TAB] Map",3)
        px.text(8,345,"Leaving this screen keeps capture running. Active capture uses deauth.",13)


    def scan_networks(self):
        if self.app._pending_cmd or getattr(self.app, '_ota_reserved', False):
            self.picker_note = 'Wait for the current transition to finish.'
            return
        if self.owner.scan.capture_targets_supported is not True:
            self.owner.scan.probe()
            self.picker_note = 'Network selection needs firmware 1.7.9+. Checking support; retry shortly.'
            return
        # Stop first, then establish the new scan so stop cannot cancel its token.
        self.app._send('stop')
        command = self.owner.targets.prepare_scan()
        self.app._pending_cmd = command
        self.app._pending_state = 'hs_target_scan'
        self.app._pending_cmd_name = 'HS network scan'
        self.app._pending_deadline = time.monotonic() + 10
        self.picker_selection = 0
        self.picker_note = 'Scanning stops the previous ESP operation. Results are a snapshot.'

    def update_picker(self):
        import pyxel as px
        target = self.owner.targets
        if self.editing_filter:
            if px.btnp(px.KEY_RETURN) or px.btnp(px.KEY_ESCAPE):
                self.editing_filter = False
            elif px.btnp(px.KEY_BACKSPACE):
                target.query = target.query[:-1]
            else:
                char = self.app._get_char_input()
                if char and len(target.query) < 32:
                    target.query += char
            self.picker_selection = 0
            return
        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.picker = False
            return
        if px.btnp(px.KEY_R):
            self.scan_networks()
            return
        if px.btnp(px.KEY_F) or px.btnp(px.KEY_SLASH):
            self.editing_filter = True
            return
        if px.btnp(px.KEY_G):
            levels = [None, -100, -90, -80, -70, -60, -50, -40]
            target.min_rssi = levels[(levels.index(target.min_rssi)+1) % len(levels)]
            self.picker_selection = 0
        if px.btnp(px.KEY_O):
            target.sort_name = not target.sort_name
            self.picker_selection = 0
        if px.btnp(px.KEY_C):
            target.draft.clear(); target.query = ''; target.min_rssi = None
        if px.btnp(px.KEY_A):
            if not self.owner.capture.runs[self.storage].active and not self.app._pending_cmd:
                target.choices[self.storage] = None
                self.picker = False
            else:
                self.picker_note = 'Stop capture before changing its scope.'
            return
        rows = target.visible()
        if px.btnp(px.KEY_UP):
            self.picker_selection = max(0, self.picker_selection-1)
        if px.btnp(px.KEY_DOWN):
            self.picker_selection = min(max(0, len(rows)-1), self.picker_selection+1)
        self.picker_selection = min(self.picker_selection, max(0,len(rows)-1))
        try:
            if px.btnp(px.KEY_SPACE) and rows:
                target.toggle(rows[self.picker_selection]['bssid'], self.app._whitelist.is_blocked)
                self.picker_note = ''
            if px.btnp(px.KEY_RETURN):
                if self.owner.capture.runs[self.storage].active or self.app._pending_cmd:
                    raise ValueError('Stop capture before changing its scope.')
                target.apply(self.storage, self.app._whitelist.is_blocked)
                self.picker = False
        except ValueError as exc:
            self.picker_note = str(exc)

    def draw_picker(self):
        import pyxel as px
        target = self.owner.targets
        rows = target.visible()
        px.cls(0); px.rect(0,0,640,18,9)
        px.text(8,5,'HS CAPTURE / SELECT NETWORKS / ' + ('NO SD' if self.storage=='serial' else 'ESP32 SD'),0)
        px.text(8,25,f'Scan: {target.state.upper()}   Visible: {len(rows)}/{len(target.rows)}   Selected: {len(target.draft)}/16',7)
        threshold = 'any' if target.min_rssi is None else f'>= {target.min_rssi} dBm'
        cursor = '_' if self.editing_filter else ''
        px.text(8,40,'Name: ' + (target.query+cursor or '(any)') + '   RSSI: ' + threshold,11 if self.editing_filter else 7)
        px.text(8,55,'Sort: ' + ('name' if target.sort_name else 'strongest first') + ' | Selections stay checked when filtered out.',13)
        px.line(8,69,631,69,5)
        for x,label in ((8,'SEL'),(34,'NETWORK'),(220,'BSSID'),(344,'CH'),(387,'RSSI'),(441,'AUTH / STATUS')):
            px.text(x,75,label,3)
        self.picker_selection = min(self.picker_selection, max(0,len(rows)-1))
        offset = max(0,self.picker_selection-11)
        auth_names = {0:'Open',1:'WEP',2:'WPA',3:'WPA2',4:'WPA/WPA2',5:'Enterprise',6:'WPA3',7:'WPA2/WPA3',9:'OWE'}
        for i,row in enumerate(rows[offset:offset+12]):
            y = 90+i*14
            if i+offset == self.picker_selection:
                px.rect(5,y-2,630,13,1)
            mac = row['bssid']
            blocked = self.app._whitelist.is_blocked(mac)
            status = 'Whitelisted' if blocked else auth_names.get(row['auth'], 'Auth '+str(row['auth']))
            if row['auth'] in (0,1): status += ' (no WPA HS)'
            color = 13 if blocked or row['auth'] in (0,1) else 7
            for x,text in ((8,'[x]' if mac in target.draft else '[ ]'),(34,row['name'][:34]),(220,mac),(344,str(row['channel'])),(387,str(row['rssi'])),(441,status)):
                px.text(x,y,text,color)
        if not rows:
            px.text(14,105,'Press R to scan nearby networks.' if not target.rows else 'No networks match the current filters.',13)
        px.text(8,265,'[F /] Name filter   [G] Minimum RSSI   [O] Sort   [C] Clear filters/selection',3)
        px.text(8,280,'[R] Scan again (clears checks; stops current ESP operation)',3)
        note = target.error or self.picker_note
        px.text(8,301,note[:120],9)
        px.line(8,319,631,319,5)
        px.text(8,327,'[SPACE] Toggle network  [ENTER] Use selected  [A] All nearby  [ESC] Back',3)
        px.text(8,343,'Selections apply when capture starts. Both capture modes use deauth.',13)
