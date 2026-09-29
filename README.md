# makeid-l1c-driver

Driver for the **MakeID L1-C** Bluetooth label printer: an IPP Everywhere server you can
print to from Inkscape or any other program, plus a command-line tool.

The vendor only offers a phone app.

## Status

Tested on:

- L1-C, firmware `V1.0_250317.2`
- `LC-16W` continuous tape, 16 mm
- Ubuntu with BlueZ 5.85 and CUPS 2.4
- Windows 11 with Python 3.13, bleak 3.0, the Microsoft IPP Class Driver and Inkscape 1.4

macOS would use the same bleak transport as Windows; it is untested.

Other L1 models (L1S, L1G, L1E) use the same code path in the app. They are untested.
Only continuous tape is supported; die-cut labels are not.

## Requirements

Linux:

```sh
sudo apt install bluez cups python3-pil   # python3-pil only for l1c-print
```

Windows: Python 3 from python.org (tested with 3.13). `windows\install.ps1` installs bleak and
Pillow into `.venv`.

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

The printer is found by its Bluetooth name (`L1â€¦`). To give the address explicitly, run
`linux/install.sh 58:8C:81:xx:xx:xx`. Pairing is not needed.

To remove it, run `linux/uninstall.sh`.

On Windows, switch the printer on and run in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File windows\install.ps1
```

This does three things:

- It creates `.venv` with bleak and Pillow.
- It starts `l1c-ippd` at logon on `http://localhost:8631/ipp/print`: a shortcut in the
  Startup folder, no console window, log in `%LOCALAPPDATA%\l1c-ippd\l1c-ippd.log`.
- It adds the printer **L1-C** with the Microsoft IPP Class Driver and sets it to Grayscale.
  Adding needs an elevated PowerShell. Without one, add it by hand: Settings â†’ Bluetooth &
  devices â†’ Printers & scanners â†’ Add device â†’ Add manually â†’ Add a printer using an IP address
  or hostname â†’ Device type **IPP Device** â†’ `http://localhost:8631/ipp/print`, then run the
  script again. ("Select a shared printer by name" does not work: it sends PDF on Letter paper.)

In the print dialog, leave everything as it is and keep portrait. Colour mode and borderless
printing make no difference.

To remove it, run `windows\uninstall.ps1`.

## Design and print

1. Open `template.svg` in Inkscape.
2. Design the label in mm. The page is the whole label: 16 mm high, as long as you like
   (Document Properties).
3. Print with File â†’ Print â†’ **L1-C**.

The guides in the template show:

- the printable band from 2 to 14 mm (the head is 12 mm wide)
- 4 mm at each end, which the printer feeds blank itself. If you change the page length,
  move the right-hand guide with it.

Drawn within the guides, the label is exactly the page. Nothing is ever cut off: drawn beyond
a guide, the label gets longer at that end. Blank tape after the last dot is not printed (on
Windows the page length does not reach the printer), so the label always ends 4 mm after it.

With the LED side towards you, the tape comes out to the left and reads normally.

Other programs print the same way; mind their page margins:

- left: 0 to 4 mm. The label starts 4 mm before the content; any margin beyond 4 mm is printed
  as blank tape.
- right: any, blank tape at the end is not printed.
- top and bottom: 2 mm. The head reaches only the middle 12 mm of the 16 mm page.

Lines thinner than a printer dot (0.125 mm) may not print on Windows: a 0.1 mm frame from Inkscape
did not arrive at all, 0.2 mm prints.

Several copies or pages of one job come out 8 mm apart with a dashed cut line between them.

The document stays vector until the client rasterises it at 203 dpi. Where the print dialog
offers print quality (CUPS), it sets the darkness: Draft = light, Normal, High = dark.

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

- `l1c.py`: BLE connection, protocol, raster decoding and LZO compression. On Linux it uses
  a raw L2CAP/ATT socket because BlueZ tries to reach this printer over BR/EDR and fails; on
  Windows and macOS it uses bleak. The LZO1X-1 compressor is a Python port of liblzo2's
  `lzo1x_1_compress`. `tests/test_lzo.py` checks it byte for byte against liblzo2 (from the
  `liblzo2-2` package or `pip install python-lzo`).
- `l1c-ippd`: IPP Everywhere server. It takes PWG raster at 203 dpi, `sgray_8` (and `black_1`).
  Grey below 67 % brightness prints black, so that anti-aliased hairlines do not vanish.
  Each page is one label of up to 1000 mm on 16 mm tape.
- `l1c-print`: command-line tool that prints without the server.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

This needs no printer. It covers the protocol, the raster decoding, the IPP server with a
fake printer, and the LZO compressor byte for byte against liblzo2: directly when `liblzo2-2`
or `python-lzo` is installed, otherwise against a stored digest of liblzo2's output.

With the printer switched on, `L1C_PRINTER=1` also runs `tests/test_printer.py`. It prints five
short labels, 10 s apart: directly, through `l1c-ippd`, with `l1c-print`, and two copies of one
job with the cut line between them. Cut the tape after the last one.

```sh
L1C_PRINTER=1 python3 -m unittest -v tests.test_printer
```

On Windows use `.venv\Scripts\python.exe` instead of `python3`; in PowerShell set the variable
with `$env:L1C_PRINTER=1`.

## License

MIT
