"""Synthetic-only protocol, lifecycle, geotagging, detection and route tests."""
import csv
from dataclasses import asdict
import json
from pathlib import Path
import threading
import time
from queue import Queue
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from watchdogs.wardrive_protocol import parse_record
from watchdogs.serial_manager import SerialLineBuffer
from watchdogs.scan_controller import ScanController
from watchdogs.notable_detector import NotableDetector, ad_fields
from watchdogs.wardrive_trail import FixHistory, WardriveTrail
from watchdogs.gps_manager import GpsFix, GpsManager
from watchdogs.loot_manager import LootManager
from watchdogs.app_state import AppState, Network
from watchdogs.app import MAP_NODE_LIMIT, RECENT_NODE_FX_LIMIT, WatchDogsGame
from watchdogs.wardrive_ui import WardriveUI

def record(kind="wifi", **kwargs):
    d = dict(v=1,kind=kind,session="test",seq=2,mac="B4:1E:52:00:00:01",rssi=-60,
             channel=6,ssid_hex="466c6f636b2d313233",capture_ms=100,age_ms=0,
             addr_type=0,event=0,data_hex="",truncated=False,
             wifi_count=1,ble_count=1,drops=0)
    d.update(kwargs)
    return d

def ad(t, data):
    return bytes([len(data)+1,t])+data

def wire(d):
    return "WDG:"+json.dumps(d,separators=(",", ":"))

def batch_control(kind="heartbeat", **changes):
    d = dict(v=2, kind=kind, session="test", seq=1, batch=1,
             state="running", uptime_ms=1000, wifi_count=1, ble_count=1, drops=0)
    if kind == "batch_start": d["window_ms"] = 10000
    if kind in ("batch_results", "batch_done"):
        d.update(batch_wifi=1, batch_ble=1)
    d.update(changes)
    return d

def batch_record(kind="wifi", **changes):
    d = record(kind, v=2, seq=2, batch=1, age_ms=10000)
    d.update(changes)
    return d

@pytest.mark.parametrize("chunk", [1,2,7,1024])
def test_framing(chunk):
    text = (wire(record())+"\r\n"+wire(record("ble"))+"\n").encode()
    buf=SerialLineBuffer();lines=[]
    for i in range(0,len(text),chunk): lines += buf.feed(text[i:i+chunk])
    assert [parse_record(l)["kind"] for l in lines]==["wifi","ble"]

def test_oversize_recovery():
    b=SerialLineBuffer(32)
    assert b.feed(b"x"*100000)==[] and len(b._buf)==0
    assert b.feed(b"fragment\nhello\n")==["hello"]
    assert b.dropped_lines==1

@pytest.mark.parametrize("change", [dict(v=2),dict(v=True),dict(seq=-1),dict(age_ms=2001),dict(rssi="-50"),dict(mac="??"),dict(ssid_hex="aa " ),dict(ssid_hex="aa"*33),dict(session="bad\n"),dict(channel=0)])
def test_bad_records(change):
    assert parse_record(wire(record(**change))) is None

def test_v2_protocol_boundaries_and_batches():
    assert parse_record(wire(batch_control()))["kind"] == "heartbeat"
    assert parse_record(wire(batch_control("batch_start")))["window_ms"] == 10000
    assert parse_record(wire(batch_control("batch_results")))["batch_wifi"] == 1
    assert parse_record(wire(batch_record(age_ms=20000)))["batch"] == 1
    assert parse_record(wire(batch_record(age_ms=20001))) is None
    assert parse_record(wire(batch_control("batch_start", batch=0))) is None
    assert parse_record(wire(batch_control("status", state="unknown"))) is None

def test_v2_is_default_with_legacy_fallback_and_independent_liveness():
    now=[0];sent=[];c=ScanController(sent.append,lambda:now[0])
    c.handle(dict(kind="capabilities",wardrive_serial_v1=True,wardrive_wifi_serial_v1=True,
                  wardrive_batch_serial_v2=True,wardrive_wifi_batch_serial_v2=True,
                  hs_capture_targets_v1=True,hs_capture_exclusions_v1=True))
    assert c.capture_targets_supported and c.capture_exclusions_supported
    assert c.start() and sent[-1].startswith("start_wardrive_batch_serial ")
    token=c.session
    c.handle(batch_control("started",session=token,seq=1,batch=0))
    c.handle(batch_control("batch_start",session=token,seq=2))
    assert c.state=="running" and c.batch_phase=="scanning"
    now[0]=7;c.tick()
    assert c.state=="running" and "wardrive_status " in sent[-2]
    # A data record proves observations are flowing, but does not forge a
    # control heartbeat or hide the warning/probe path.
    now[0]=14;c.handle(batch_record(session=token,seq=3));c.tick()
    assert c.state=="running" and c.last_control==0 and c.last_data==14
    now[0]=16;c.tick();assert c.state=="running"
    c.handle(batch_control("heartbeat",session=token,seq=4,batch=2))
    assert c.last_control==16
    c.stop();assert c.accept_plain_stop() and c.state=="idle"

    legacy=ScanController(sent.append,lambda:0)
    legacy.handle(dict(kind="capabilities",wardrive_serial_v1=True))
    assert legacy.start() and legacy.legacy and sent[-1].startswith("start_wardrive_serial ")
    legacy.stop();assert not legacy.accept_plain_stop()

def test_v2_status_recovers_missed_start_and_stop():
    now=[0];sent=[];c=ScanController(sent.append,lambda:now[0])
    c.supported=True;c.batch_supported=True;c.start();token=c.session
    now[0]=4;c.tick();assert sent[-1]=="wardrive_status "+token
    c.handle(batch_control("status",session=token,seq=1,state="running",batch=1))
    assert c.state=="running"
    c.stop();c.handle(batch_control("status",session=token,seq=2,state="stopped",batch=1))
    assert c.state=="idle"

def test_malformed_json():
    for s in ('WDG:[]','WDG:null','WDG:{','WDG:'+ '['*1100):
        assert parse_record(s) is None

def test_scan_lifecycle():
    now=[0];sent=[];c=ScanController(sent.append,lambda:now[0]);c.supported=True
    assert c.start() and c.state=="starting"
    token=c.session
    assert not c.handle(record("started",session="old",seq=1))
    c.handle(record("started",session=token,seq=1));assert c.state=="running"
    assert c.handle(record("ble",session=token,seq=2))
    assert not c.handle(record("ble",session=token,seq=2))
    now[0]=5;c.tick();assert sent[-1]=="wardrive_keepalive "+token
    c.stop();assert not c.handle(record("wifi",session=token,seq=3))
    c.handle(record("stopped",session=token,seq=4));assert c.state=="idle"
    assert not c.handle(record("started",session=token,seq=5))

def test_timeouts_and_disconnect():
    now=[0];sent=[];c=ScanController(sent.append,lambda:now[0]);c.probe()
    now[0]=9;c.tick();assert c.supported is None and c.probe_error and not c.start()
    c.supported=True;c.start();now[0]=18;c.tick();assert c.state=="error" and sent[-1]=="stop"
    c.reset();assert c.session=="" and c.supported is None

@pytest.mark.parametrize("name,expected", [("Penguin-123",True),("Flock-abc",True),("pigvision",True),("FS Ext Battery",True),("myFlock-router",False),("Penguin",False),("1234567890",False)])
def test_names(name,expected):
    d=NotableDetector();hits=d.classify(record("ble",mac="C2:00:00:00:00:01",addr_type=1,data_hex=ad(9,name.encode()).hex()),0)
    assert bool(hits)==expected

def test_oem_is_not_camera_and_scan_response_upgrade():
    d=NotableDetector();e=record("ble",mac="C2:00:00:00:00:01",addr_type=1,data_hex=ad(255,b"\xc8\x09\x00").hex())
    assert d.classify(e,0)[0]["strength"]==0
    e.update(event=4,data_hex=ad(9,b"Penguin-123").hex())
    hits=d.classify(e,1);assert hits[0]["strength"]==2 and len(hits[0]["evidence"])==2
    d.clear();assert not d.cache

def test_random_addresses_and_axon_networks_excluded():
    d=NotableDetector()
    assert not d.classify(record("ble",mac="00:25:DF:00:00:01",addr_type=1),0)
    assert not d.classify(record("wifi",mac="00:58:28:00:00:01",ssid_hex=""),0)
    assert d.classify(record("ble",mac="00:25:DF:00:00:01"),0)[0]["label"]=="Possible Axon device"

