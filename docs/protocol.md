# RT4K serial protocol, as measured

RetroTINK has not published a spec for the extended interface; its firmware
changelog still says "example software and documentation pending". This page
covers only what `rt4k-serial.py` uses, and every line was measured on one
RT4K Pro, firmware 1.75.0–1.86.0.

## Transport

- USB: FTDI FT232R, `0403:6001`. **2,000,000 baud** 8N1 by default since
  1.75.0 (115200 before that).
- Commands end in `\r`. Replies are text lines prefixed `[COM] `. Lines
  prefixed `[MCU] ` also appear.
- Hold **one** descriptor open for the whole session, and don't toggle DTR
  between commands.
- Unknown input is echoed back as `Bad Command: <text>`. That echo is the best
  link diagnostic there is: if it comes back garbled, the baud rate is wrong
  (see [zaparoo.md](zaparoo.md)).

## Text commands

| Sent | Reply |
|---|---|
| `ver` | `RT4KPRO, FW Version: <v>`, then `Build tag: <tag>` |
| `baud` | `baud 2000000 (ladder: 115200 500000 1000000 2000000)` |
| `baud <n>` | `baud switching to <n> -- send 'baud ok' within 5000 ms`, then `baud ok` → `baud confirmed <n>`. If it is not confirmed, the firmware reverts |
| `remote <key>` | `Serial Remote: <key>`. The keys are the remote's buttons: `menu`, `up`, `down`, `left`, `right`, `ok`, `back`, `prof1`–`prof12`, `res4k`, … |
| `pwr on` | `Bad Command: pwr on` when the unit is already awake |
| `SVS NEW INPUT=<n>` | *(silent)*. Loads `profile/SVS/S<n>_*.rt4` when Auto Load SVS is on |
| `prof get` | `prof loaded=1 dir=/profile file=<path relative to /profile>` |
| `prof load <path>` | `prof load ok`. The path is relative to `/profile`, may contain spaces, and is case-insensitive |
| `ls [<dir>]` | one `ent t=F\|D sz=<bytes> mt=<unix> nm=<name>` per entry, then `ls end <n>`. **Stops at 512 entries** (`ls truncated at 512`) |
| `stat <path>` | `stat t=F sz=<n> mt=… nm=<path>` or `stat err=2 NOSUCH` |
| `rm <path>` | `rm ok` |
| `input` / `output` | `input=0 HDMI ic=2 model=0` / `output sel=0 live=0 model=0` |
| `banner`, `osd2 state` | the banner image path, and the secondary OSD plane's state |

`get` wants `-- <path>`. `ls`, `stat` and `rm` take the path bare.

Files written by `put` get an mtime of 2020-01-01, because the unit has no
clock. Every profile is 23,004 bytes. So neither mtime nor size can tell two
profiles apart; only the content hash can.

## Binary transfers: RTL1 frames

`osd`, `osd2`, `font`, `get` and `put` switch to framed binary after a text
`… ready … nonce=0x<n>` line:

```
A5 5A | nonce lo hi | len lo hi | type | seq | payload (≤ 2048) | crc16 lo hi
```

- The CRC is CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over the bytes from
  the nonce through the payload. It is `binascii.crc_hqx`.
- Frame types: 1 command, 2 response, 3 data, 4 ack, 5 nak, 6 abort, 7 ping.
  NAK reasons: 1 CRC, 2 NONCE, 3 SEQ, 4 LEN, 5 BUSY, 6 STATE.
- **Download** (`get -- <path>`, `osd`): data frames carry the bytes, and a
  type-2 response frame carries the SHA-256 of the whole transfer.
- **Upload** (`put <size> <sha256hex> <path>` → `put ready nonce=…`): send
  data frames with seq 0, 1, 2 …, then **an empty data frame** with the next
  seq. The firmware checks the size and hash from the command line and answers
  `put done`. Ending with a response frame or a ping instead leaves the parser
  in binary mode, deaf to text, for about a minute, until `put timeout`. Wait;
  don't power-cycle.
- `osd` returns 2,048 character cells then 2,048 colour bytes (32 rows ×
  stride 64, 40 columns visible). The OSD is text, not a bitmap.

## Why transfers drop to 1 Mbaud

At 2 Mbaud the FT232R inside the RT4K overruns its 256-byte receive FIFO on
every 2 KB frame. This was measured with `TIOCGICOUNT`. The RT4K doesn't honour
RTS/CTS, and the DE10-Nano's USB host can't drain the FIFO fast enough. At
1 Mbaud there are zero overruns. `rt4k-serial.py` negotiates `baud 1000000` for
each binary transfer and switches back afterwards. Text commands are fine at
2 Mbaud.

Generate frames lazily. The firmware waits only a few seconds after
`put ready` for the first frame. Building every frame up front with a
pure-Python CRC took over 4 s for an 800 KB file on the DE10-Nano. That missed
the window, and it looked exactly like a file-size cap.
