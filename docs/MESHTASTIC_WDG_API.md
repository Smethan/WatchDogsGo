# Meshtastic WatchDogsGo Local API

WatchDogsGo uses a restricted local API when it runs with the
[`Smethan/meshtastic-firmware`](https://github.com/Smethan/meshtastic-firmware)
daemon. The API carries the node, message, discovery, Bluetooth, and status
operations WDG needs without giving it a second unrestricted Meshtastic
`PhoneAPI` session. The official phone app can therefore remain connected over
Bluetooth while WDG receives its own event stream.

This document describes wire protocol version 1, API version 1.1, as implemented by
`src/platform/portduino/WdgApi.{h,cpp}` on the firmware branch and consumed by
`watchdogs/meshtastic_manager.py` in this repository. It is an implementation
contract, not a general Meshtastic client API.

API 1.1 is shipped by `Smethan/meshtastic-firmware` `v2.8.0-wdg.7` and is the
paired dependency for WDG 0.9.46. The envelope remains version 1; the minor API
version advertises additive capabilities within that envelope. Install the
firmware package before updating WDG so phone-bond reconciliation is available
on WDG's first connection.

## Transport and authorization

- Transport: Linux `AF_UNIX` with `SOCK_SEQPACKET`.
- Default path: `/run/meshtasticd/wdg.sock`.
- Encoding: exactly one UTF-8 JSON object in each packet.
- Protocol version: `1`.
- Maximum packet size: 65,536 bytes.
- Client limit: one connected WDG client.

The daemon creates the socket as mode `0660`. Its root-owned policy at
`/etc/meshtasticd/wdg-portduino.yaml` names one `allowed_uid`. At socket
creation the daemon grants that UID traversal of `/run/meshtasticd` and
read/write access to the socket with POSIX ACLs. It then checks every accepted
connection with `SO_PEERCRED`; only root or the exact configured UID is
accepted. File permissions alone do not authorize a client.

The policy file must be a regular, non-symlink file owned by root, no larger
than 65,536 bytes, and not writable by group or other. Once the daemon account
exists, setup and package installation keep it `root:meshtasticd` mode `0640`.
A missing, malformed, unreadable, or unsafe policy disables the WDG API and
phone Bluetooth while leaving the LoRa node running. `setup.sh` writes the
actual login UID and preserves the other policy settings when it is rerun.

## Envelopes

A command has this form:

```json
{"v":1,"type":"command","request_id":"client-unique-id","name":"get_status","body":{}}
```

`request_id` is a non-empty string of at most 128 bytes. IDs may not be reused
among the most recent 256 commands on one connection. `body` is always an
object when present.

A successful reply has this form:

```json
{"v":1,"type":"reply","request_id":"client-unique-id","ok":true,"body":{}}
```

An unsuccessful reply preserves the request ID where possible:

```json
{"v":1,"type":"reply","request_id":"client-unique-id","ok":false,"error_code":"not_ready","message":"Meshtastic radio service is not ready"}
```

An asynchronous event has a monotonically increasing connection-local
`event_id`:

```json
{"v":1,"type":"event","event_id":42,"name":"node","body":{}}
```

Clients send `hello` first. Its result includes canonical
`api={"major":1,"minor":1}`, compatibility aliases `api_version="1.1"` and
numeric `api_major`/`api_minor`, plus the capability list and
`protocol_version=1`. The daemon accepts commands before negotiation,
but it does not enable NodeDB and message observer events until `hello`
succeeds.

## Commands

| Name | Body | Result and restrictions |
|---|---|---|
| `hello` | Client metadata may be supplied; version 1 does not depend on it. | Returns wire `protocol_version`, API major/minor, `max_packet_bytes`, and supported capability names, then emits `ready`. |
| `get_status` | `{}` | Returns the complete status object described below. |
| `snapshot_nodes` | `{}` | Starts one NodeDB snapshot. The reply contains `count`; events follow as `snapshot_begin`, zero or more `node`, and `snapshot_complete`. A second concurrent snapshot returns `busy`. |
| `send_text` | `text`, optional `destination`, `channel`, `want_ack` | Sends through the firmware's normal packet allocation, rate limit, channel, PKI, encryption, and radio queues. Text is 1–233 UTF-8 bytes. Destination defaults to broadcast and accepts a node number or `!xxxxxxxx`; channel defaults to 0. WDG explicitly sets `want_ack=false` for broadcast/channel text and `true` for direct text. Sends are limited to one every two seconds. |
| `request_node_info` | Optional `hop_limit`, which must be `0` | Broadcasts a direct, zero-hop NodeInfo request. The daemon enforces one request per 60 seconds. |
| `set_phone_ble` | Required boolean `enabled`; optional `adapter` | Enables or disables the Linux BlueZ phone service. The adapter is `auto`, an `hciX` name, or a controller MAC. A policy-pinned adapter cannot be changed through the socket, and an adapter cannot be changed while a phone is connected. |
| `set_phone_pairing_mode` | `mode="random_pin"` | Persists authenticated random-PIN pairing and restarts the BlueZ phone surface when required. No fixed-PIN or silent no-PIN substitution is accepted by this command. |
| `adopt_phone_bond` | Exact phone `address` and stable controller MAC in `controller` | Adopts an existing BlueZ bond only when it is the unambiguous exact device on the selected adapter and BlueZ reports `Paired`, `Bonded`, and `Trusted`. A conflicting daemon identity or unrelated connected phone fails closed. |
| `clear_phone_identity` | `expected_address` | Clears only the daemon's matching retained phone identity. A connected phone or a different retained address is rejected. It does not bulk-remove BlueZ devices. |
| `open_pairing` | Positive integer `seconds` | Opens an explicit unbonded-phone pairing window, capped by both the API and host policy at 120 seconds. It fails while a phone is connected or a bond already exists. |
| `forget_phone` | `{}` | Removes the daemon identity and the exact stored BlueZ phone. The phone must first be disconnected. WDG removes its controller registry entry only after this succeeds. |
| `ble_scan_lease_acquire` | Positive integer `seconds` | Grants a Host BLE scan window capped at 20 seconds. With no phone, advertising is suspended. With a connected phone, the lease is marked shared and WDG attempts concurrent discovery without disconnecting it. An open pairing window denies the lease. |
| `ble_scan_lease_release` | `{}` | Releases the Host BLE scan lease and restores phone advertising when possible. |
| `pairing_agent_lease_acquire` | Positive integer `seconds` | Yields the daemon's BlueZ pairing agent for another bounded WDG pairing flow, capped at 120 seconds. |
| `pairing_agent_lease_release` | `{}` | Restores the Meshtastic pairing agent. |
| `retry_shared_adapter` | `{}` | Clears the session-only shared-controller degradation state and retries advertising after Host BLE has stopped. |

The two Bluetooth leases expire automatically and are also released when WDG
disconnects. If a phone connects during an exclusive scan lease, advertising
is restored and the lease becomes shared. If a connected phone drops during a
shared lease, the remainder becomes exclusive so WDG cannot accept a new phone
mid-scan. WDG treats repeated uncommanded phone disconnects as an incompatible
shared controller and pauses Host BLE until the user retries it.

A client timeout after a lease command has been written is indeterminate: the
daemon may have applied the command even though its reply was lost. WDG sends
an ordered compensating release after a timed-out acquisition and keeps the
lease in a local `possibly-active` state if that release is also unconfirmed.
The same owner must retry release before new Bluetooth work begins. Only an
acknowledged release or socket disconnect retires that conservative ownership;
a late reply for the cancelled acquisition cannot reactivate it.

## Status body

`get_status` and the initial `ready` event include:

| Field | Meaning |
|---|---|
| `state` | `ready` after the local API is initialized. |
| `client_connected` | Whether the one WDG client is connected. |
| `pending_replies` | Current outbound packet count. |
| `packets_received` | All accepted remote mesh packets observed by the firmware core, independent of whether a WDG client is connected. |
| `radio_status` | `ready` or `unavailable`. |
| `full_client_owner` | `none`, `bluetooth`, `bluetooth_pending`, or `tcp`. The restricted WDG socket never owns this lease. |
| `identity` | Local `node_id`, `name`, `short_name`, `has_public_key`, and `has_private_key`. |
| `channels` | Enabled channels as `index`, `name`, numeric `role`, and `has_psk`. |
| `phone_connected` | Whether the Meshtastic phone transport is connected. |
| `ble_status` | `disabled_by_policy`, `unavailable`, `connected`, `pairing`, `scan_lease`, `pairing_agent_lease`, `advertising`, or `ready`. |
| `phone_ble_enabled` | Whether the BlueZ transport is running. |
| `phone_ble_adapter` | Applied Linux controller name. |
| `phone_ble_adapter_address` | Applied stable controller MAC when available. |
| `ble_scan_lease_active` | Whether WDG currently holds the bounded scan lease. |
| `ble_scan_lease_shared` | Whether that lease is concurrently using an adapter with a connected phone. |
| `pairing_agent_lease_active` | Whether WDG currently holds the bounded pairing-agent lease. |
| `phone_ble_policy_enabled` | Whether host policy permits phone BLE. |
| `phone_bond` | API 1.1 summary containing `present`, `address`, display `name`, stable `controller`, `paired`, `bonded`, `trusted`, `connected`, `service_authorized`, and `authentication`. It contains no passkey or link key. |

Security material is deliberately reduced to presence booleans. The local API
does not return channel PSKs, public or private key bytes, admin sessions, raw
configuration protobufs, or unrestricted `ToRadio`/`FromRadio` traffic.

## Events

| Name | Important body fields |
|---|---|
| `ready` | Full status body after `hello`. |
| `status` | Changed status fields, currently including `full_client_owner`. |
| `snapshot_begin` | Expected `count`. |
| `node` | `id`, numeric `num`, `name`, `short_name`, numeric `hardware`, `rssi`, `snr`, `hops`, `last_heard`, `cached`, `lat`, and `lon`. Snapshot rows have `cached=true`. |
| `snapshot_complete` | Final snapshot `count`. |
| `message` | Sanitized `text`, `sender_id`, display `sender`, `channel`, `rssi`, `snr`, `hops`, and `packet_id`. |
| `send_accepted` | `request_id`, `packet_id`, `destination`, `channel`, and `want_ack`. This means the firmware accepted the packet for sending; it is not a routing acknowledgement. |
| `send_failed` | `request_id`, `packet_id`, Meshtastic `error`, and `message`. |
| `send_status` | Correlatable direct-send result with `request_id`, `packet_id`, `destination`, and `state=delivered|failed`; failures may include a routing `error` and `detail`. Direct tracking is bounded to 64 pending packets and fails after 120 seconds without a routing result. |
| `discovery_sent` | `request_id`, `hop_limit=0`, and a display `message`. |
| `phone_connected` / `phone_disconnected` | `phone_connected` and a display `message`. |
| `ble_status` | `state`, connection/advertising/lease flags, and optional shared-controller pause reason. |
| `radio_status` | New radio `state`. |
| `pairing_passkey` | Six-digit `passkey`/`pin` plus a display message. |
| `phone_bond` | Current API 1.1 non-secret bond summary after its identity, authorization, connection, or controller state changes. WDG normalizes this into its shared controller registry. |
| `overflow` | Dropped `count` and `resync_required=true`. |

The firmware currently embeds identity and channel metadata in status; it does
not emit key material or separate configuration records.

## Backpressure and resynchronization

The outbound queue is bounded to 256 packets or 1 MiB. Node events are low
priority and are removed before replies, message events, or delivery-state
events when capacity is needed. The observer inbox separately coalesces at
most 256 node identities, holds at most 128 text events or 64 KiB of text, and
bounds direct-message delivery tracking. If low-priority observations are
dropped, the daemon emits `overflow` as soon as queue space permits.

On `overflow`, WDG requests `snapshot_nodes` and replaces its cached node view
when `snapshot_complete` arrives. If the queue contains no low-priority packet
that can be removed and a required reply or event cannot fit, the daemon closes
the client. WDG reconnects, negotiates again, and requests a new snapshot.

## Compatibility

WDG's `auto` backend uses this socket whenever the fork socket is live or the
fork service is installed. It does not silently fall back to TCP while the fork
could be serving a phone. `legacy_tcp` remains available for stock
`meshtasticd`; it is a separate unrestricted PhoneAPI path and does not use
this JSON protocol. API 1.0 can still provide its earlier observation and
control capabilities, but WDG disables the shared-bond controls unless the
daemon negotiates API 1.1 and advertises all required capabilities.

See [Meshtastic service integration](MESHTASTIC_SERVICE.md) for installation,
radio ownership, Bluetooth coexistence, and rollback behavior.