def test_body_tag_requires_service_data():
    d=NotableDetector();base=record("ble",mac="C2:00:00:00:00:01",addr_type=1)
    base["data_hex"]=ad(255,b"\x81\xfcBWCDEVICE").hex()
    assert not d.classify(base,0)
    base["data_hex"]=ad(0x16,b"\x81\xfcBWCDEVICE").hex()
    assert d.classify(base,1)[0]["strength"]==3
    assert ad_fields(b"\xff\x09Flock-123")==[]

def test_probe_role_and_whitelist():
    d=NotableDetector();e=record("wifi_mgmt",subtype=4,ssid_present=True,ssid_hex="",receiver="ff:ff:ff:ff:ff:ff")
    assert d.classify(e,0)[0]["strength"]==2
    assert not d.classify(e,0,lambda _:True)
    e.update(mac="00:11:22:00:00:01",receiver="B4:1E:52:00:00:01")
    assert not d.classify(e,0)  # receiver does not identify transmitter

def test_fix_history_and_stale():
    h=FixHistory();f=GpsFix(latitude=0,longitude=0,valid=True,received_at=10)
    h.update(f,10);assert h.at(10)["latitude"]==0
    f.latitude=2;f.received_at=12;h.update(f,12)
    assert h.at(11)["latitude"]==0 and h.at(12)["latitude"]==2
    assert h.at(16) is None
    f.valid=False;f.received_at=13;h.update(f,13);assert h.at(13) is None

def test_fix_history_keeps_batch_window_and_uses_nearest_fix():
    h=FixHistory()
    for stamp in range(1,31):
        h.update(GpsFix(latitude=stamp,longitude=0,valid=True,received_at=stamp),stamp)
    assert h.at(20.4)["latitude"]==20
    assert h.at(9)["latitude"]==9
    h.update(GpsFix(latitude=32,longitude=0,valid=True,received_at=32),32)
    assert h.at(-2) is None and len(h.fixes)<=600

def test_frozen_nmea_does_not_refresh(monkeypatch):
    import watchdogs.gps_manager as g
    now=[10];monkeypatch.setattr(g.time,"monotonic",lambda:now[0])
    m=GpsManager();sentence="$GPGGA,120000,0000.000,N,00000.000,E,1,8,1.0,10,M,,M,,"
    m._parse(sentence);assert m.fix.received_at==10
    now[0]=20;m._parse(sentence);assert m.fix.received_at==10

@pytest.fixture
def loot(tmp_path):
    l=LootManager.__new__(LootManager);l._session=tmp_path;l._session_active=True;l._gps=None
    l.log_serial=Mock();l.save_bt_device=Mock()
    return l

def test_csv_escaping_dedup_and_unknown_gps(loot):
    n=Network(bssid="00:11:22:33:44:55",ssid='a,"b\nline',rssi="-60",channel="6",auth="OPEN")
    fix=asdict(GpsFix(valid=True,latitude=0,longitude=0))
    assert not loot.save_wardriving_network(n,observation_fix=None)
    assert loot.save_wardriving_network(n,observation_fix=fix,observed_at=10)
    assert not loot.save_wardriving_network(n,observation_fix=fix,observed_at=11)
    n.rssi="-50";assert loot.save_wardriving_network(n,observation_fix=fix,observed_at=12)
    with (loot._session/"wardriving.csv").open(newline="") as f:
        next(f);rows=list(csv.reader(f))
    assert len(rows)==2 and rows[1][1]=='a,"b\nline' and rows[1][-1]=="WIFI"


