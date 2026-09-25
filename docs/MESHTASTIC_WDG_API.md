# Meshtastic WatchDogsGo Local API

WatchDogsGo uses a restricted local API when it runs with the
[`Smethan/meshtastic-firmware`](https://github.com/Smethan/meshtastic-firmware)
daemon. The API carries the node, message, discovery, Bluetooth, and status
operations WDG needs without giving it a second unrestricted Meshtastic
`PhoneAPI` session. The official phone app can therefore remain connected over
Bluetooth while WDG receives its own event stream.

This document describes protocol version 1 as implemented by
`src/platform/portduino/WdgApi.{h,cpp}` on the firmware branch and consumed by
`watchdogs/meshtastic_manager.py` in this repository. It is an implementation
contract, not a general Meshtastic client API.

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

Clients send `hello` first. The daemon accepts commands before negotiation,
but it does not enable NodeDB and message observer events until `hello`
succeeds.

## Commands

| Name | Body | Result and restrictions |
|---|---|---|
| `hello` | Client metadata may be supplied; version 1 does not depend on it. | Returns `protocol_version`, `max_packet_bytes`, and the supported command names, then emits `ready`. |
| `get_status` | `{}` | Returns the complete status object described below. |
| `snapshot_nodes` | `{}` | Starts one NodeDB snapshot. The reply contains `count`; events follow as `snapshot_begin`, zero or more `node`, and `snapshot_complete`. A second concurrent snapshot returns `busy`. |
| `send_text` | `text`, optional `destination`, `channel`, `want_ack` | Sends through the firmware's normal packet allocation, rate limit, channel, PKI, encryption, and radio queues. Text is 1–233 UTF-8 bytes. Destination defaults to broadcast and accepts a node number or `!xxxxxxxx`; channel defaults to 0. Sends are limited to one every two seconds. |
| `request_node_info` | Optional `hop_limit`, which must be `0` | Broadcasts a direct, zero-hop NodeInfo request. The daemon enforces one request per 60 seconds. |
| `set_phone_ble` | Required boolean `enabled`; optional `adapter` | Enables or disables the Linux BlueZ phone service. The adapter is `auto`, an `hciX` name, or a controller MAC. A policy-pinned adapter cannot be changed through the socket, and an adapter cannot be changed while a phone is connected. |
| `open_pairing` | Positive integer `seconds` | Opens an explicit unbonded-phone pairing window, capped by both the API and host policy at 120 seconds. It fails while a phone is connected or a bond already exists. |
| `forget_phone` | `{}` | Removes the one stored phone bond. The phone must first be disconnected. |
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
| `send_accepted` | `request_id`, `packet_id`, `destination`, `channel`, and accepted `text`. This means the firmware accepted the packet for sending; it is not a routing acknowledgement. |
| `send_failed` | `request_id`, `packet_id`, Meshtastic `error`, and `message`. |
| `discovery_sent` | `request_id`, `hop_limit=0`, and a display `message`. |
| `phone_connected` / `phone_disconnected` | `phone_connected` and a display `message`. |
| `ble_status` | `state`, connection/advertising/lease flags, and optional shared-controller pause reason. |
| `radio_status` | New radio `state`. |
| `pairing_passkey` | Six-digit `passkey`/`pin` plus a display message. |
| `overflow` | Dropped `count` and `resync_required=true`. |

The firmware currently embeds identity and channel metadata in status; it does
not emit key material or separate configuration records.

## Backpressure and resynchronization

The outbound queue is bounded to 256 packets or 1 MiB. Node events are low
priority and are removed before replies or message events when capacity is
needed. The observer inbox separately coalesces at most 256 node identities and
holds at most 128 text events or 64 KiB of text. If low-priority observations
are dropped, the daemon emits `overflow` as soon as queue space permits.

On `overflow`, WDG requests `snapshot_nodes` and replaces its cached node view
when `snapshot_complete` arrives. If the queue contains no low-priority packet
that can be removed and a required reply or event cannot fit, the daemon closes
the client. WDG reconnects, negotiates again, and requests a new snapshot.

## Compatibility

WDG's `auto` backend uses this socket whenever the fork socket is live or the
fork service is installed. It does not silently fall back to TCP while the fork
could be serving a phone. `legacy_tcp` remains available for stock
`meshtasticd`; it is a separate unrestricted PhoneAPI path and does not use
this JSON protocol.

See [Meshtastic service integration](MESHTASTIC_SERVICE.md) for installation,
radio ownership, Bluetooth coexistence, and rollback behavior.
