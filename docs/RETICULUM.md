# Reticulum/LXMF on the AIO SX1262

## Status and scope

This integration is **experimental**. The software path is implemented and
covered by synthetic tests, but it is not release-ready until the physical
interoperability gate below passes on an AIO v2 uConsole and a standard RNode.
LXMF itself is described upstream as beta software and has not received an
external security audit.

Version one is an endpoint-only client. It provides a persistent Reticulum
identity, LXMF delivery announces, contact discovery, encrypted direct or
propagated text, explicit propagation-node inbox sync, delivery state, private
local history, and per-session loot. It does not enable Reticulum transport
mode, route third-party traffic, or implement file/resource transfer,
telemetry, groups, channels, or map markers.

Reticulum and LXMF are installed from PyPI and are not vendored. Their own
[Reticulum License](https://github.com/markqvist/Reticulum/blob/master/LICENSE)
and [LXMF license](https://github.com/markqvist/LXMF/blob/master/LICENSE) apply
separately from WatchDogsGo's license.

## Process and ownership model

WDG never creates `RNS.Reticulum` in the Pyxel process. Selecting Reticulum
starts a supervised child with the active virtual-environment interpreter:

```text
python -m watchdogs.reticulum_sidecar --profile <private-profile> --socket <private-socket>
```

The child hosts exactly one RNS instance, one LXMF router, one delivery
identity, and one `AioSX1262Interface`. Its RNS configuration sets
`share_instance = No` and `enable_transport = No`. The child exits completely
before another protocol may own the SX1262.

Parent and child use a mode-0600 Linux `SOCK_SEQPACKET` socket below a validated
mode-0700 user runtime directory. The parent accepts only the exact spawned PID
and current UID through `SO_PEERCRED`. There is no TCP listener, root daemon,
systemd unit, or privileged helper.

MeshCore and Reticulum are the two direct-SPI owners. Meshtastic remains the
daemon owner. The existing process lock and exact active/enabled service
snapshot are shared by all three paths. Direct-to-direct handoffs retain the
same snapshot; direct-to-daemon handoffs restore it before transactional daemon
selection. A failed radio close or unverifiable lock release activates the
existing restart-required ownership barrier.

## Radio transport

`watchdogs.reticulum_interface.AioSX1262Interface` is an RNS external interface
for `/dev/spidev1.0`. It uses the AIO v2 reset GPIO 25, busy GPIO 24, polling
IRQ arrangement, DIO2 RF switch, and DIO3 1.8 V TCXO. All LoRaRF calls,
including initialization and teardown, run on one worker thread.

The implementation follows the on-air framing and CSMA constants in RNode
Firmware commit
[`f84c7c79ed0553deeac4aa1319dc6f12e0953907`](https://github.com/markqvist/RNode_Firmware/tree/f84c7c79ed0553deeac4aa1319dc6f12e0953907):

- explicit LoRa header, CRC enabled, standard IQ, sync word `0x1424`;
- computed RNode preamble and `HW_MTU = 508`;
- one-byte random sequence/split header and adjacent two-frame transmission at
  the 254/255-byte boundary;
- matching-sequence reassembly with malformed, stale, CRC-failed, and duplicate
  fragment rejection;
- DIFS, four airtime contention bands, frozen/restarted contention, three-slot
  post-transmit yield, noise-floor/interference detection, and short/long
  airtime limits;
- continuous receive outside transmission and fatal teardown on missed
  `TX_DONE`, SPI failure, or unrecoverable modem state.

The direct-SPI implementation is not an RNode serial emulator. It is an
external RNS interface that deliberately uses the same LoRa wire framing and
medium-access behavior so it can interoperate over RF with a standard RNode.

### LoRa, transport nodes, and TCP

WDG does not send TCP over the SX1262. It sends native Reticulum packets over
LoRa. A reachable Reticulum transport node may forward those packets from its
LoRa interface to any of its other RNS interfaces, including a TCP backbone,
and onward to an LXMF propagation node:

```text
WDG AIO SX1262 -- native RNS over LoRa --> RF transport node
                                         |
                                         +-- another RNS interface/backbone
                                             --> LXMF propagation node
```

WDG remains an endpoint with `enable_transport = No`; it neither forwards
third-party traffic nor needs a local TCP interface for this path. The RF
transport is joined by matching its frequency, bandwidth, spreading factor,
coding rate, sync word, and optional IFAC. An RNode `transport_identity` or an
observed transport hash identifies transport infrastructure, but is not a
connection target entered into WDG. A 125 kHz RNode and a 500 kHz RNode are on
different, mutually incompatible RF configurations even when every other value
matches.

## Configuration

Open **SNIFF > Wardrive Settings > LoRa settings > Reticulum RF and identity**.
The first visit is seeded from the current MeshCore region, but selection is
not committed until the operator reviews and confirms:

- frequency, bandwidth, spreading factor, coding rate, and transmit power;
- short- and long-term airtime limits;
- optional paired IFAC network name and passphrase;
- an optional 32-hex LXMF propagation-node destination and whether outbound
  messages should use direct delivery or propagation storage.

Confirmation is an operator acknowledgement, not a regulatory-compliance
claim. Peer radio/IFAC settings, antenna suitability, permitted bands, power,
and duty cycle must match the deployment and local rules.

The default interface is public/unfiltered. A private IFAC requires both a
network name and passphrase; neither may be supplied alone. Active edits are
transactional: WDG starts the pending profile, commits it only after sidecar
readiness, and restarts the previous profile if the candidate fails.

For the WDG Wars service, the propagation destination supplied by its operator
is `90ab9d448f17f3a121dc0f1230af39be`. The separately published TCP endpoint
`rns.wdgwars.pl:4242` and transport hash
`74f1c6e4b668f42c6a8882b3273aab59` describe backbone/transport connectivity;
they are not entered into this LoRa-only endpoint. WDG still needs an in-range
RF transport with exactly matching radio and IFAC parameters. For example, a
nearby RNode configured for 914.875 MHz, 125 kHz, SF8, and CR 4/5 maps to:

```text
frequency       914875000 Hz
bandwidth       125000 Hz
spreading factor 8
coding rate     4/5
```

Use 500 kHz only if the particular reachable RF transport is actually
configured for 500 kHz. WDG cannot receive 125 kHz and 500 kHz profiles at the
same time. Choose local TX power for the AIO hardware, antenna, and applicable
rules; it is not learned from the remote node.

Private state is stored below `reticulum/` in the WDG application data root:

```text
reticulum/profile.json       mode 0600
reticulum/identity           mode 0600
reticulum/history.jsonl      mode 0600
reticulum/rns/               mode 0700
reticulum/lxmf/              mode 0700
```

Symlinked private paths are rejected. The Reticulum identity is separate from
the MeshCore Ed25519 key and Meshtastic identity.

## Messenger and quiet operation

Reticulum reuses **ADDONS > Mesh Messenger**. It has no channel picker:

| Key | Reticulum action |
|---|---|
| `Ctrl+H` | Select a discovered LXMF delivery contact |
| `Enter` | Send an encrypted direct or propagated message to the selected contact, according to the active profile |
| `Ctrl+A` | Explicitly announce the local LXMF delivery destination |
| `Ctrl+P` | Request stored messages from the configured propagation node; press again to cancel an active sync |
| `Ctrl+N` | Transactionally change the display name |
| `Ctrl+X` | Clear the visible chat log |

Startup is quiet. Starting Reticulum or All Wardrive sends no announce and
schedules no periodic discovery. The first explicit outbound message announces
once immediately before queueing; `Ctrl+A` announces explicitly and satisfies
that session's one-time announce. Protocol responses addressed to the local
identity remain enabled.

Propagation sync is also explicit: startup and All Wardrive never request
stored messages automatically. When propagated outbound is enabled, a message
shown as `STORED` has been accepted by the propagation node for
store-and-forward. It does not claim that the final recipient has downloaded
or read the message. Direct outbound retains the normal `SENT`, `DELIVERED`,
and `FAILED` lifecycle.

All Wardrive passively records delivery announces and direct messages. It does
not transmit a Reticulum discovery probe. An announce has no standard position,
so Reticulum contacts are not placed on the map.

RNS/LXMF encrypt messages in transit. WDG stores plaintext at rest, matching
the existing MeshCore/Meshtastic logging model. The private durable history
loads at most its newest 200 valid message records into the UI. An active loot
session also receives `reticulum_contacts.csv` and
`reticulum_messages.log`.

## Verification

Automated tests cover configuration/permissions, wire boundaries and golden
headers, split reassembly failures, deterministic CSMA timing and airtime,
single-thread fake-radio access, teardown-before-unlock, IPC validation,
sidecar configuration, LXMF verification/direct construction, and
direct-to-direct rollback. The complete pre-existing WDG suite must also pass.

The following hardware gate is still mandatory before calling the feature
release-ready:

1. Match frequency, bandwidth, SF, CR, IFAC, and antennas between the AIO v2
   endpoint and a standard RNode.
2. Confirm two-way announces, names/hashes, path distance, direct messages, and
   delivery callbacks. If using WDG Wars, also confirm a path to its configured
   propagation destination, one successful explicit inbox sync, and one
   propagated message accepted as `STORED` through the RF transport/backbone.
3. Exchange at least 20 messages each way with 20/20 correct plaintext and
   terminal delivery state.
4. Prove one-frame and split-frame interoperability across the 254/255-byte
   boundary with lower-level RNS packets.
5. Verify ten quiet minutes emit no packet, then verify `Ctrl+A` and the first
   outbound message produce the intended one-time announcement behavior.
6. Exercise a busy channel and observe DIFS/contention rather than immediate
   transmission.
7. Complete at least 25 three-way protocol switches and a two-hour receive/send
   soak with no child, lock, identity, daemon-state, SPI, or queue leak.
8. Power-cycle and confirm identity, destination, profile, and history
   persistence.

Synthetic results are not evidence of RF interoperability. A failure anywhere
in this gate blocks release-ready status.
