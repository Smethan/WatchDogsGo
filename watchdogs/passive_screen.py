"""Passive capture status screen; visibility never owns the running scan."""
import time
from .wardrive_protocol import display_bytes


class PassiveScreen:
    def __init__(self, app, owner):
        self.app, self.owner = app, owner
        self.open = False
        self.selection = 0

    def update(self):
        import pyxel as px
        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.open = False
            return
        count = len(self.owner.passive.rows)
        if px.btnp(px.KEY_UP):
            self.selection = max(0, self.selection-1)
        if px.btnp(px.KEY_DOWN):
            self.selection = min(max(0,count-1), self.selection+1)
        scan = self.owner.scan
        if px.btnp(px.KEY_S):
            if (scan.mode == "hs_sniff" and scan.active) or (self.app._pending_cmd and self.app._pending_state == "hs_sniff"):
                self.app._send("stop")
        if px.btnp(px.KEY_RETURN):
            if not (scan.mode == "hs_sniff" and scan.active) and not self.app._pending_cmd:
                self.app._execute_item("start_hs_sniff_serial", "hs_sniff", "HS Sniff", [])

    def draw(self):
        import pyxel as px
        scan, capture = self.owner.scan, self.owner.passive
        state = scan.state.upper() if scan.mode == "hs_sniff" else "IDLE"
        if self.app._pending_cmd and self.app._pending_state == "hs_sniff":
            state = "WAITING FOR PREVIOUS SCAN TO STOP"
        px.cls(0)
        px.rect(0,0,640,18,3)
        px.text(8,5,"HS SNIFF / PASSIVE / SERIAL TO uCONSOLE",0)
        px.text(8,25,"Capture: " + state,11 if scan.mode == "hs_sniff" and scan.state == "running" else 9)
        px.text(8,40,f"EAPOL:{capture.eapol}  PMKID:{capture.pmkids}  Frames:{capture.frames}  Drops:{scan.stats.get('drops',0) if scan.mode=='hs_sniff' else 0}  Incomplete:{capture.lost}",7)
        px.text(8,55,"Packet counts only; matching handshake pairs are not checked.",13)
        self.draw_rows(capture)
        if capture.path:
            px.text(8,293,"File: handshakes/"+capture.path.name,13)
        messages = getattr(self.app,"msgs",[])
        if messages:
            px.text(8,309,messages[-1][0][:120],9)
        elif scan.error or scan.probe_error:
            px.text(8,309,(scan.error or scan.probe_error)[:120],9)
        px.line(8,323,631,323,5)
        px.text(8,331,"[ENTER] Start   [S] Stop   [UP/DOWN] Details   [ESC/TAB] Map",3)
        px.text(8,345,"Leaving this screen keeps capture running. No ESP32 SD card needed.",13)

    def draw_rows(self, capture, *, active=False):
        import pyxel as px
        columns = ((8,"NETWORK"),(143,"AP"),(205,"CLIENT"),(269,"CH"),(301,"RSSI"),
                   (345,"PMKID"),(393,"M1"),(433,"M2"),(473,"M3"),(513,"M4"),(563,"AGE"))
        px.line(8,68,631,68,5)
        for x, label in columns:
            px.text(x,74,label,3)
        rows = list(capture.rows.values())
        self.selection = min(self.selection, max(0,len(rows)-1))
        offset = max(0,self.selection-11)
        for i, row in enumerate(rows[offset:offset+12]):
            y=90+i*14
            selected = i+offset == self.selection
            if selected:
                px.rect(5,y-2,630,13,1)
            ssid = capture.ssids.get(row["bssid"])
            name = display_bytes(ssid) if ssid else "<unknown SSID>"
            values = ((8,name[:24]),(143,row["bssid"][-8:]),(205,row["station"][-8:] or "--"),
                      (269,str(row["channel"]) if row["channel"] is not None else "--"),(301,str(row["rssi"]) if row["rssi"] is not None else "--"),
                      (563,str(max(0,int(time.time()-row["last"])))+"s"))
            for x, label in values:
                px.text(x,y,label[:13] if x==563 else label,7)
            for x, count in zip((345,393,433,473,513),(row["pmkids"],*row["messages"])):
                label = "N/A" if count is None else (str(count) if count < 1000 else "999+")
                px.text(x,y,label if count is None or count else "-",11 if count else 13)
        if not rows:
            px.text(14,108,"Waiting for captured EAPOL / PMKID frames." if active else "Waiting for naturally occurring EAPOL / PMKID traffic.",13)
        if rows:
            row=rows[self.selection]
            px.text(8,266,f"AP {row['bssid']}   Client {row['station'] or 'not in this frame'}",7)
            px.text(8,279,f"Row {self.selection+1}/{len(rows)}  " + ("M1-M4 counts reflect forwarded capture frames." if active else "M1-M4 counts include retransmissions."),13)
