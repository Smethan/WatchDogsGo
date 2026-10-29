"""Background-friendly progress screen for both existing HS Capture modes."""
import time
from collections import OrderedDict
from types import SimpleNamespace
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
        self.device_tab = 0

    def show(self, storage):
        if storage != self.storage:
            self.selection = 0
            self.device_tab = 0
        self.storage = storage
        self.picker = False
        self.open = True

    def wifi_whitelist(self):
        """Return only AP BSSIDs; BLE whitelist entries are unrelated."""
        return tuple(
            entry.mac for entry in getattr(self.app._whitelist, "entries", ())
            if str(getattr(entry, "type", "")).lower() == "wifi"
        )

    def _managed_capture_view(self):
        """Return the latest managed handshake group matching this storage mode."""
        pool = getattr(self.app, "c5_pool", None)
        if pool is None:
            return None
        candidates = []
        for group in pool.groups.values():
            if group.operation != "handshake":
                continue
            devices = [did for did, request in group.requests.items()
                       if group.roles.get(did) == "foreground"
                       and request.get("op") == "handshake"
                       and request.get("options", {}).get("storage") == self.storage]
            if devices:
                candidates.append((group, devices))
        if not candidates:
            return None
        group, devices = candidates[-1]
        tabs = []
        if len(devices) >= 2:
            tabs.append({"id": "all", "label": "ALL", "device_id": None})
        for did in devices:
            entry = pool.registry.get(did)
            alias = sorted(entry.aliases)[0] if entry and entry.aliases else ""
            board = entry.board if entry else "ESP32"
            prefix = "COMBINED / " if len(devices) == 1 else ""
            tabs.append({"id": did, "label": prefix + ((alias + " / ") if alias else "") + board,
                         "device_id": did})
        self.device_tab %= len(tabs)
        selected = tabs[self.device_tab]

        def read_device(did):
            request = group.requests[did]
            key = (did, request.get("boot_id"), group.tokens.get(did))
            capture = pool.captures.get(key)
            if capture:
                return capture.rows, capture.ssids
            rows = OrderedDict(((row["bssid"], row["station"]), row)
                               for row in group.capture_rows.get(did, ()))
            return rows, group.capture_ssids.get(did, {})

        visible_devices = devices if selected["device_id"] is None else [selected["device_id"]]
        rows, ssids, sources = OrderedDict(), {}, {}
        for did in visible_devices:
            device_rows, device_ssids = read_device(did)
            for target in group.requests[did].get("targets", ()):
                if target.get("ssid_hex"):
                    ssids.setdefault(target["bssid"], target["ssid_hex"])
            ssids.update(device_ssids)
            for identity, source_row in device_rows.items():
                row = dict(source_row, messages=list(source_row["messages"]))
                if identity not in rows:
                    rows[identity], sources[identity] = row, 1
                else:
                    target = rows[identity]
                    target["pmkids"] += row["pmkids"]
                    target["messages"] = [a+b for a,b in zip(target["messages"], row["messages"])]
                    sources[identity] += 1
                    if row.get("last", 0) >= target.get("last", 0):
                        target.update(channel=row.get("channel"), rssi=row.get("rssi"),
                                      last=row.get("last"))
        target_devices = devices if selected["device_id"] is None else [selected["device_id"]]
        targets, excluded = [], set()
        for did in target_devices:
            request = group.requests[did]
            targets.extend(request.get("targets", ()))
            excluded.update(request.get("exclude", ()))
        unique_targets = list({target["bssid"]: target for target in targets}.values())
        options = group.requests[target_devices[0]].get("options", {})
        scope = ("ALL NEARBY" if options.get("scope") == "all" else
                 f"SELECTED {len(unique_targets)} NETWORKS (" +
                 ",".join(target.get("bssid", "")[-8:] for target in unique_targets[:3]) +
                 (" ...)" if len(unique_targets) > 3 else ")"))
        if excluded:
            scope += f" / EXCLUDES {len(excluded)} WL"
        if selected["device_id"] is None:
            scope += " / summed counts; duplicate AP/client sightings may be included"
        did = selected["device_id"]
        entry = pool.registry.get(did) if did else None
        session = entry.session if entry else None
        # AP/client row channels come from captured frames; the current radio
        # hop is separate telemetry for each foreground C5.
        radio = []
        for index, radio_id in enumerate(target_devices, 1):
            radio_entry = pool.registry.get(radio_id)
            radio_session = radio_entry.session if radio_entry else None
            channels = group.requests[radio_id].get("channels", ())
            current = (radio_session.applied_channel
                       if radio_session and radio_session.job_token == group.tokens.get(radio_id)
                       else None)
            if did is None:
                radio.append(f"D{index} ch:{current if current is not None else '--'}/{len(channels)}")
            else:
                radio.append(f"ch:{current if current is not None else '--'} "
                             f"plan:{len(channels)} [{self._format_channel_plan(channels)}]")
        if did in group.disconnected or (entry and not entry.connected):
            device_state = "DISCONNECTED"
        elif did in group.stopped or group.state == "stopped":
            device_state = "STOPPED"
        elif group.state == "error" or (session and session.state == "error"):
            device_state = "STOPPED / ERROR"
        elif session and session.job_token == group.tokens.get(did):
            device_state = session.state.upper()
        elif group.state in ("starting", "stopping"):
            device_state = group.state.upper()
        else:
            device_state = "RUNNING"
        helpers = [helper for helper, role in group.roles.items() if role == "passive-helper"]
        return {"group": group, "tabs": tabs, "tab_index": self.device_tab,
                "selected": selected, "rows": rows, "ssids": ssids, "sources": sources,
                "scope": scope, "device_state": device_state, "entry": entry,
                "helpers": helpers, "state": group.state.upper(),
                "radio_summary": " | ".join(radio)}

    @staticmethod
    def _format_channel_plan(channels):
        """Show plan bounds without implying every channel in between."""
        values = sorted(set(channels))
        if not values:
            return "none"
        return str(values[0]) if len(values) == 1 else f"bounds {values[0]}..{values[-1]}"

    def update(self):
        import pyxel as px
        if self.picker:
            self.update_picker()
            return
        run = self.owner.capture.runs[self.storage]
        pool = getattr(self.app,'c5_pool',None)
        managed = pool is not None and pool.available
        managed_group = next((g for g in pool.groups.values()
            if g.operation == 'handshake' and g.state in ('starting','running','stopping')),None) if managed else None
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
        view = self._managed_capture_view()
        row_count = len(view["rows"]) if view else len(run.rows)
        if px.btnp(px.KEY_UP):
            self.selection = min(max(0, row_count-1), max(0, self.selection-1))
        if px.btnp(px.KEY_DOWN):
            self.selection = min(max(0, row_count-1), self.selection+1)
        if view and len(view["tabs"]) > 1:
            if px.btnp(px.KEY_LEFT):
                self.device_tab = (self.device_tab-1) % len(view["tabs"])
                self.selection = 0
            if px.btnp(px.KEY_RIGHT):
                self.device_tab = (self.device_tab+1) % len(view["tabs"])
                self.selection = 0
        if px.btnp(px.KEY_S) and (run.active or pending or managed_group):
            if managed:
                pool.stop_operation('handshake')
            else:
                self.app._send("stop")
        elif px.btnp(px.KEY_RETURN) and not run.active and not managed_group and not self.app._pending_cmd:
            # If the other capture variant is running, use the regular stop/start
            # transition rather than treating ENTER as a shared-state toggle.
            try:
                command = self.owner.targets.command(
                    self.storage,
                    self.owner.scan.capture_targets_supported,
                    self.app._whitelist.is_blocked,
                    self.owner.scan.capture_exclusions_supported,
                    self.wifi_whitelist(),
                )
            except ValueError as exc:
                self.app.msg(str(exc), 9)
                return
            self.app._execute_item(command, "handshake_start", "HS Capture" + (" no SD" if self.storage == "serial" else ""), [])

    def draw(self):
        import pyxel as px
        if self.picker:
            self.draw_picker()
            return
        view = self._managed_capture_view()
        if view:
            self._draw_managed_capture(px, view)
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
        if (not run.active and self.owner.targets.choices[self.storage] is None
                and self.wifi_whitelist()):
            scope += f" / EXCLUDES {len(self.wifi_whitelist())} WL"
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


    def _draw_managed_capture(self, px, view):
        group, selected = view["group"], view["selected"]
        did = selected["device_id"]
        label = "COMBINED" if did is None else selected["label"]
        px.cls(0)
        px.rect(0,0,640,18,9)
        destination = "ESP32 SD CARD" if self.storage == "sd" else "SERIAL TO uCONSOLE / NO SD"
        px.text(8,5,"HS CAPTURE / ACTIVE / "+destination,0)
        px.text(8,22,"Capture: "+view["state"]+"   Device: "+label[:28],11)
        if did:
            px.text(430,22,view["device_state"],11 if view["device_state"] == "RUNNING" else 9)
        if len(view["tabs"]) > 1:
            tab_labels = "  ".join(("["+tab["label"]+"]") if i == view["tab_index"] else tab["label"]
                                  for i,tab in enumerate(view["tabs"]))
            px.text(8,36,"ESP32: "+tab_labels[:90]+"  [LEFT/RIGHT]",7)
        scope = "ALL NEARBY / SUMMED" if did is None else view["scope"][:44]
        radio_limit = 72 if did is None else 55
        px.text(8,50,"Targets: "+scope+"  Radio hop: "+view["radio_summary"][:radio_limit],13)
        metric_devices = ([did] if did else [tab["device_id"] for tab in view["tabs"]
                                                   if tab["device_id"]])
        metrics = [group.capture_metrics.get(source, {}) for source in metric_devices]
        total = {key: sum(metric.get(key, 0) for metric in metrics)
                 for key in ("frames", "eapol", "pmkids", "validated", "lost")}
        px.text(8,63,f"Frames:{total['frames']} EAPOL:{total['eapol']} PMKID:{total['pmkids']} "
                f"VALID:{total['validated']} LOST:{total['lost']}",7)
        self.draw_rows(SimpleNamespace(rows=view["rows"],ssids=view["ssids"]),active=True)
        destination_line = ("Files: ESP32 SD /lab/handshakes/" if self.storage == "sd"
                            else "Files: uConsole loot/handshakes after capture stops.")
        px.text(8,289,destination_line,13)
        if view["helpers"]:
            pool = getattr(self.app,"c5_pool",None)
            names = []
            for helper_id in view["helpers"]:
                helper = pool.registry.get(helper_id) if pool else None
                names.append((helper.board if helper else "ESP32")+" passive helper")
            px.text(8,301,("Helpers: "+", ".join(names))[:120],3)
        note = group.error or "Packet sightings; matching EAPOL pairs are not checked."
        px.text(8,312,note[:120],9)
        px.line(8,323,631,323,5)
        px.text(8,331,"[LEFT/RIGHT] ESP32  [UP/DOWN] Rows  [N] Networks  [S] Stop  [ESC/TAB] Map",3)
        px.text(8,345,"Leaving this screen keeps capture running. Active capture uses deauth.",13)

    def scan_networks(self):
        if self.app._pending_cmd or getattr(self.app, '_ota_reserved', False):
            self.picker_note = 'Wait for the current transition to finish.'
            return
        if self.owner.scan.capture_targets_supported is not True:
            self.owner.scan.probe()
            self.picker_note = 'Network selection needs firmware 1.7.9+. Checking support; retry shortly.'
            return
        pool = getattr(self.app,'c5_pool',None)
        if pool is not None and pool.available:
            command = self.owner.targets.prepare_scan()
            pool.start_command(command,'hs_target_scan','HS network scan')
            self.picker_selection = 0
            self.picker_note = 'Scanning both supported Wi-Fi bands; selections use BSSID/channel descriptors.'
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
        if target.intel_available:
            age = '<1s' if target.intel_age_ms < 1000 else f'{target.intel_age_ms // 1000}s'
            intel = f'Pkt Sniffer: {target.intel_packets} packets / {target.intel_probes} probe requesters / age {age}'
        else:
            intel = 'Pkt Sniffer: no completed sample; client/packet/probe columns are unavailable'
        px.text(8,55,intel[:120],13)
        px.line(8,69,631,69,5)
        for x,label in ((8,'SEL'),(34,'NETWORK'),(154,'BSSID'),(278,'CH'),
                        (310,'RSSI'),(354,'CLI'),(386,'PKTS'),(440,'PRB'),
                        (470,'AUTH / STATUS')):
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
            clients = str(row['clients']) if target.intel_available else '-'
            packets = str(row['packets']) if target.intel_available else '-'
            probes = str(row['probes']) if target.intel_available else '-'
            for x,text in (
                    (8,'[x]' if mac in target.draft else '[ ]'),
                    (34,row['name'][:22]),(154,mac),(278,str(row['channel'])),
                    (310,str(row['rssi'])),(354,clients),
                    (386,packets),(440,probes),
                    (470,status[:32])):
                px.text(x,y,text,color)
        if not rows:
            px.text(14,105,'Press R to scan nearby networks.' if not target.rows else 'No networks match the current filters.',13)
        px.text(8,265,'[F /] Name filter   [G] Minimum RSSI   [O] Sort   [C] Clear filters/selection',3)
        px.text(8,280,'[R] Scan again (clears checks; stops current ESP operation)',3)
        note = target.error or self.picker_note
        px.text(8,301,note[:120],9)
        px.line(8,319,631,319,5)
        px.text(8,327,'[SPACE] Toggle network  [ENTER] Use selected  [A] All nearby  [ESC] Back',3)
        px.text(8,343,'PRB = unique requester/SSID pairs. Both capture modes use deauth.',13)
