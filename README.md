# p6_tool — Roland P-6 Backup & Restore

A guided terminal utility for backing up and restoring the **Roland P-6** via its USB storage modes. No GUI, no desktop required. Works on macOS and Linux.

---

## At a glance

```bash
python p6_tool.py backup                              # prompts for folder + name
python p6_tool.py restore --name <name> [--dir DIR]
python p6_tool.py list [--dir DIR]
python p6_tool.py diagnostic
python p6_tool.py midi-status
```

**Default backup folder:**
- macOS: `~/Music/P6`
- Linux: `~/p6_backups`

**Each backup contains:**
- `patterns/` — Export patterns mode (Hold PLAY)
- `BANK_A…BANK_H/` — per-bank export modes (Hold A … Hold SAMPLING+D)
- `manifest.json` — step completion record
- `<name>.7z` — compressed archive created alongside the folder

---

## What it does

The P-6 exposes its content as a USB mass-storage device across 9 startup modes (one per bank export + pattern export). `p6_tool` walks you through each mode step by step, copies the files to a local folder, then compresses everything into a `.7z` archive.

Restore is the reverse: two modes (sample import + pattern import), guided the same way.

---

## Backup folder structure

Example backup created at `~/Music/P6/P6_BACKUP_20260527_143000/`:

```
P6_BACKUP_20260527_143000/
├── patterns/          (Hold PLAY on power-on)
├── BANK_A/            (Hold A on power-on)
├── BANK_B/            (Hold B on power-on)
├── BANK_C/            (Hold C on power-on)
├── BANK_D/            (Hold D on power-on)
├── BANK_E/            (Hold SAMPLING + A on power-on)
├── BANK_F/            (Hold SAMPLING + B on power-on)
├── BANK_G/            (Hold SAMPLING + C on power-on)
├── BANK_H/            (Hold SAMPLING + D on power-on)
├── manifest.json      (completion record with file counts and timestamps)
└── ../P6_BACKUP_20260527_143000.7z  (compressed archive)
```

Each bank folder contains the P-6's exported samples. The `manifest.json` tracks which steps completed successfully. The `.7z` archive alongside the folder is optional but recommended for archival.

---

## Install

```bash
pip install -r requirements.txt
```

Requirements: `rich`, `py7zr`, `python-rtmidi`, `soundfile`, `numpy`  
Python 3.11+

---

## Usage

### Backup

```bash
python p6_tool.py backup
```

The tool prompts for a folder and archive name, then walks through all 9 export modes one by one. For each step:
1. Power off the P-6
2. Hold the indicated button and power on
3. Wait for it to mount (~30-90 s)
4. The tool copies the files automatically, then tells you to power off for the next step

### Restore

```bash
python p6_tool.py restore --name P6_BACKUP_20260527_143000
```

Two steps:
1. **Load samples** -- Hold SAMPLING and power on -> tool writes all bank folders to `IMPORT/`
2. **Load patterns** -- Hold REC and power on -> tool writes pattern files to `BACKUP/`

Use `--dir /path/to/folder` if your backups are not in the default location.

### List backups

```bash
python p6_tool.py list
python p6_tool.py list --dir /Volumes/ExternalDrive/P6
```

Shows a table with name, date, completed steps, raw size, and whether a `.7z` archive exists.

### Diagnostic

```bash
python p6_tool.py diagnostic
```

Connects to the currently-mounted P-6 (any mode) and prints the full directory tree with file sizes. Useful for verifying the volume structure before a backup or after a restore.

### MIDI status

```bash
python p6_tool.py midi-status
```

Lists all MIDI ports, highlights P-6 ports, and sends a Universal SysEx identity request to confirm the device responds.

---

## Notes

- The tool auto-detects P-6 volumes by label (`P-6`, `AIRAP6`, `ROLANDP6`, `P6`) and by the presence of `EXPORT`, `BACKUP`, or `IMPORT` directories. It scans `/Volumes`, `/media`, `/run/media`, and `/mnt`.
- After copying, it calls `sync` + `diskutil eject` (macOS) before asking you to power off. Always wait for the "Ejected" message before unplugging.
- The raw folder is kept alongside the `.7z` so you can restore directly without decompressing.
- `py7zr` is optional -- if it is missing, the folder is kept but no archive is created.

---

## License

MIT
