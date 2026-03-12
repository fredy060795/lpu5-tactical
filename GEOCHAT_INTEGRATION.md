# CoT GeoChat Bridge – ATAK/WinTAK Integration

This document describes how ATAK/WinTAK **GeoChat** messages (CoT type `b-t-f`) are
automatically bridged into the LPU5 **All Units** chat channel, and how to configure
and troubleshoot the integration.

---

## Overview

```
ATAK / WinTAK                       LPU5 Backend
─────────────────────────────────────────────────────
GeoChat message ──TCP 8088──────► CoT Listener
                ──UDP 4242──────► │
                ──Multicast──────► │  type=b-t-f?
                 239.2.3.1:6969   │      │
                                  ▼      ▼
                          _cot_listener_ingest_callback
                                  │
                                  ▼
                          _ingest_atak_geochat()
                            • saves ChatMessage (channel='all')
                            • WebSocket broadcast → all connected clients
                            • LPU5 chat window shows message in real time
```

---

## Configuration (`config.json`)

The CoT listener is enabled via `config.json` in the LPU5 root directory.  The
default configuration ships with the listener **on** and all standard ports
pre-configured:

```json
{
    "cot_listener_enabled": true,
    "cot_listener_tcp_port": 8088,
    "cot_listener_udp_port": 4242,
    "sa_multicast_enabled": true,
    "sa_multicast_group":  "239.2.3.1",
    "sa_multicast_port":   6969
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `cot_listener_enabled` | `true` | Enable/disable the CoT socket listener |
| `cot_listener_tcp_port` | `8088` | TCP port for ATAK streaming connections |
| `cot_listener_udp_port` | `4242` | UDP port for ATAK/WinTAK SA datagrams |
| `sa_multicast_enabled` | `true` | Enable LAN SA Multicast listener |
| `sa_multicast_group` | `239.2.3.1` | SA Multicast group address |
| `sa_multicast_port` | `6969` | SA Multicast port |

Changes to `config.json` take effect after restarting LPU5 **or** by calling the
listener management API (see below).

---

## ATAK / WinTAK Client Setup

### Adding the LPU5 Server as a TAK Server Connection

1. Open **ATAK** → ☰ Menu → **Settings** → **Network** → **TAK Servers**.
2. Tap **+** to add a new server.
3. Set:
   - **Description**: `LPU5`
   - **Address**: `<LPU5 host IP or hostname>`
   - **Port**: `8088`
   - **Protocol**: `TCP` (use `SSL/TLS` if the LPU5 instance is configured with TLS)
4. Save and connect.

### Sending a GeoChat Message

After connecting, open the ATAK **GeoChat** panel, select the **All Chat Rooms**
channel, and send a message.  The message appears in the LPU5 web app's
**All Units** chat window within seconds.

> **Tip**: WinTAK users follow the same steps via **Preferences → Network**
> → **TAK Server Configuration**.

---

## Data Flow Details

### Incoming: ATAK → LPU5

1. ATAK sends a CoT XML event with `type="b-t-f"` over TCP/UDP.
2. `CoTListenerService` (TCP 8088 / UDP 4242 / Multicast 239.2.3.1:6969) receives
   the raw XML and calls `_cot_listener_ingest_callback(xml_string)`.
3. The callback detects `type="b-t-f"` and calls `_ingest_atak_geochat(root)`.
4. The function applies three layers of deduplication:
   - **Echo-back detection** – UIDs starting with `GeoChat.LPU5-` are LPU5-originated
     messages echoed back by the TAK server; they are skipped immediately.
   - **UID-based dedup** – recently seen event UIDs are tracked in
     `_GEOCHAT_SEEN_UIDS` (5-minute TTL) so the same CoT event arriving via
     multiple paths (TAK echo, multicast, TCP) is only processed once.
   - **Content-based dedup** – a hash of `(sender, text)` is tracked in
     `_GEOCHAT_SEEN_CONTENT` (60-second TTL) to catch the same message arriving
     with a *different* UID (e.g. TAK server re-wraps the event or ATAK resends).
5. The function extracts:
   - **sender** – `__chat/@senderCallsign`, falls back to event `uid`.
   - **text**   – `detail/remarks` element text.
6. A `ChatMessage` row is inserted into the `chat_messages` table with
   `channel='all'`.
7. A WebSocket event `{"type": "new_message", "data": {...}}` is published to the
   `chat` channel so every open browser tab updates in real time.
8. If the message is genuinely new, `_cot_listener_ingest_callback` forwards it
   **only to the TAK server** (not back to TCP clients or multicast) to avoid
   echo-back loops with the sending ATAK device.

### Outgoing: LPU5 → ATAK

When a user sends a message via the LPU5 chat UI (`POST /api/chat/message`), the
backend calls `_forward_chat_to_atak(username, text)`, which:

1. Builds a valid `b-t-f` CoT XML packet via `_build_atak_geochat_xml()`.
2. Sends it to all directly-connected TCP clients (WinTAK/ATAK windows on port 8088).
3. Forwards it to the configured TAK Server if `tak_forward_enabled=true`.
4. Publishes it on the SA Multicast group so ATAK devices on the same LAN pick it up.

---

## API Reference

### Listener Management

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/cot/listener/status` | Current status (running, ports, stats) |
| `POST` | `/api/cot/listener/start`  | Start the CoT listener (auth required) |
| `POST` | `/api/cot/listener/stop`   | Stop the CoT listener (auth required) |

