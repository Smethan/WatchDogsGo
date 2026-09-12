"""Evidence-aware Flock/Axon matching. Never equate a vendor with a camera."""
from collections import OrderedDict
import json
from pathlib import Path
import re


def ad_fields(data):
    """Parse all AD elements; reject malformed packets instead of matching raw bytes."""
    fields = []
    pos = 0
    while pos < len(data):
        size = data[pos]
        if size == 0:
            break
        if pos + size >= len(data):
            return []
        fields.append((data[pos+1], data[pos+2:pos+size+1]))
        pos += size + 1
    return fields


def ble_name(data):
    names = [v for t, v in ad_fields(data) if t in (8, 9)]
    return names[-1].decode("utf-8", "replace") if names else ""


class NotableDetector:
    def __init__(self):
        self.rules = json.loads((Path(__file__).parent / "data/notable_signatures.json").read_text())["rules"]
        self.cache = OrderedDict()

    def clear(self):
        self.cache.clear()

    def classify(self, event, now, blocked=lambda mac: False, suppressed_rules=()):
        kind, mac = event["kind"], event["mac"]
        if blocked(mac):
            return []
        fields = []
        if kind == "ble":
            data = bytes.fromhex(event.get("data_hex", ""))
            key = (mac, event.get("addr_type"))
            entries = self.cache.pop(key, {})
            entries = {k:v for k,v in entries.items() if now-v[0] <= 3}
            entries[event.get("event", 0)] = (now, ad_fields(data))
            self.cache[key] = entries
            while len(self.cache) > 512:
                self.cache.popitem(last=False)
            fields = [field for _, fs in entries.values() for field in fs]
        names = [v.decode("utf-8", "replace") for t,v in fields if t in (8,9)]
        if kind != "ble":
            names.append(bytes.fromhex(event.get("ssid_hex", "")).decode("utf-8", "replace"))
        if event.get("name"):
            names.append(event["name"])
        # A BLE random/static/private address cannot identify a manufacturer by OUI.
        public = kind != "ble" or event.get("addr_type") == 0
        public = public and not (int(mac[:2],16) & 3)
        hits = []
        for rule in self.rules:
            if rule["id"] in suppressed_rules:
                continue
            method, value = rule["method"], rule["value"]
            match = False
            if method == "oui":
                match = public and mac.startswith(value + ":")
            elif method == "name":
                match = any(re.fullmatch(value, name) for name in names)
            elif method == "company":
                match = any(t == 255 and len(v)>=2 and int.from_bytes(v[:2],"little")==value for t,v in fields)
            elif method == "service_uuid":
                for t,v in fields:
                    values = [int.from_bytes(v[i:i+2], "little") for i in range(0,len(v)-1,2)] if t in (2,3) and len(v)%2==0 else ([int.from_bytes(v[:2], "little")] if t==0x16 and len(v)>=2 else [])
                    match = match or value in values
            elif method == "service_tag":
                # Only actual service-data fields, following their UUID; never arbitrary AD bytes.
                for t,v in fields:
                    prefix = {0x16:2,0x20:4,0x21:16}.get(t)
                    if prefix and value.encode() in v[prefix:]:
                        match = True
            if match:
                hits.append(dict(rule))
        if kind == "wifi_mgmt" and event.get("subtype") == 4 and event.get("ssid_present") and event.get("ssid_hex") == "":
            if "flock-wildcard-probe" not in suppressed_rules and any(h["id"] == "flock-oui" for h in hits):
                hits.append(dict(id="flock-wildcard-probe",category="flock",method="wildcard_probe",strength=2,source=self.rules[0]["source"]))
        results = []
        for category in ("flock", "axon"):
            evidence = [h for h in hits if h["category"] == category]
            if not evidence:
                continue
            strength = max(h["strength"] for h in evidence)
            label = ("Axon body-camera signature" if strength >= 3 else "Possible Axon device") if category == "axon" else ("Flock signature match" if strength >= 2 else "Possible Flock" if strength == 1 else "Flock-associated OEM clue")
            results.append(dict(category=category,strength=strength,label=label,evidence=evidence))
        return results
