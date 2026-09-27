# makeid-l1c-driver

Driver for the **MakeID L1-C** Bluetooth label printer: an IPP Everywhere server you can
print to from Inkscape or any other program, plus a command-line tool.

The vendor only offers a phone app.

## Status

Tested on:

- L1-C, firmware `V1.0_250317.2`
- `LC-16W` continuous tape, 16 mm
- Ubuntu with BlueZ 5.85 and CUPS 2.4

The Bluetooth transport in `l1c.py` uses a raw L2CAP socket and therefore only runs on Linux.
Windows and macOS support needs a second transport, for example one based on bleak.

Other L1 models (L1S, L1G, L1E) use the same code path in the app. They are untested.
Only continuous tape is supported; die-cut labels are not.

## Requirements

```sh
sudo apt install liblzo2-2 bluez cups python3-pil   # python3-pil only for l1c-print
```

Inkscape is optional. It is only needed for `l1c-print file.svg`; the native package and the
Flatpak both work.

## Install

`l1c-ippd` is a small IPP Everywhere server. Any system that can print to an IPP Everywhere
printer can use it; the server talks to the printer over Bluetooth LE.

On Linux, switch the printer on and run:

```sh
linux/install.sh
```

This does two things:

- It sets up `l1c-ippd` as a systemd user service on `ipp://localhost:8631/ipp/print`.
- It adds the CUPS queue **L1-C** with `lpadmin -m everywhere`. You need to be in the
  `lpadmin` group; otherwise run the script with `sudo`.

The printer is found by its Bluetooth name (`L1…`). To give the address explicitly, run
`linux/install.sh 58:8C:81:xx:xx:xx`. Pairing is not needed.

To remove it, run `linux/uninstall.sh`.

Windows and macOS need a Bluetooth transport that is not written yet; see
[Status](#status).

## Design and print

1. Open `template.svg` in Inkscape.
2. Design the label in mm. The page is the whole label: 16 mm high, as long as you like
   (Document Properties).
3. Print with File → Print → **L1-C**.

The guides in the template show:

- the printable band from 2 to 14 mm (the head is 12 mm wide)
- 4 mm at each end that is cut off. The printer feeds this blank tape itself.

If you change the page length, move the right-hand guide with it.

The document stays vector until the client rasterises it at 203 dpi. Print quality in the
print dialog sets the darkness: Draft = light, Normal, High = dark.

## Command line

```sh
./l1c-print label.svg            # SVG, 16 mm high
./l1c-print label.png            # PNG, 128 px (16 mm) or 96 px (12 mm) high
./l1c-print -t "Hello"           # plain text
./l1c-print label.svg -p out.png # preview of the printed dots, no printing
./l1c-print --status             # battery, tape, firmware
```

The printer address comes from, in this order: `-a`, `$L1C_ADDR`, or a Bluetooth search.

## How it works

- `l1c.py`: BLE connection, protocol, raster decoding and LZO compression. It uses a raw
  L2CAP/ATT socket because BlueZ tries to reach this printer over BR/EDR and fails.
- `l1c-ippd`: IPP Everywhere server. It takes PWG raster (`black_1` or `sgray_8`, 203 dpi).
  Each page is one label, with a length of 10–1000 mm on 16 mm tape.
- `l1c-print`: command-line tool that prints without the server.

## License

MIT
