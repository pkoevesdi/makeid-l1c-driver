# L1-C Bluetooth LE protocol

Worked out from the vendor app *MakeID* (`com.wewin.house_print`) and checked against an
L1-C with firmware `V1.0_250317.2` and `LC-16W` continuous tape.

## Transport

- The printer is an ESP32 with the ESP-IDF `ble_spp_server` layout: service `0xABF0`.
  - `0xABF1` (handle `0x2A`): host → printer, write without response.
  - `0xABF2` (handle `0x2E`): printer → host, notify; CCCD at `0x2F`, write `01 00`.
  - `0xABF3` is not used. Service `1d14d6ee-…` is Espressif OTA.
- It advertises with flags `0x02` from a public address and no "BR/EDR not supported" bit,
  so BlueZ' `Device1.Connect()` pages it over BR/EDR and times out. `l1c.py` opens an
  L2CAP socket on the ATT channel (CID 4, LE public) instead and speaks ATT directly.
- MTU 517 is accepted. A command is split into chunks of MTU − 3 bytes without further
  framing; the printer reassembles by the length field. One command at a time, each
  answered by one notification sequence.
- Replies starting with `##` carry a 4-byte prefix to drop. `**…` is an out-of-band marker.

## Frame

```
66 | LEN (u16 LE, whole frame) | CMD | payload | CS
CS = (-sum(all preceding bytes)) & 0xFF
```

## Commands

| frame | meaning |
|---|---|
| `66 06 00 10 00 84` | status query |
| `66 06 00 10 01 83` | pause |
| `66 06 00 10 02 82` | restore (sent once before a print job) |
| `66 06 00 10 03 81` | cancel |
| `66 05 00 50 45` | firmware: NUL-separated hardware version, firmware version, name |
| `66 LEN 1B …` | print block, see below |

No handshake or authentication is needed after connecting.

## Status reply (CMD `0x10`)

L1-C firmware `V1.0_250317.2` answers with 37 bytes.

| offset | content |
|---|---|
| 4 | bit 7 busy/wait, bit 6 resend, bits 0–5 status code |
| 5 | bit 7 charging, bits 0–6 battery % |
| 6 | bits 0–2 head DPI (0 = 203) |
| 8–9 | last label length in dots, LE |
| 10–14 | printer type, ASCII (`L1C`) |
| 16–17 | tape remaining, LE (unit unknown) |
| 18–19 | tape total, LE (4000 for `LC-16W`) |
| 20–33 | tape type, ASCII (`LC-16W`) |
| 35 | bits 5–6: printer-side request, 1 = pause, 3 = cancel |
| 36–37 | protocol version major/minor, **absent on this firmware** |

Status codes: 0 ready, 1/4/13 no tape, 3 tape used up, 5 tape chip not detected,
6 cover open, 8 overheated, 11 busy, 15 tape error, 16 cancelled on the printer,
17 reprint request, 23 chip error (treated as ready by the app).

## Image data

Without a protocol version in the status reply the app uses its "L1" path
(`CreateL1DotArray`):

- Head: 96 dots (12 mm) at 203 dpi, centred on the 16 mm tape.
- One print line = 12 bytes, MSB first, 1 = black; then **every byte pair swapped**
  (`B1 B0 B3 B2 …`).
- Lines are grouped into blocks of 85 lines, each block compressed with LZO1X-1
  (raw stream, no header).
- The printer adds about 4 mm of blank tape before and after the image.

## Print block (CMD `0x1B`)

| offset | value |
|---|---|
| 4 | bits 0–4 darkness (app: 10 / 15 / 20); bits 5–7 media: `000` gap label, `001` continuous |
| 5 | cut type, app always sends 3 |
| 6–7 | labels in job, LE |
| 8–9 | current label, 1-based, LE |
| 10 | `01` (unknown) |
| 11–12 | label length in lines, LE |
| 13–14 | lines in this block, LE |
| 15 | blocks still to follow (0 on the last block) |
| 16 | `00` (unknown) |
| 17… | LZO data |

Each block is answered with a status reply. Resend bit set: send the same block again.
Busy bit set: poll status until clear, then send the next block.

## Job

1. `66 06 00 10 02 82`
2. For each label, all its `0x1B` blocks as above.
3. Poll status until not busy.