def test_meshtastic_loot_records_nodes_once_and_received_messages(tmp_path):
    loot = LootManager.__new__(LootManager)
    loot._session = tmp_path
    loot._session_active = True
    loot.update_session_loot = Mock()

    loot.save_meshtastic_node(
        "!12345678", "Road Node", 40.1, -90.2, -91, 4.5, 1,
        40.0, -90.0, "RAK4631")
    loot.save_meshtastic_node(
        "!12345678", "Road Node", 40.1, -90.2, -80, 5.0, 1,
        40.01, -90.01, "RAK4631")
    loot.save_meshtastic_message("0", "Road Node", "hello", -91)

    with (tmp_path / "meshtastic_nodes.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["node_id"] == "!12345678"
    assert rows[0]["observed_lat"] == "40.0"
    assert "[ch:0] Road Node: hello" in (
        tmp_path / "meshtastic_messages.log").read_text()

def test_async_scan_loot_keeps_disk_writes_off_observation_path(tmp_path):
    loot=LootManager.__new__(LootManager);loot._session=tmp_path
    loot._session_active=True;loot._gps=None
    loot._init_scan_loot_state(async_writes=True)
    # Keep the worker in its coalescing window until this test asks it to stop.
    loot.SCAN_LOOT_WRITE_DELAY=10;loot.SCAN_LOOT_WRITE_MAX_DELAY=10
    loot._scan_loot_thread=threading.Thread(target=loot._scan_loot_writer_loop)
    loot._scan_loot_thread.start()
    fix=asdict(GpsFix(valid=True,latitude=40,longitude=-90))
    for i in range(500):
        mac=f"02:00:{(i>>16)&255:02X}:{(i>>8)&255:02X}:{i&255:02X}:01"
        assert loot._save_wardrive_row(mac,f"ap-{i}","[ESS]",i%14+1,-70,
                                       "WIFI",fix,i+1)
        loot.save_bt_device(mac,-70,f"ble-{i}",False,False,
                            observation_fix=fix,observed_at=i+1)
    assert not (tmp_path/"wardriving.csv").exists()
    assert not (tmp_path/"bt_devices.csv").exists()

    # A stronger repeat updates the indexed WiGLE row; a weaker one is ignored.
    mac="02:00:00:00:00:01"
    assert loot._save_wardrive_row(mac,"strong","[ESS]",1,-40,"WIFI",fix,999)
    assert not loot._save_wardrive_row(mac,"weak","[ESS]",1,-80,"WIFI",fix,1000)
    loot._scan_loot_stop=True;loot._scan_loot_event.set()
    loot._scan_loot_thread.join(timeout=2)
    assert not loot._scan_loot_thread.is_alive()

    with (tmp_path/"wardriving.csv").open(newline="") as stream:
        next(stream);rows=list(csv.DictReader(stream))
    with (tmp_path/"bt_devices.csv").open(newline="") as stream:
        bt_rows=list(csv.DictReader(stream))
    assert len(rows)==len(bt_rows)==500
    winner=next(row for row in rows if row["MAC"]==mac)
    assert winner["SSID"]=="strong" and winner["RSSI"]=="-40"

@pytest.fixture
def game(tmp_path,loot,monkeypatch):
    import watchdogs.app as appmod
    monkeypatch.setattr(appmod,"pyxel",NS(frame_count=100))
    app=WatchDogsGame.__new__(WatchDogsGame)
    app._app_dir=str(tmp_path);app.loot=loot;app._pending_cmd=None
    app.gps=NS(available=True,fix=GpsFix(valid=True,latitude=40,longitude=-90,received_at=10))
    app.serial=NS(is_open=True,send_command=Mock());app._esp32=True
    app._term_add=Mock();app.msg=Mock();app.gain_xp=Mock();app._earn_badge=Mock()
    app._clear_scan_state();app._gps_wait=False;app._gps_wait_cmd="";app._whitelist=NS(is_blocked=lambda _:False)
    app.player_lat=40;app.player_lon=-90;app.gps_fix=True;app.ble_devices=[];app.wifi_networks=[]
    app._known_ble=set();app._known_wifi=set();app.state=AppState()
    app.loot_points=[];app._cluster_zoom=-1
    app.wardrive=WardriveUI(app)
    import watchdogs.wardrive_ui as ui
    monkeypatch.setattr(ui.time,"monotonic",lambda:10)
    app.wardrive.tick()
    return app

def test_integration_both_radios_precise_toggle_and_raw_gps(game):
    w=game.wardrive;w.scan.supported=True;w.scan.start();token=w.scan.session
    w.handle_line(wire(record("started",session=token,seq=1)))
    w.handle_line(wire(record("wifi",session=token,seq=2)))
    w.handle_line(wire(record("ble",session=token,seq=3,mac="00:25:DF:00:00:01")))
    assert len(game.wifi_networks)==len(game.ble_devices)==1
    item=next(iter(w.notables.values()));assert w.position(item)==(40,-90)
    w.settings["precise"]=False;assert w.position(item)==(game.wifi_networks[0].lat,game.wifi_networks[0].lon)
    assert item["fix"]["latitude"]==40
    before=game.gain_xp.call_count
    w.handle_line(wire(record("wifi",session=token,seq=4)))
    assert game.gain_xp.call_count==before and len(game.state.networks)==1
    assert len(w.alerts)==2
    w.settings["network_dots"] = False
    assert list(w.visible_notables())  # special Flock/Axon markers stay enabled
    with (game.loot._session/"wardriving.csv").open(newline="") as f:
        next(f);rows=list(csv.reader(f))[1:]
    assert {row[-1] for row in rows}=={"WIFI","BLE"}


def test_recent_notable_stops_suppressing_generic_dot_after_expiry():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = {
        "flock": True,
        "axon": False,
        "dot_flock_mode": "recent",
        "dot_fade_seconds": 30,
    }
    ui.notables = {
        "wifi:AA:flock": {
            "identity": "wifi:AA", "category": "flock",
            "strength": 1, "seen": 100,
        },
    }
    ui._notable_identities = frozenset()
    ui._notable_revision = 0

    ui._refresh_notable_identities(now=129.999)
    assert ui.live_notable_identities()[0] == frozenset(("wifi:AA",))
    visible_revision = ui.live_notable_identities()[1]

    ui._refresh_notable_identities(now=130)
    assert ui.live_notable_identities()[0] == frozenset()
    assert ui.live_notable_identities()[1] == visible_revision + 1


def test_restored_notable_is_historical_even_during_early_uptime():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = {
        "flock": True,
        "axon": False,
        "dot_flock_mode": "recent",
        "dot_fade_seconds": 30,
    }
    ui.notables = {
        "wifi:AA:flock": {
            "identity": "wifi:AA", "category": "flock",
            "strength": 1, "seen": 0,
        },
    }
    ui._notable_identities = frozenset()
    ui._notable_revision = 0

    ui._refresh_notable_identities(now=5)

    assert ui.live_notable_identities()[0] == frozenset()
    assert ui.is_notable("wifi", "AA") is False


def test_live_map_node_window_evicts_globally_oldest_without_losing_counts():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.wifi_networks = []
    game.ble_devices = []
    for index in range(MAP_NODE_LIMIT + 10):
        node = NS()
        game._remember_live_map_node(node, "wifi" if index % 2 else "ble")

    retained = sorted(
        game.wifi_networks + game.ble_devices,
        key=lambda node: node.map_sequence)
    assert len(retained) == MAP_NODE_LIMIT
    assert retained[0].map_sequence == 11
    assert retained[-1].map_sequence == MAP_NODE_LIMIT + 10
    assert (game._session_wifi_discovered + game._session_ble_discovered
            == MAP_NODE_LIMIT + 10)
    assert len(game._recent_wifi_fx) <= RECENT_NODE_FX_LIMIT
    assert len(game._recent_ble_fx) <= RECENT_NODE_FX_LIMIT


def test_network_dot_toggle_skips_only_generic_node_layers():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.wardrive = NS(show_network_dots=lambda: False)
    game._draw_loot_points = Mock()
    game._draw_live_nodes = Mock()
    game._draw_wifi = Mock()
    game._draw_ble = Mock()

    game._draw_wardrive_nodes()

    assert not game._draw_loot_points.called
    assert not game._draw_live_nodes.called
    assert not game._draw_wifi.called
    assert not game._draw_ble.called

    game.wardrive.show_network_dots = lambda: True
    game._draw_wardrive_nodes()
    assert all(method.called for method in (
        game._draw_loot_points, game._draw_live_nodes,
        game._draw_wifi, game._draw_ble))


def test_wifi_target_index_recovers_after_public_list_clear(game):
    first=Network(bssid="00:11:22:33:44:55",ssid="first",rssi="-70")
    game._ingest_wifi(first)
    updated=Network(bssid=first.bssid,ssid="updated",rssi="-50")
    game._ingest_wifi(updated)
    assert len(game.state.networks)==1
    assert game.state.networks[0].ssid=="updated"

    game.state.networks.clear()
    game._ingest_wifi(updated)
    assert game.state.networks==[updated]

def test_transition_cancels_old_timers_and_waits_for_final_stop(game):
    game.wifi_scanning=True;game._wifi_scan_done_time=1;game._bt_scan_done_time=1
    game._start_scan_cmd("scan_bt","bt_scanning","BT Wardrive")
    assert not game.wifi_scanning and not game.ble_scanning and game._wifi_scan_done_time==0
    assert not game.wardrive.handle_line("Stop command received")
    assert game._pending_cmd=="scan_bt"
    assert game.wardrive.handle_line("All operations stopped.")
    assert game.ble_scanning and not game.wifi_scanning and game._pending_cmd is None
    assert game.serial.send_command.call_args.args==("scan_bt",)
    game._send("stop");assert not game.ble_scanning and game._pending_cmd is None

def test_all_never_runs_legacy_timers(game):
    game.wardrive.scan.state="running";game.wifi_scanning=True;game.ble_scanning=True
    game._wifi_scan_done_time=game._bt_scan_done_time=1
    before=game.serial.send_command.call_count;game._update_wardriving_loop()
    assert game.serial.send_command.call_count==before

def test_route_segments_reload_and_no_discoveries(tmp_path):
    t=WardriveTrail();t.set_path(tmp_path/"trail.jsonl")
    f=asdict(GpsFix(valid=True,latitude=40,longitude=-90,received_at=1,hdop=1))
    t.sample(f,1,True)
    f.update(latitude=40.0001,received_at=2);t.sample(f,2,True)
    assert len(t.points)==2 and t.points[0]["segment"]==t.points[1]["segment"]
    t.sample(None,3,True)
    f.update(latitude=40.0002,received_at=4);t.sample(f,4,True)
    assert t.points[1]["segment"]!=t.points[2]["segment"]
    count=len(t.points);t.sample(f,5,False);assert len(t.points)==count
    with t.path.open("a") as out:out.write('{"incomplete":')
    restored=WardriveTrail();restored.set_path(t.path);assert len(restored.points)==3


def test_trail_density_counts_unique_radios_and_expires_old_ids():
    trail = WardriveTrail()
    trail.note_observation("wifi:AA", 1)
    trail.note_observation("wifi:AA", 5)
    trail.note_observation("ble:BB", 6)

    assert trail.recent_unique_count(6) == 2
    assert trail.recent_unique_count(15) == 2
    assert trail.recent_unique_count(15.001) == 1
    assert trail.recent_unique_count(16.001) == 0


def test_trail_density_is_saved_and_restored(tmp_path):
    trail = WardriveTrail()
    trail.set_path(tmp_path / "trail.jsonl")
    fix = asdict(GpsFix(
        valid=True, latitude=40, longitude=-90, received_at=1, hdop=1))
    trail.sample(fix, 1, True, density=27)

    restored = WardriveTrail()
    restored.set_path(trail.path)

    assert restored.points[0]["density"] == 27


def test_map_detail_and_parent_fallback(tmp_path):
    from watchdogs.tile_manager import GAME_TO_OSM,TileRenderer
    assert GAME_TO_OSM[13]==16 and GAME_TO_OSM[12]==15
    renderer=TileRenderer(tmp_path)
    renderer._get_tile_image=Mock(side_effect=lambda z,x,y: bytearray([7]*65536) if z==14 else None)
    px=NS(screen=NS(data_ptr=lambda:bytearray(640*360)),pset=Mock())
    proj=NS(geo_to_screen=Mock(side_effect=[(0,16),(256,234)]))
    assert renderer._draw_tile(px,proj,16,100,100,640,360,16,234)


def test_map_tiles_prepare_native_image_once(tmp_path):
    from watchdogs.tile_manager import TileRenderer

    made=[]
    class Image:
        def __init__(self,w,h):
            self.width=w;self.height=h;self.data=bytearray(w*h);made.append(self)
        def data_ptr(self): return self.data

    renderer=TileRenderer(tmp_path)
    renderer._get_tile_image=Mock(return_value=bytearray([7]*65536))
    px=NS(Image=Image,blt=Mock())
    proj=NS(geo_to_screen=Mock(side_effect=[(0,16),(64,48)]*2))
    assert renderer._draw_tile(px,proj,16,100,100,640,360,16,234)
    assert renderer._draw_tile(px,proj,16,100,100,640,360,16,234)
    assert len(made)==1
    assert px.blt.call_count==2


def test_map_missing_tile_and_resolution_are_cached(tmp_path):
    from watchdogs.tile_manager import TileRenderer

    renderer=TileRenderer(tmp_path)
    assert renderer._resolve_tile(16,100,100) is None
    missing=set(renderer._missing)
    assert missing
    assert renderer._resolve_tile(16,100,100) is None
    assert renderer._missing==missing


def test_map_manifest_reload_invalidates_render_caches(tmp_path):
    from watchdogs.tile_manager import TileRenderer

    renderer=TileRenderer(tmp_path)
    renderer._cache[(12,1,1)]=bytearray(65536)
    renderer._missing.add((13,2,2))
    renderer._resolved[(13,2,2)]=None
    renderer._display_cache[(13,2,2,64,64)]=(object(),4096)
    renderer._display_cache_bytes=4096
    renderer.reload_manifest()
    assert not renderer._cache and not renderer._missing
    assert not renderer._resolved and not renderer._display_cache
    assert renderer._display_cache_bytes==0


def test_map_manifest_reload_defers_native_image_release_to_draw_thread(tmp_path):
    from watchdogs.tile_manager import TileRenderer

    renderer=TileRenderer(tmp_path)
    owner=threading.get_ident()
    renderer._display_owner_thread=owner
    native_image=object()
    renderer._display_cache[(13,2,2,64,64)]=(native_image,4096)
    renderer._display_cache_bytes=4096
    errors=[]

    def reload_on_worker():
        try:
            renderer.reload_manifest()
        except Exception as exc:
            errors.append(exc)

    worker=threading.Thread(target=reload_on_worker)
    worker.start();worker.join()

    assert not errors
    assert renderer._display_reset_pending
    assert renderer._display_cache

    # Even an out-of-range zoom drains the native cache before returning.
    renderer._draw_locked(NS(zoom=0),640,360,16,234)
    assert not renderer._display_reset_pending
    assert not renderer._display_cache
    assert renderer._display_cache_bytes==0


def test_delayed_observation_keeps_original_fix(game, monkeypatch):
    import watchdogs.wardrive_ui as ui
    w=game.wardrive
    monkeypatch.setattr(ui.time,"monotonic",lambda:12)
    game.gps.fix.latitude=41;game.gps.fix.received_at=12
    w.fixes.update(game.gps.fix,12)
    w.observation(record(age_ms=2000))
    item=next(iter(w.notables.values()))
    assert item["observation_fix"]["latitude"]==40
    game.gps.available=False
    w.observation(record(mac="00:25:DF:00:00:02"))
    item=list(w.notables.values())[-1]
    assert item["observation_fix"] is None and w.position(item) is None


def test_zero_discoveries_trail_stationary_and_freeze(tmp_path):
    t=WardriveTrail();t.set_path(tmp_path/"route.jsonl")
    f=asdict(GpsFix(latitude=40,longitude=-90,valid=True,received_at=1,hdop=1))
    for now in range(1,13):
        f["received_at"]=now;t.sample(f,now,True)
    assert len(t.points)==2
    assert t.points[0]["segment"]==t.points[1]["segment"]
    t.sample(f,15,True);assert len(t.points)==2
    f.update(received_at=16,latitude=40.0001);t.sample(f,16,True)
    assert t.points[-1]["segment"]!=t.points[-2]["segment"]


def test_route_append_after_crash(tmp_path):
    path=tmp_path/"route.jsonl";path.write_text('{"partial":')
    t=WardriveTrail();t.set_path(path)
    f=asdict(GpsFix(latitude=1,longitude=1,valid=True,received_at=1))
    t.sample(f,1,True)
    reloaded=WardriveTrail();reloaded.set_path(path)
    assert len(reloaded.points)==1


@pytest.mark.parametrize("payload", [ad(255,b"\x4d\x03\x00"),ad(3,b"\x81\xfc"),ad(0x16,b"\x81\xfc\x00")])
def test_registry_axon_ids_on_random_addresses(payload):
    hits=NotableDetector().classify(record("ble",mac="C2:00:00:00:00:01",addr_type=1,data_hex=payload.hex()),0)
    assert hits[0]["label"]=="Possible Axon device" and hits[0]["strength"]==1


def test_ble_cache_is_bounded_and_does_not_cross_addresses():
    d=NotableDetector()
    for i in range(600):
        mac=f"C2:00:00:00:{i//256:02X}:{i%256:02X}"
        d.classify(record("ble",mac=mac,addr_type=1,data_hex=ad(9,b"Flock-123").hex()),i)
    assert len(d.cache)==512
    assert not d.classify(record("ble",mac="C2:11:22:33:44:55",addr_type=1),601)


def test_real_pty_serial_stream():
    # A local pseudo-terminal; no physical USB or radio device is opened.
    import os,pty,select
    from watchdogs.serial_manager import SerialManager
    master,slave=pty.openpty();s=SerialManager(os.ttyname(slave))
    try:
        s.setup()
        payload=(wire(record())+"\n"+wire(record("ble"))+"\n").encode()
        os.write(master,payload[:25]);select.select([s.fd],[],[],1)
        assert s.read_available()==[]
        os.write(master,payload[25:]);select.select([s.fd],[],[],1)
        assert [parse_record(l)["kind"] for l in s.read_available()]==["wifi","ble"]
    finally:
        s.close();os.close(master);os.close(slave)


def test_late_legacy_stop_cannot_start_over_active_session(game):
    w=game.wardrive;w.scan.supported=True;w.scan.start();token=w.scan.session
    game._start_scan_cmd("scan_bt","bt_scanning","BT Wardrive")
    assert w.scan.state=="stopping"
    w.handle_line("All operations stopped.")
    assert game._pending_cmd=="scan_bt" and not game.ble_scanning
    w.handle_line(wire(record("stopped",session=token,seq=1)))
    w.handle_line("All operations stopped.")
    assert game._pending_cmd is None and game.ble_scanning


def test_suppressed_body_rule_downgrades_label():
    d=NotableDetector()
    e=record("ble",mac="00:25:DF:00:00:01",data_hex=ad(0x16,b"\x81\xfcBWCDEVICE").hex())
    h=d.classify(e,0,suppressed_rules=["axon-body-tag"])
    assert h[0]["label"]=="Possible Axon device" and h[0]["strength"]==1


def test_capability_retries_and_late_reply():
    now = [0]
    sent = []
    c = ScanController(sent.append, lambda: now[0])
    assert c.probe()
    assert not c.probe()  # repeated clicks cannot postpone the timeout
    for t in (2, 4, 6, 8):
        now[0] = t
        c.tick()
    assert sent == ["get_capabilities"] * 3
    assert c.supported is None and not c.probing and c.probe_error
    assert c.probe()  # selecting All Wardrive retries a missed handshake
    c.handle({"kind": "capabilities", "wardrive_serial_v1": True})
    now[0] = 20
    c.tick()
    assert c.supported is True and not c.probing and not c.probe_error
    assert c.start()
    assert not c.probe()  # never inject a new probe cycle into an active scan


def test_capability_negative_reply_and_reset():
    now = [0]
    sent = []
    c = ScanController(sent.append, lambda: now[0])
    c.probe()
    c.handle({"kind": "capabilities", "wardrive_serial_v1": False})
    now[0] = 10
    c.tick()
    assert c.supported is False and not c.start() and len(sent) == 1
    c.reset()
    c.tick()
    assert c.supported is None and not c.probe_error and not c.probing


def passive_frame(pmkid=True):
    hdr = bytearray(24)
    hdr[0:2] = b"\x08\x02"
    hdr[4:10] = bytes.fromhex("020000000001")
    hdr[10:16] = hdr[16:22] = bytes.fromhex("020000000002")
    key = bytearray(99)
    key[0:2] = b"\x02\x03"
    key[4] = 2
    key[5:7] = (0x008a).to_bytes(2, "big")
    data = b"\xdd\x14\x00\x0f\xac\x04" + bytes(range(16)) if pmkid else b""
    key[97:99] = len(data).to_bytes(2, "big")
    key[2:4] = (len(key)-4+len(data)).to_bytes(2, "big")
    return bytes(hdr) + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + bytes(key) + data


def passive_records(frame, session="test", packet=1, seq=1):
    for offset in range(0, len(frame), 240):
        yield dict(v=1,kind="hs_packet",session=session,seq=seq+offset//240,
                   packet=packet,offset=offset,total=len(frame),capture_ms=1,
                   age_ms=0,channel=6,rssi=-50,data_hex=frame[offset:offset+240].hex())


def test_passive_pcap_pmkid_and_corrupt_fragment(tmp_path):
    from watchdogs.passive_capture import PassiveCapture
    p=PassiveCapture();p.open(tmp_path)
    frame=passive_frame()
    for d in passive_records(frame):
        parsed=parse_record(wire(d));assert parsed;p.accept(parsed)
    assert p.frames==1 and p.eapol==1 and p.pmkids==1
    # Missing middle chunk must never become a malformed PCAP record.
    large=frame+b"\x00"*600
    parts=list(passive_records(large,packet=2,seq=2))
    p.accept(parts[0]);p.accept(parts[2])
    assert p.frames==1 and p.lost==1
    p.close()
    from watchdogs.pcapng import validate_pcapng
    assert p.path.suffix==".pcapng"
    ng=validate_pcapng(p.path.read_bytes())
    assert ng.packets==1 and ng.last_timestamp_us>=946684800000000
    entry=next(d for d in map(json.loads,p.path.with_suffix(".jsonl").read_text().splitlines()) if d["kind"]=="pmkid")
    assert entry["pmkid"]==bytes(range(16)).hex()
    assert entry["bssid"]=="02:00:00:00:00:02" and entry["station"]=="02:00:00:00:00:01"


def test_passive_rsn_encrypted_key_data_and_malformed():
    from watchdogs.passive_capture import inspect_frame, rsn_pmkids
    pmkid=bytes(range(16))
    rsn=b"\x01\x00"+b"\x00\x0f\xac\x04"+b"\x01\x00"+b"\x00\x0f\xac\x04"+b"\x01\x00"+b"\x00\x0f\xac\x02"+b"\x00\x00"+b"\x01\x00"+pmkid
    assert rsn_pmkids(rsn)==[pmkid]
    assert rsn_pmkids(rsn[:-1])==[]
    association=bytearray(28);association[0]=0
    association+=b"\x00\x03lab"+bytes([48,len(rsn)])+rsn
    info=inspect_frame(association)
    assert info["pmkids"]==[pmkid.hex()] and info["ssid_hex"]==b"lab".hex()
    encrypted=bytearray(passive_frame());encrypted[32+5]|=0x10
    assert inspect_frame(encrypted)["pmkids"]==[]
    assert not inspect_frame(passive_frame()[:-1])["eapol"]
    d=next(passive_records(passive_frame()));d["offset"]=1
    assert parse_record(wire(d)) is None


def test_passive_capabilities_and_stop_drain(tmp_path):
    from watchdogs.passive_capture import PassiveCapture
    c=ScanController(Mock(),lambda:0)
    c.handle(dict(kind="capabilities",wardrive_serial_v1=True))
    assert c.hs_supported is False and not c.start("hs_sniff")
    c.handle(dict(kind="capabilities",wardrive_serial_v1=True,hs_sniff_serial_v1=True))
    assert c.start("hs_sniff")
    assert c.send.call_args.args[0].startswith("start_hs_sniff_serial ")
    c.handle(record("started",session=c.session,seq=1))
    c.stop()
    d=next(passive_records(passive_frame(),session=c.session,seq=2))
    assert c.handle(d)  # complete queued packets are accepted during stop
    c.handle(record("stopped",session=c.session,seq=3))
    assert not c.handle(dict(d,seq=4))


def test_sd_warning_matches_command_not_shared_state():
    from watchdogs.app import MENU_CATS, _NEEDS_ESP_SD
    for _, items in MENU_CATS:
        for _, name, cmd, _, _ in items:
            if name=="HS Capture":
                assert cmd in _NEEDS_ESP_SD
            elif name in ("HS Capture no SD", "HS Sniff"):
                assert cmd not in _NEEDS_ESP_SD


def test_passive_ui_capture_and_switch_back_to_wardrive(game):
    w=game.wardrive
    w.handle_line(wire(dict(v=1,kind="capabilities",wardrive_serial_v1=True,wardrive_wifi_serial_v1=True,hs_sniff_serial_v1=True)))
    game._start_scan_cmd("start_hs_sniff_serial","hs_sniff","HS Sniff")
    w.handle_line("All operations stopped.")
    assert w.passive.file and w.scan.mode=="hs_sniff"
    token=w.scan.session
    w.handle_line(wire(record("started",session=token,seq=1)))
    for d in passive_records(passive_frame(),session=token,seq=2):
        w.handle_line(wire(d))
    assert w.passive.eapol==1 and w.passive.pmkids==1
    assert not game.gain_xp.called  # receiving a frame is not a complete handshake
    game._start_scan_cmd("start_wardrive_serial","all_wardrive","All Wardrive")
    w.handle_line("All operations stopped.")
    assert w.scan.state=="stopping" and w.passive.file
    w.handle_line(wire(record("stopped",session=token,seq=3)))
    assert w.passive.file is None
    w.handle_line("All operations stopped.")
    assert w.scan.mode=="wardrive" and w.scan.state=="starting"
    assert not w.scan.wifi_only


@pytest.mark.parametrize("message,key_info,nonce", [(1,0x008a,True),(2,0x010a,True),(3,0x13ca,True),(4,0x030a,False),(0,0x0382,False)])
def test_passive_message_numbers(message,key_info,nonce):
    from watchdogs.passive_capture import inspect_frame
    frame=bytearray(passive_frame(False))
    frame[37:39]=key_info.to_bytes(2,"big")
    frame[49:81]=b"\x42"*32 if nonce else b"\x00"*32
    assert inspect_frame(frame)["message"]==message


def test_passive_clients_do_not_share_progress(tmp_path):
    from watchdogs.passive_capture import PassiveCapture
    p=PassiveCapture();p.open(tmp_path)
    m1=passive_frame(False)
    m4=bytearray(m1);m4[4:10]=bytes.fromhex("020000000099");m4[37:39]=(0x030a).to_bytes(2,"big")
    for packet,frame in enumerate((m1,m4),1):
        for d in passive_records(frame,packet=packet,seq=packet):p.accept(d)
    assert [r["messages"] for r in p.rows.values()]==[[1,0,0,0],[0,0,0,1]]
    p.close()


def test_passive_menu_back_and_reopen_keeps_capture(game,monkeypatch):
    import sys
    px=sys.modules["pyxel"]
    w=game.wardrive;w.scan.hs_supported=True;w.scan.start("hs_sniff")
    w.scan.state="running"
    game.serial.send_command.reset_mock()
    game._execute_item("_hs_sniff_menu","_hs_sniff_menu","HS Sniff",[])
    assert w.hs_screen.open
    monkeypatch.setattr(px,"btnp",lambda key:key==px.KEY_ESCAPE)
    w.hs_screen.update()
    assert not w.hs_screen.open and w.scan.state=="running"
    game._execute_item("_hs_sniff_menu","_hs_sniff_menu","HS Sniff",[])
    assert w.hs_screen.open and w.scan.state=="running"
    game.serial.send_command.assert_not_called()


def test_split_wardrive_host_ble_detection_and_stop(game):
    from watchdogs.host_ble import advertisement_record
    w = game.wardrive
    w.scan.clock = lambda:10
    w.host_ble = Mock(state="idle", drops=0)
    w.host_ble.start.return_value = True
    w.host_ble.poll.return_value = []
    w.handle_line(wire(dict(v=1,kind="capabilities",wardrive_serial_v1=True,wardrive_wifi_serial_v1=True)))
    game._start_scan_cmd("start_wardrive_wifi_serial", "all_wardrive_host", "All Wardrive (host BLE)")
    w.handle_line("All operations stopped.")
    token = w.scan.session
    assert w.scan.wifi_only
    assert game.serial.send_command.call_args.args[0] == "start_wardrive_wifi_serial " + token
    w.handle_line(wire(record("started", session=token, seq=1)))
    w.host_ble.start.assert_called_once_with(token, adapter="auto")
    device = NS(address="C2:00:00:00:00:01", details={"props":{"AddressType":"random"}})
    adv = NS(local_name="Penguin-123", rssi=-52, manufacturer_data={}, service_data={}, service_uuids=[])
    d = advertisement_record(device, adv)
    w.host_ble.poll.return_value = [(token,"started",None), (token,"ble",(10,d))]
    w.poll_host_ble(10)
    assert len(game.ble_devices) == 1 and game.ble_devices[0].name == "Penguin-123"
    item = next(iter(w.notables.values()))
    assert w.position(item) == (40,-90) and item["category"] == "flock"
    assert w.scan.last_heartbeat == 10  # only the firmware started event renewed this
    # Bluetooth records cannot keep a silent firmware session alive.
    w.host_ble.poll.return_value = [(token,"ble",(18,d))]
    w.poll_host_ble(18)
    assert w.scan.last_heartbeat == 10
    game._send("stop")
    assert w.scan.state == "stopping"
    w.host_ble.stop.assert_called()
    w.observation = Mock()
    w.poll_host_ble(18)
    w.observation.assert_not_called()


def test_host_ble_failure_keeps_wifi_running_and_ignores_old_session(game):
    w = game.wardrive
    w.scan.wifi_supported = True
    w.scan.start(wifi_only=True)
    w.scan.state = "running"
    w.host_ble = Mock()
    w.host_ble.poll.return_value = [("old", "error", "ignore")]
    w.poll_host_ble(10)
    assert w.scan.state == "running"
    w.host_ble.poll.return_value = [(w.scan.session, "error", "No powered Bluetooth adapter")]
    w.poll_host_ble(10)
    assert w.scan.state == "running" and "Bluetooth adapter" in w.scan.error
    assert "stop" not in [call.args[0] for call in game.serial.send_command.call_args_list]


def test_dual_diagnostic_works_on_previous_firmware_and_saves_timing(game, monkeypatch):
    import watchdogs.wardrive_ui as ui
    w = game.wardrive
    w.scan.clock = lambda:ui.time.monotonic()
    w.scan.supported = True
    game._start_scan_cmd("start_wardrive_serial", "all_wardrive_test", "ESP Dual Test")
    w.handle_line("All operations stopped.")
    assert w.scan.diagnostic and not w.scan.wifi_only
    token = w.scan.session
    w.handle_line(wire(record("started",session=token,seq=1)))
    monkeypatch.setattr(ui.time,"monotonic",lambda:18)
    w.handle_line(wire(record("wifi",session=token,seq=2)))
    w.tick()
    assert w.scan.state == "running" and w.scan.false_timeouts == 1
    path = Path(game.loot.session_path) / "wardrive_diagnostics.jsonl"
    last = json.loads(path.read_text().splitlines()[-1])
    assert last["stats_age"] == 8 and last["record_age"] == 0 and last["false_timeouts"] == 1
    assert w.host_ble._thread is None
    monkeypatch.setattr(ui.time,"monotonic",lambda:26)
    w.tick()
    assert w.scan.state == "stopping"
    last = json.loads(path.read_text().splitlines()[-1])
    assert "No firmware records" in last["error"]


@pytest.mark.parametrize("label,command,wifi_only,diagnostic", [
    ("All Wardrive", "start_wardrive_serial", False, False),
    ("All Wardrive (host BLE)", "start_wardrive_wifi_serial", True, False),
    ("ESP Dual Test", "start_wardrive_serial", False, True),
])
def test_all_wardrive_menu_selects_distinct_backends(game, label, command, wifi_only, diagnostic):
    from watchdogs.app import MENU_CATS
    game._is_running = Mock(return_value=False)
    w = game.wardrive
    w.scan.supported = True
    w.scan.wifi_supported = wifi_only  # ESP modes must work without the new capability
    entry = next(item for category,items in MENU_CATS if category=="SNIFF"
                 for item in items if item[1]==label)
    game._execute_item(entry[2],entry[3],entry[1],[])
    assert game._pending_state == entry[3]
    w.handle_line("All operations stopped.")
    assert w.scan.state == "starting"
    assert w.scan.wifi_only is wifi_only and w.scan.diagnostic is diagnostic
    assert game.serial.send_command.call_args.args[0] == command + " " + w.scan.session
    # Cell work starts only after the firmware acknowledges the new session.
    assert not w.cell.active


@pytest.mark.parametrize("wifi_only", [False, True])
def test_all_wardrive_starts_enabled_adsb_and_meshcore_after_ack(game, wifi_only):
    w = game.wardrive
    w.settings["lte_modem"] = False
    lora = NS(running=False, mode="")
    lora.set_mc_channels = Mock()

    def start_meshcore(region):
        lora.running = True
        lora.mode = "meshcore"

    lora.start_meshcore = Mock(side_effect=start_meshcore)
    sdr = NS(running=False, mode="")
    sdr.start_adsb = Mock(return_value=True)
    sdr.poll_events = Mock(return_value=[])
    game._lora_enabled = game._sdr_enabled = True
    game._lora = lora
    game._sdr = sdr
    game._mc_region = "US_NARROW"
    game._mc_channels_list = [{"name": "public"}]
    w.scan.supported = True
    w.scan.wifi_supported = True
    if wifi_only:
        w.host_ble = Mock(state="idle", drops=0)
        w.host_ble.start.return_value = True
        w.host_ble.poll.return_value = []

    assert w.scan.start(wifi_only=wifi_only)
    token = w.scan.session
    lora.start_meshcore.assert_not_called()
    sdr.start_adsb.assert_not_called()
    w.handle_line(wire(record("started", session=token, seq=1)))

    lora.set_mc_channels.assert_called_once_with(game._mc_channels_list)
    lora.start_meshcore.assert_called_once_with("US_NARROW")
    sdr.start_adsb.assert_called_once_with(str(game.loot.session_path))
    assert w.scan.state == "running"
    if wifi_only:
        w.host_ble.start.assert_called_once_with(token, adapter="auto")


def test_all_wardrive_aux_collectors_skip_diagnostic_and_busy_radios(game):
    w = game.wardrive
    w.settings["lte_modem"] = False
    game._lora_enabled = game._sdr_enabled = True
    game._lora = NS(
        running=True, mode="meshtastic", start_meshcore=Mock(),
        set_mc_channels=Mock())
    game._sdr = NS(
        running=True, mode="433", start_adsb=Mock(),
        poll_events=Mock(return_value=[]))
    w.scan.supported = True
    assert w.scan.start(diagnostic=True)
    w.handle_line(wire(record(
        "started", session=w.scan.session, seq=1)))
    game._lora.start_meshcore.assert_not_called()
    game._sdr.start_adsb.assert_not_called()

    w.scan.diagnostic = False
    assert not w.start_auxiliary_collectors()
    game._lora.start_meshcore.assert_not_called()
    game._sdr.start_adsb.assert_not_called()
    lines = [call.args[0] for call in game._term_add.call_args_list]
    assert any("MeshCore skipped: LoRa busy" in line for line in lines)
    assert any("ADS-B skipped: SDR busy" in line for line in lines)


def test_all_wardrive_reuses_running_aux_collectors(game):
    w = game.wardrive
    game._lora_enabled = game._sdr_enabled = True
    game._lora = NS(
        running=True, mode="meshcore", start_meshcore=Mock(),
        set_mc_channels=Mock())
    game._sdr = NS(
        running=True, mode="adsb", start_adsb=Mock(),
        poll_events=Mock(return_value=[]))
    w.scan.mode = "wardrive"
    w.scan.state = "running"
    w.scan.diagnostic = False

    assert not w.start_auxiliary_collectors()
    game._lora.start_meshcore.assert_not_called()
    game._sdr.start_adsb.assert_not_called()
    lines = [call.args[0] for call in game._term_add.call_args_list]
    assert "[ALL] MeshCore collection active" in lines
    assert "[ALL] ADS-B collection active" in lines


@pytest.mark.parametrize("wifi_only", [False, True])
def test_all_wardrive_collector_settings_leave_lora_free_and_start_433(
        game, wifi_only):
    w = game.wardrive
    w.settings.update(
        wardrive_lora=False, wardrive_adsb=False, wardrive_433=True,
        lte_modem=False)
    lora = NS(running=False, mode="", start_meshcore=Mock(),
              set_mc_channels=Mock())
    sdr = NS(running=False, mode="", start_adsb=Mock(),
             start_433=Mock(return_value=True), poll_events=Mock(return_value=[]))
    game._lora_enabled = game._sdr_enabled = True
    game._lora = lora
    game._sdr = sdr
    w.scan.mode = "wardrive"
    w.scan.state = "running"
    w.scan.wifi_only = wifi_only
    w.scan.diagnostic = False

    assert w.start_auxiliary_collectors()

    lora.start_meshcore.assert_not_called()
    sdr.start_adsb.assert_not_called()
    sdr.start_433.assert_called_once_with(
        str(game.loot.session_path), game.player_lat, game.player_lon)
    assert w._wdg_owned_sdr is True
    lines = [call.args[0] for call in game._term_add.call_args_list]
    assert any("LoRa disabled (Wardrive Settings" in line
               for line in lines)
    assert "[ALL] 433 MHz collection started (SDR enabled)" in lines


def test_all_wardrive_can_disable_every_host_radio_collector(game):
    w = game.wardrive
    w.settings.update(
        wardrive_lora=False, wardrive_adsb=False, wardrive_433=False,
        lte_modem=False)
    game._lora_enabled = game._sdr_enabled = True
    game._lora = NS(running=False, mode="", start_meshcore=Mock())
    game._sdr = NS(running=False, mode="", start_adsb=Mock(),
                   start_433=Mock())
    w.scan.mode = "wardrive"
    w.scan.state = "running"
    w.scan.diagnostic = False

    assert not w.start_auxiliary_collectors()
    game._lora.start_meshcore.assert_not_called()
    game._sdr.start_adsb.assert_not_called()
    game._sdr.start_433.assert_not_called()


@pytest.mark.parametrize("wifi_only", [False, True])
def test_all_wardrive_uses_selected_meshtastic_client(game, wifi_only):
    w = game.wardrive
    w.settings.update(
        lora_protocol="meshtastic", wardrive_lora=True,
        wardrive_adsb=False, wardrive_433=False, lte_modem=False)
    meshtastic = NS(
        running=False, connected=False, start=Mock(return_value=True))

    def switch(protocol, *, start_if_enabled):
        assert protocol == "meshtastic" and start_if_enabled
        meshtastic.start()
        meshtastic.running = True

    game._switch_lora_protocol = Mock(side_effect=switch)
    game._meshtastic = meshtastic
    game._lora = NS(running=False, mode="", start_meshcore=Mock())
    game._lora_enabled = True
    game._sdr_enabled = False
    w.scan.mode = "wardrive"
    w.scan.state = "running"
    w.scan.wifi_only = wifi_only
    w.scan.diagnostic = False

    assert w.start_auxiliary_collectors()
    game._switch_lora_protocol.assert_called_once_with(
        "meshtastic", start_if_enabled=True)
    meshtastic.start.assert_called_once()
    game._lora.start_meshcore.assert_not_called()
    assert w._wdg_owned_lora


def test_disabled_lora_collector_suppresses_active_meshcore_discovery(game):
    w = game.wardrive
    w.settings["wardrive_lora"] = False
    w.scan.mode = "wardrive"
    w.scan.state = "running"
    w.scan.diagnostic = False
    game._lora_enabled = True
    game._lora = NS(
        running=True,
        mode="meshcore",
        send_meshcore_discovery=Mock(return_value=True),
    )

    assert not w.poll_meshcore_discovery(100.0)
    game._lora.send_meshcore_discovery.assert_not_called()
    assert w.mc_discovery_next > 100.0


@pytest.mark.parametrize("wifi_only", [False, True])
def test_all_wardrive_meshcore_discovery_uses_fresh_moving_gps(
        game, wifi_only):
    send = Mock(return_value=b"tag!")
    game._lora_enabled = True
    game._lora = NS(
        running=True, mode="meshcore", send_meshcore_discovery=send)
    w = game.wardrive
    w.scan.mode = "wardrive"
    w.scan.state = "running"
    w.scan.session = "disc-session"
    w.scan.wifi_only = wifi_only
    w.scan.diagnostic = False

    w.fixes.update(GpsFix(
        valid=True, latitude=40.0, longitude=-90.0, received_at=10), 10)
    assert w.poll_meshcore_discovery(10)
    send.assert_called_once_with(40.0, -90.0, now=10)

    # A fresh fix at the next interval is still skipped until the wardriver
    # has moved the same 25 metres MeshMapper requires.
    w.fixes.update(GpsFix(
        valid=True, latitude=40.0, longitude=-90.0, received_at=40), 40)
    assert not w.poll_meshcore_discovery(40)
    assert send.call_count == 1

    w.fixes.update(GpsFix(
        valid=True, latitude=40.001, longitude=-90.0, received_at=70), 70)
    assert w.poll_meshcore_discovery(70)
    assert send.call_count == 2

    w.scan.diagnostic = True
    w.fixes.update(GpsFix(
        valid=True, latitude=40.002, longitude=-90.0, received_at=100), 100)
    assert not w.poll_meshcore_discovery(100)
    assert send.call_count == 2


@pytest.mark.parametrize("wifi_only", [False, True])
def test_all_wardrive_meshtastic_discovery_is_zero_hop_manager_request(
        game, wifi_only):
    request = Mock(return_value=True)
    game._lora_enabled = True
    game._meshtastic = NS(connected=True, request_discovery=request)
    w = game.wardrive
    w.settings["lora_protocol"] = "meshtastic"
    w.scan.mode = "wardrive"
    w.scan.state = "running"
    w.scan.session = "mt-disc-session"
    w.scan.wifi_only = wifi_only
    w.scan.diagnostic = False

    w.fixes.update(GpsFix(
        valid=True, latitude=40.0, longitude=-90.0, received_at=10), 10)
    assert w.poll_lora_discovery(10)
    request.assert_called_once_with()

    # Meshtastic is intentionally less chatty than MeshCore: 60 seconds and
    # at least 50 metres of movement between NodeInfo requests.
    w.fixes.update(GpsFix(
        valid=True, latitude=40.0, longitude=-90.0, received_at=70), 70)
    assert not w.poll_lora_discovery(70)
    assert request.call_count == 1

    w.fixes.update(GpsFix(
        valid=True, latitude=40.001, longitude=-90.0, received_at=130), 130)
    assert w.poll_lora_discovery(130)
    assert request.call_count == 2


def test_meshcore_messenger_restarts_powered_stopped_receiver(game):
    lora = NS(running=False, mode="", queue=Queue())
    lora.set_mc_channels = Mock()

    def start_meshcore(_region):
        lora.running = True
        lora.mode = "meshcore"

    lora.start_meshcore = Mock(side_effect=start_meshcore)
    game._lora = lora
    game._meshtastic = NS(close=Mock())
    game._lora_enabled = True
    game._watch = NS(connected=False)
    game._mc_channels_list = [{"name": "public"}]
    game._mc_region = "us_ca_narrow"
    game._mc_screen = False
    game._mc_scroll = 99
    game._mc_help_shown = True
    game.menu_open = True

    game._execute_item("_meshcore", "meshcore", "MeshCore Messenger", [])

    lora.set_mc_channels.assert_called_once_with(game._mc_channels_list)
    lora.start_meshcore.assert_called_once_with("us_ca_narrow")
    game._meshtastic.close.assert_called_once_with(stop_daemon=True)
    assert game._mc_screen and game._mc_scroll == 0
    assert not any("Enable LoRa" in call.args[0]
                   for call in game.msg.call_args_list)


def test_protocol_preference_change_does_not_stop_daemon_when_auto_is_off(game):
    game._lora_enabled = True
    game._lora = NS(running=False, mode="", stop=Mock())
    game._meshtastic = NS(close=Mock())

    game._switch_lora_protocol("meshcore", start_if_enabled=False)

    game._meshtastic.close.assert_called_once_with()
    game._lora.stop.assert_not_called()


def test_lora_init_error_is_reported_after_receiver_stops(game):
    radio_events = Queue()
    radio_events.put(("SX1262 not detected on SPI bus", "error"))
    game._lora = NS(running=False, mode="meshcore", queue=radio_events)
    game._mc_event_queue = Queue()
    game._mc_bubbles = []
    game._term_add.reset_mock()
    game.msg.reset_mock()

    game._poll_lora()

    game._term_add.assert_called_once_with(
        "[LoRa] SX1262 not detected on SPI bus", raw=True)
    game.msg.assert_called_once()
    assert "SX1262 not detected" in game.msg.call_args.args[0]


def test_all_wardrive_prefers_batches_and_prints_scan_boundaries(game):
    w=game.wardrive
    caps=dict(v=1,kind="capabilities",wardrive_serial_v1=True,
              wardrive_wifi_serial_v1=True,wardrive_batch_serial_v2=True,
              wardrive_wifi_batch_serial_v2=True)
    w.handle_line(wire(caps))
    game._start_scan_cmd("start_wardrive_serial","all_wardrive","All Wardrive")
    w.handle_line("All operations stopped.")
    token=w.scan.session
    assert game.serial.send_command.call_args.args[0]=="start_wardrive_batch_serial "+token
    w.handle_line(wire(batch_control("started",session=token,seq=1,batch=0)))
    w.handle_line(wire(batch_control("batch_start",session=token,seq=2)))
    w.handle_line(wire(batch_control("batch_results",session=token,seq=3,
                                     batch_wifi=2,batch_ble=3)))
    w.handle_line(wire(batch_record(session=token,seq=4)))
    game.loot.checkpoint_scan_loot=Mock()
    w.handle_line(wire(batch_control("batch_done",session=token,seq=5,
                                     batch_wifi=1,batch_ble=0)))
    lines=[call.args[0] for call in game._term_add.call_args_list]
    assert any("Background scan #1 started" in line for line in lines)
    assert any("Results #1: WiFi 2 | BLE 3" in line for line in lines)
    assert any("Batch #1 complete" in line for line in lines)
    assert len(game.wifi_networks)==1
    game.loot.checkpoint_scan_loot.assert_called_once_with()


@pytest.mark.parametrize("wifi_only", [False, True])
def test_cell_tracking_starts_after_ack_and_saves_on_batch_done(game, wifi_only):
    from watchdogs.cell_monitor import HostCellScanner
    from watchdogs.modem_location import ModemLocationSnapshot
    broker=NS(error="",enabled_sources=5)
    broker.acquire=Mock(return_value=True);broker.release=Mock()
    broker.snapshot=Mock(return_value=ModemLocationSnapshot(
        observed_monotonic=time.monotonic(),observed_utc=20,modem_generation=1,
        operator_id="311480",technology="LTE",lac=0,tac=33544,
        cell_id=33784342,nmea=(),signal_dbm=-101,
        signal_quality_percent=65,modem_path="/org/freedesktop/ModemManager1/Modem/0",
        model="SIMCOM_SIM7600G-H",revision="LE20B04",qmi_device="/dev/cdc-wdm0"))
    w=game.wardrive;w.cell=HostCellScanner(broker,clock=lambda:10)
    w.scan.supported=True;w.scan.wifi_supported=True
    w.scan.batch_supported=True;w.scan.wifi_batch_supported=True
    if wifi_only:
        w.host_ble=Mock(state="idle",drops=0)
        w.host_ble.start.return_value=True;w.host_ble.poll.return_value=[]
    assert w.scan.start(wifi_only=wifi_only)
    token=w.scan.session
    assert not w.cell.active
    w.handle_line(wire(batch_control("started",session=token,seq=1,batch=0)))
    assert w.cell.active
    w.handle_line(wire(batch_control("batch_done",session=token,seq=2,
                                     batch=1,batch_wifi=2,batch_ble=3)))
    w.poll_cell(10)
    with (game.loot._session/"wardriving.csv").open(newline="") as stream:
        next(stream);rows=list(csv.DictReader(stream))
    assert rows[-1]["MAC"]=="311480_33544_33784342"
    assert rows[-1]["Type"]=="LTE" and rows[-1]["RSSI"]=="-101"
    assert len(w.cell.unique)==w.cell.observations==1
    assert game.loot_points[-1]["type"]=="cell"
    w.cell.stop()


def test_cell_tracking_never_starts_for_dual_test(game):
    game.wardrive.cell=Mock(active=False)
    w=game.wardrive;w.scan.supported=True;w.scan.batch_supported=True
    assert w.scan.start(diagnostic=True)
    token=w.scan.session
    w.handle_line(wire(batch_control("started",session=token,seq=1,batch=0)))
    w.cell.start.assert_not_called()


@pytest.mark.parametrize("wifi_only", [False, True])
def test_lte_off_never_starts_cell_and_does_not_block_all_wardrive(game, wifi_only):
    w = game.wardrive
    w.settings["lte_modem"] = False
    w.cell = Mock(active=False)
    w.cell.poll.return_value = []
    w.scan.supported = True
    w.scan.wifi_supported = True
    w.scan.batch_supported = True
    w.scan.wifi_batch_supported = True
    if wifi_only:
        w.host_ble = Mock(state="idle", drops=0)
        w.host_ble.start.return_value = True
        w.host_ble.poll.return_value = []

    assert w.scan.start(wifi_only=wifi_only)
    token = w.scan.session
    w.handle_line(wire(batch_control("started", session=token, seq=1, batch=0)))

    assert w.scan.state == "running"
    w.cell.start.assert_not_called()
    if wifi_only:
        w.host_ble.start.assert_called_once_with(token, adapter="auto")


def test_lte_toggle_stops_only_cell_and_reconfigures_gps(game):
    w = game.wardrive
    w.cell = Mock(active=True)
    w.host_ble = Mock(state="running")
    w.scan.state = "running"
    w.scan.mode = "wardrive"
    game.gps.provider = "serial"
    game.gps.set_modem_enabled = Mock(return_value=True)

    w.toggle_setting("lte_modem")

    assert w.settings["lte_modem"] is False
    w.cell.stop.assert_called_once_with()
    game.gps.set_modem_enabled.assert_called_once_with(False, reconnect=True)
    w.host_ble.stop.assert_not_called()
    assert w.scan.state == "running"
    assert json.loads((Path(game._app_dir) / "wardrive_settings.json").read_text())[
        "lte_modem"] is False


def test_disabling_gps_stops_cell_without_restarting_it(game):
    cell = Mock(active=True)
    cell.poll.return_value=[]
    game.wardrive.cell=cell
    game.wardrive.scan.state="running"
    game.wardrive.scan.mode="wardrive"
    game.gps.available=False
    game.wardrive.poll_cell(10)
    cell.stop.assert_called_once()
    cell.start.assert_not_called()

def test_host_ble_batch_updates_map_immediately_but_groups_terminal(game):
    from watchdogs.host_ble import advertisement_record
    w=game.wardrive;w.scan.supported=True;w.scan.wifi_supported=True
    w.scan.wifi_batch_supported=True;w.scan.start(wifi_only=True);w.scan.state="running"
    w.scan.batch_number=1;w.scan.batch_phase="scanning"
    w.host_ble=Mock(state="running",drops=0)
    device=NS(address="C2:00:00:00:00:01",details={"props":{"AddressType":"random"}})
    adv=NS(local_name="Penguin-123",rssi=-52,manufacturer_data={},service_data={},service_uuids=[])
    d=advertisement_record(device,adv)
    w.host_ble.poll.return_value=[(w.scan.session,"ble",(10,d))]
    game._term_add.reset_mock();w.poll_host_ble(10)
    assert len(game.ble_devices)==1 and len(w.host_ble_batch_lines)==1
    assert not any("[BLE]" in call.args[0] for call in game._term_add.call_args_list)
    token=w.scan.session
    w.handle_line(wire(batch_control("batch_results",session=token,seq=1,
                                     batch_wifi=0,batch_ble=0)))
    assert not w.host_ble_batch_lines
    assert any("[BLE]" in call.args[0] for call in game._term_add.call_args_list)


def test_host_ble_menu_requires_new_firmware_without_stopping_current_scan(game):
    game._is_running = Mock(return_value=False)
    game.wardrive.scan.supported = True
    game.wardrive.scan.wifi_supported = False
    game.serial.send_command.reset_mock()
    game._execute_item("start_wardrive_wifi_serial", "all_wardrive_host", "All Wardrive (host BLE)", [])
    game.serial.send_command.assert_not_called()
    assert "1.7.3" in game.msg.call_args.args[0]