### GeoChat Diagnostic & Push

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/cot/geochat/events` | List recent GeoChat messages from the `all` channel |
| `POST` | `/api/cot/geochat/push`   | Manually inject a GeoChat message |

#### `GET /api/cot/geochat/events`

Query parameters:
- `limit` (int, default `50`, max `500`): number of messages to return.

Example response:
```json
{
  "channel": "all",
  "count": 2,
  "messages": [
    {
      "id": "abc123",
      "channel_id": "all",
      "username": "ALPHA-1",
      "text": "On the way",
      "timestamp": "2025-01-01T12:00:00+00:00",
      "type": "text",
      "delivered_to": [],
      "read_by": []
    }
  ]
}
```

#### `POST /api/cot/geochat/push`

Inject a GeoChat message without a real CoT event.  Useful for integration tests
and external systems that do not speak CoT natively.

Request body (JSON):
```json
{
    "callsign": "ALPHA-1",
    "text": "Checkpoint reached",
    "uid": "ANDROID-abc123"
}
```

Fields:
- `callsign` (string, **required**): sender display name.
- `text` (string, **required**): message body.
- `uid` (string, optional): sender UID (defaults to `callsign`).

Response:
```json
{
  "status": "ok",
  "message": { ... }
}
```

The message is saved to `channel='all'`, broadcast via WebSocket, and echoed to
connected ATAK clients as a `b-t-f` CoT event.

### CoT Monitor UI

The built-in CoT data monitor is available at `/api/cot/monitor/ui` and shows
every CoT event flowing through the system in real time, including `b-t-f`
GeoChat events.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Messages not appearing in LPU5 chat | Verify `cot_listener_enabled: true` in `config.json` and that the service is running (`GET /api/cot/listener/status`). |
| Duplicate messages or relay loop | Check the logs for `"duplicate content"` or `"duplicate uid"` entries.  Content-based dedup suppresses identical sender+text pairs for 60 s; UID-based dedup covers 5 min. |
| ATAK cannot connect on TCP 8088 | Check firewall rules.  On Linux: `sudo ufw allow 8088/tcp`. |
| Multicast not working on Windows | Ensure the network adapter is on the same subnet as the ATAK device and that Windows Firewall allows UDP 6969 inbound. |
| Listener starts but no messages received | Use `GET /api/cot/monitor/ui` to verify raw CoT events arrive.  Also check that ATAK is configured to use the correct server IP and port. |
| "CoT listener service not available" error | Ensure `cot_listener_service.py` is present in the LPU5 directory and that all Python dependencies are installed (`pip install -r requirements.txt`). |

---

## Quick curl Test

Inject a test GeoChat message without ATAK:

```bash
curl -X POST http://localhost:8101/api/cot/geochat/push \
  -H "Content-Type: application/json" \
  -d '{"callsign": "TEST-1", "text": "Hello from curl", "uid": "TEST-UID-001"}'
```

List recent GeoChat messages:

```bash
curl http://localhost:8101/api/cot/geochat/events?limit=10
```

Check listener status:

```bash
curl http://localhost:8101/api/cot/listener/status
```
