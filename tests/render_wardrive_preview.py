import os
os.environ['SDL_AUDIODRIVER']='dummy'
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from dataclasses import asdict
import tempfile,time
import pyxel as px
from watchdogs.app import WatchDogsGame,MapProjection,WifiNetwork,BleDevice
from watchdogs.app_state import AppState
from watchdogs.gps_manager import GpsFix
from watchdogs.wardrive_ui import WardriveUI
px.init(640,360,title='Offline wardrive UI check',display_scale=1)
a=WatchDogsGame.__new__(WatchDogsGame)
a._app_dir=tempfile.mkdtemp(prefix='wdg-visual-');a.loot=None;a.gps=NS(available=True,fix=GpsFix(valid=True,latitude=40,longitude=-90,received_at=time.monotonic()))
a.serial=None;a._term_add=Mock();a.msg=Mock();a._whitelist=NS(is_blocked=lambda _:False)
a._clear_scan_state();a.player_lat=40;a.player_lon=-90;a.gps_fix=True;a.wifi_networks=[];a.ble_devices=[]
a.wardrive=WardriveUI(a);a.wardrive.tick();a.proj=MapProjection();a.proj.center_lat=40;a.proj.center_lon=-90;a.proj.zoom=13
fix=asdict(a.gps.fix)
for kind,mac,name,lat,lon in [('wifi','B4:1E:52:00:00:01','Flock-123',40,-90),('ble','00:25:DF:00:00:01','',40.0002,-89.9997)]:
 d=dict(kind=kind,mac=mac,rssi=-60,name=name,ssid_hex=name.encode().hex(),addr_type=0,data_hex='',fix=dict(fix,latitude=lat,longitude=lon),observed_at=time.time())
 a.wardrive.detect(d)
a.wardrive.tick();a.wardrive.settings['trail']=True
for i in range(30):
 a.wardrive.trail.points.append(dict(lat=40-0.00003*(30-i),lon=-90-0.00004*(30-i),segment=1,time=0))
a.wardrive.scan.state='running';a.wardrive.scan.last_seen={'wifi':time.monotonic(),'ble':time.monotonic()}
a._mc_bubbles=[('Simulated MeshCore message',9999)]
# Deliberately synthetic street background; no real tiles/GPS or radio use.
def base():
 px.cls(0)
 for x in range(0,640,45):px.rect(x,16,3,218,5)
 for y in range(25,235,34):px.rect(0,y,640,2,13)
 px.text(8,4,'ALL WARDRIVE   WiFi + BLE   RUNNING     SYNTHETIC PREVIEW',7)
 a.wardrive.draw_trail();a.wardrive.draw_markers()
 a.wardrive.draw_radar(610,40,20,20000)
 a._draw_mc_toast();a.wardrive.draw_overlay()
 px.text(8,243,'[WDG] WiFi and BLE streaming / GPS on uConsole',3)
 px.text(8,258,'[DETECT] Flock signature match / Axon vendor candidate',7)
 px.text(8,340,'[6] All Wardrive   [O] Wardrive Settings   [S] Stop',13)
out=Path(os.environ.get('WDG_PREVIEW_DIR','/tmp/wdg-visuals'));out.mkdir(exist_ok=True)
base();px.screen.save(str(out/'flock-preview.png'),1)
a.wardrive.alerts.popleft();base();px.screen.save(str(out/'axon-preview.png'),1)
a.wardrive.alerts.clear();a.wardrive.settings_open=True;a.wardrive.details=True;base();px.screen.save(str(out/'settings-preview.png'),1)
print('Saved three simulated 640x360 UI previews')
px.quit()
