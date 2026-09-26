"""Structured Packet Sniffer export and picker-integration tests."""

import json

from watchdogs.sniffer_intel import (
    SnifferIntelCollector,
    parse_sniffer_record,
)


def wire(kind, session="intel_token", **extra):
    record = {"v": 1, "kind": kind, "session": session}
    record.update(extra)
    return "SNIFF:" + json.dumps(record, separators=(",", ":"))


def summary(**extra):
    values = {"available": True, "packets": 913, "aps": 1,
              "probes": 1, "age_ms": 1500}
    values.update(extra)
    return wire("summary", **values)


def ap(**extra):
    values = {
        "seq": 1, "bssid": "02:00:00:00:00:01",
        "ssid_hex": "4c61622057694669", "channel": 6,
        "clients": 3, "packets": 421, "probes": 1,
    }
    values.update(extra)
    return wire("ap", **values)


def probe(**extra):
    values = {
        "seq": 1, "mac": "02:00:00:00:10:01",
        "ssid_hex": "4c61622057694669", "rssi": -61,
    }
    values.update(extra)
    return wire("probe", **values)


def done(**extra):
    values = {"aps": 1, "probes": 1}
    values.update(extra)
    return wire("done", **values)


def test_parser_and_collector_commit_only_a_complete_token_bound_export():
    collector = SnifferIntelCollector()
    assert collector.start("intel_token") == "show_sniffer_intel intel_token"
    assert not collector.accept(parse_sniffer_record(summary()))
    assert not collector.accept(parse_sniffer_record(ap()))
    assert not collector.accept(parse_sniffer_record(probe()))
    assert collector.accept(parse_sniffer_record(done()))
    assert collector.state == "complete"
    assert collector.packets == 913 and collector.age_ms == 1500
    assert collector.aps[0].bssid == "02:00:00:00:00:01"
    assert (collector.aps[0].client_count, collector.aps[0].packet_count,
            collector.aps[0].probe_count) == (3, 421, 1)
    assert collector.probes[0].ssid == "Lab WiFi"
    assert collector.probes[0].rssi == -61


def test_collector_rejects_truncation_reordering_and_wrong_sessions():
    collector = SnifferIntelCollector()
    collector.start("intel_token")
    assert not collector.accept(parse_sniffer_record(ap()))
    assert collector.state == "error"

    collector.start("intel_token")
    assert not collector.accept(parse_sniffer_record(summary()))
    assert not collector.accept(parse_sniffer_record(
        ap(session="other_token")))
    assert collector.state == "receiving"
    assert not collector.accept(parse_sniffer_record(done()))
    assert collector.state == "error"


def test_parser_rejects_unbounded_or_invalid_intel_records():
    bad = [
        summary(packets=-1),
        summary(aps=101),
        ap(clients=51),
        ap(packets=2**32),
        ap(bssid="03:00:00:00:00:01"),
        ap(ssid_hex="zz"),
        probe(rssi=-128),
        probe(mac="FF:FF:FF:FF:FF:FF"),
        wire("unknown"),
        "SNIFF:null",
    ]
    assert all(parse_sniffer_record(line) is None for line in bad)


def test_collector_rejects_internally_inconsistent_metrics():
    collector = SnifferIntelCollector()
    collector.start("intel_token")
    assert not collector.accept(parse_sniffer_record(summary(available=False)))
    assert collector.state == "error"

    collector.start("intel_token")
    assert not collector.accept(parse_sniffer_record(summary()))
    assert not collector.accept(parse_sniffer_record(ap(packets=914)))
    assert collector.state == "error"
