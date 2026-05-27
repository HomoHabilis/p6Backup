#!/usr/bin/env python3
"""p6_tool.py — Roland P-6 guided backup and restore utility.

Serializes the P-6's 11 USB storage startup modes into an interactive
terminal session. Run on macOS (or Linux). No GUI required.

Usage:
    python p6_tool.py backup                              # prompts for folder + name
    python p6_tool.py restore --name <name> [--dir DIR]
    python p6_tool.py list [--dir DIR]
    python p6_tool.py diagnostic
    python p6_tool.py midi-status

Default backup folder:  ~/Music/P6  (macOS)   ~/p6_backups  (Linux)
Each backup contains:
    patterns/       — Export patterns mode (Hold PLAY)
    BANK_A…BANK_H/  — per-bank export modes (Hold A … Hold SAMPLING+D)
    manifest.json   — step completion record
    <name>.7z       — compressed archive created alongside the folder
"""

import argparse
import json
import os
import platform
import shutil
import sys
import time
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

# ── P-6 volume label candidates ───────────────────────────────────
P6_LABELS = {"P-6", "AIRAP6", "ROLANDP6", "P6"}

# Mount roots to scan (macOS + Linux)
SCAN_ROOTS = ["/Volumes", "/media", "/run/media", "/mnt"]

MOUNT_TIMEOUT_S = 180  # P-6 can take 1-2 min to boot into each storage mode
POLL_INTERVAL_S = 1.0

# ── Backup steps: (step_key, label, instruction) ─────────────────
BACKUP_STEPS = [
    ("patterns", "Export patterns",    "Hold [bold]PLAY[/bold] and power on"),
    ("BANK_A",   "Export Bank A",      "Hold [bold]A[/bold] and power on"),
    ("BANK_B",   "Export Bank B",      "Hold [bold]B[/bold] and power on"),
    ("BANK_C",   "Export Bank C",      "Hold [bold]C[/bold] and power on"),
    ("BANK_D",   "Export Bank D",      "Hold [bold]D[/bold] and power on"),
    ("BANK_E",   "Export Bank E",      "Hold [bold]SAMPLING + A[/bold] and power on"),
    ("BANK_F",   "Export Bank F",      "Hold [bold]SAMPLING + B[/bold] and power on"),
    ("BANK_G",   "Export Bank G",      "Hold [bold]SAMPLING + C[/bold] and power on"),
    ("BANK_H",   "Export Bank H",      "Hold [bold]SAMPLING + D[/bold] and power on"),
]

# ── Restore steps: (step_key, label, instruction, src_dirs, dst_root) ──
# src_dirs: subdirs of the backup to read from (BANK_* for samples, patterns for patterns)
# dst_root: folder name to create on the P-6 mount
RESTORE_STEPS = [
    ("samples",  "Load samples",   "Hold [bold]SAMPLING[/bold] and power on",
     ["BANK_A", "BANK_B", "BANK_C", "BANK_D", "BANK_E", "BANK_F", "BANK_G", "BANK_H"],
     "IMPORT"),
    ("patterns", "Load patterns",  "Hold [bold]REC[/bold] and power on",
     ["patterns"],
     "RESTORE"),
]


# ── Mount detection ───────────────────────────────────────────────

def _is_p6_volume(path: str, label: str) -> bool:
    """Return True if this directory looks like a P-6 USB volume."""
    if label.upper() in P6_LABELS:
        return True
    # Fallback: presence of known P-6 directories
    for marker in ("EXPORT", "BACKUP", "IMPORT", "RESTORE"):
        if os.path.isdir(os.path.join(path, marker)):
            return True
    return False


def _scan_volumes() -> list[tuple[str, str]]:
    """Return list of (mount_point, label) for all candidate P-6 volumes."""
    found = []
    seen: set[str] = set()

    # On macOS, /Volumes contains direct subdirs per volume
    # On Linux, /media/<user>/<label> or /run/media/<user>/<label>
    for root in SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                candidate = os.path.join(root, entry)
                if candidate in seen or not os.path.isdir(candidate):
                    continue
                seen.add(candidate)
                if _is_p6_volume(candidate, entry):
                    found.append((candidate, entry))
                else:
                    # One level deeper (Linux: /media/<user>/<label>)
                    try:
                        for sub in os.listdir(candidate):
                            sub_path = os.path.join(candidate, sub)
                            if sub_path in seen or not os.path.isdir(sub_path):
                                continue
                            seen.add(sub_path)
                            if _is_p6_volume(sub_path, sub):
                                found.append((sub_path, sub))
                    except PermissionError:
                        pass
        except PermissionError:
            pass
    return found


def _wait_for_mount(timeout_s: int = MOUNT_TIMEOUT_S) -> Optional[str]:
    """Poll until a P-6 volume appears; return its mount point or None."""
    deadline = time.monotonic() + timeout_s
    with console.status("[cyan]Waiting for P-6 to mount…[/cyan]") as status:
        while time.monotonic() < deadline:
            volumes = _scan_volumes()
            if volumes:
                mount_point, label = volumes[0]
                status.stop()
                console.print(f"[green]✓[/green] Detected: [bold]{mount_point}[/bold] (label: {label})")
                return mount_point
            time.sleep(POLL_INTERVAL_S)
    return None


def _wait_for_unmount(mount_point: str) -> None:
    """Wait until the given mount point disappears (device powered off)."""
    with console.status("[yellow]Waiting for P-6 to unmount…[/yellow]"):
        while os.path.ismount(mount_point) or os.path.isdir(mount_point):
            time.sleep(POLL_INTERVAL_S)
            # If label-based dir disappears, volume is gone
            if not os.path.exists(mount_point):
                break
    console.print("[dim]P-6 unmounted.[/dim]")


def _eject(mount_point: str) -> None:
    """Flush writes and eject on macOS; just sync on Linux."""
    # Sync filesystem writes first
    try:
        import subprocess
        subprocess.run(["sync"], check=False, capture_output=True)
    except Exception:
        pass

    if platform.system() == "Darwin":
        try:
            import subprocess
            subprocess.run(["diskutil", "eject", mount_point],
                           check=False, capture_output=True)
            console.print("[dim]Ejected.[/dim]")
        except Exception:
            pass

    # Wait for the volume directory to disappear before returning
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not os.path.exists(mount_point):
            break
        time.sleep(0.3)


# ── Copy helpers ──────────────────────────────────────────────────

def _copy_dir(src: str, dst: str) -> int:
    """Copy all files from src tree to dst, returning file count."""
    count = 0
    os.makedirs(dst, exist_ok=True)
    for dirpath, _, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        dst_dir = os.path.join(dst, rel)
        os.makedirs(dst_dir, exist_ok=True)
        for fname in filenames:
            shutil.copy2(os.path.join(dirpath, fname), os.path.join(dst_dir, fname))
            count += 1
    return count


def _dir_size_mb(path: str) -> float:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total / (1024 * 1024)


# ── Location + compression helpers ──────────────────────────────────────────


def _default_backup_dir() -> str:
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Music/P6")
    return os.path.expanduser("~/p6_backups")


def _prompt_backup_location() -> tuple[str, str]:
    """Interactively ask for backup folder and archive name."""
    default_dir = _default_backup_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"P6_BACKUP_{ts}"

    console.print("\n[bold]Backup location[/bold]")
    dir_input = input(f"  Folder [{default_dir}]: ").strip()
    base_dir = os.path.expanduser(dir_input) if dir_input else default_dir

    name_input = input(f"  Archive name [{default_name}]: ").strip()
    archive_name = name_input if name_input else default_name
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in archive_name).strip("_") or "P6_BACKUP"

    console.print()
    return base_dir, safe_name


def _compress_to_7z(src_dir: str, archive_path: str) -> bool:
    """Compress src_dir into a .7z archive alongside the source folder."""
    try:
        import py7zr
    except ImportError:
        console.print("[yellow]py7zr not installed — skipping compression.[/yellow]")
        console.print("  Install with: pip install py7zr")
        return False
    with console.status(f"[cyan]Compressing to {os.path.basename(archive_path)}…[/cyan]"):
        with py7zr.SevenZipFile(archive_path, "w") as z:
            z.writeall(src_dir, arcname=os.path.basename(src_dir))
    size_mb = os.path.getsize(archive_path) / (1024 * 1024)
    console.print(f"[green]✓[/green] Archive: [bold]{archive_path}[/bold] ({size_mb:.1f} MB)")
    return True


# ── Backup command ────────────────────────────────────────────────

def cmd_backup() -> None:
    console.rule("[bold]P-6 Full Backup[/bold]")

    base_dir, archive_name = _prompt_backup_location()
    os.makedirs(base_dir, exist_ok=True)
    dest = os.path.join(base_dir, archive_name)
    os.makedirs(dest, exist_ok=True)
    console.print(f"Working folder: [dim]{dest}[/dim]\n")

    manifest: dict[str, dict] = {}
    total_steps = len(BACKUP_STEPS)

    for idx, (step_key, label, instruction) in enumerate(BACKUP_STEPS, 1):
        console.rule(f"Step {idx}/{total_steps}: [bold]{label}[/bold]")
        if idx == 1:
            console.print(f"  Power [bold]OFF[/bold] the P-6, then {instruction}")
        else:
            console.print(f"  {instruction}")
        console.print()

        while True:
            mount_point = _wait_for_mount()
            if mount_point is None:
                console.print("[red]Timed out.[/red] ", end="")
                retry = input("Retry? [y/n] ").strip().lower()
                if retry != "y":
                    console.print(f"[yellow]Skipping {label}.[/yellow]")
                    manifest[step_key] = {"skipped": True}
                    break
                continue

            # Determine source folder on the device
            step_dst = os.path.join(dest, step_key)
            if step_key == "patterns":
                src_folder = os.path.join(mount_point, "BACKUP")
            else:
                bank_letter = step_key[-1]  # "A" from "BANK_A"
                src_folder = os.path.join(mount_point, "EXPORT", f"BANK_{bank_letter}")

            if not os.path.isdir(src_folder):
                console.print(f"[yellow]Expected folder not found ({os.path.relpath(src_folder, mount_point)}), copying entire mount.[/yellow]")
                src_folder = mount_point

            with console.status(f"[cyan]Copying {label}…[/cyan]"):
                count = _copy_dir(src_folder, step_dst)
            size_mb = _dir_size_mb(step_dst)
            console.print(f"[green]✓[/green] Copied {count} files ({size_mb:.1f} MB) → {step_key}/")

            manifest[step_key] = {
                "files": count,
                "size_mb": round(size_mb, 2),
                "timestamp": datetime.now().isoformat(),
            }

            _eject(mount_point)

            if idx < total_steps:
                console.print()
                input("Power OFF the P-6, then press Enter for the next step… ")
            break

    # Write manifest
    ts_final = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(dest, "manifest.json"), "w") as f:
        json.dump({"name": archive_name, "timestamp": ts_final, "steps": manifest}, f, indent=2)

    completed = sum(1 for v in manifest.values() if not v.get("skipped"))
    total_mb = _dir_size_mb(dest)
    console.rule()
    console.print(f"\n[bold green]All steps done![/bold green]  {completed}/{total_steps} steps, {total_mb:.1f} MB raw\n")

    # Compress to 7z (folder is kept alongside for restore convenience)
    archive_path = os.path.join(base_dir, f"{archive_name}.7z")
    _compress_to_7z(dest, archive_path)
    console.print()


# ── Restore command ───────────────────────────────────────────────

def _find_backup(name: str, backup_dir: str = "") -> Optional[str]:
    """Find a backup directory by name or prefix."""
    search_dir = backup_dir or _default_backup_dir()
    if not os.path.isdir(search_dir):
        return None
    matches = []
    for entry in os.listdir(search_dir):
        path = os.path.join(search_dir, entry)
        if not os.path.isdir(path):
            continue
        manifest_path = os.path.join(path, "manifest.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path) as f:
                    meta = json.load(f)
                if meta.get("name") == name:
                    matches.append(path)
                    continue
            except Exception:
                pass
        if entry == name or entry.startswith(name + "_"):
            matches.append(path)
    if not matches:
        return None
    return sorted(matches)[-1]


def _restore_samples(mount_point: str, backup_path: str) -> int:
    """Write all bank backups into IMPORT/BANK_X/PAD_N/ on the mounted P-6."""
    import_root = os.path.join(mount_point, "IMPORT")
    count = 0
    for bank_letter in "ABCDEFGH":
        bank_src = os.path.join(backup_path, f"BANK_{bank_letter}")
        if not os.path.isdir(bank_src):
            continue
        bank_dst = os.path.join(import_root, f"BANK_{bank_letter}")
        # bank_src structure: PAD_1/P6_X-N.WAV + .PRM
        for pad_dir in sorted(os.listdir(bank_src)):
            pad_src = os.path.join(bank_src, pad_dir)
            if not os.path.isdir(pad_src):
                continue
            pad_dst = os.path.join(bank_dst, pad_dir)
            os.makedirs(pad_dst, exist_ok=True)
            for fname in os.listdir(pad_src):
                shutil.copy2(os.path.join(pad_src, fname), os.path.join(pad_dst, fname))
                count += 1
    return count


def _restore_patterns(mount_point: str, backup_path: str) -> int:
    """Write pattern files back to RESTORE/ on the mounted P-6."""
    patterns_src = os.path.join(backup_path, "patterns")
    if not os.path.isdir(patterns_src):
        return 0
    # Patterns must be imported from RESTORE on the device, not BACKUP
    patterns_dst = os.path.join(mount_point, "RESTORE")
    os.makedirs(patterns_dst, exist_ok=True)
    count = 0
    for fname in os.listdir(patterns_src):
        shutil.copy2(os.path.join(patterns_src, fname), os.path.join(patterns_dst, fname))
        count += 1
    return count


def cmd_restore(name: str, backup_dir: str = "") -> None:
    backup_path = _find_backup(name, backup_dir)
    if backup_path is None:
        search_dir = backup_dir or _default_backup_dir()
        console.print(f"[red]No backup found for '{name}' in {search_dir}.[/red]")
        console.print("Run `list` (or `list --dir PATH`) to see available backups.")
        sys.exit(1)

    console.rule(f"[bold]P-6 Restore — {name}[/bold]")
    console.print(f"Restoring from: [dim]{backup_path}[/dim]\n")

    steps = [
        ("samples",  "Load samples",   "Hold [bold]SAMPLING[/bold] and power on", _restore_samples),
        ("patterns", "Load patterns",  "Hold [bold]REC[/bold] and power on",      _restore_patterns),
    ]

    for idx, (step_key, label, instruction, restore_fn) in enumerate(steps, 1):
        console.rule(f"Step {idx}/{len(steps)}: [bold]{label}[/bold]")
        console.print(f"  1. Power [bold]OFF[/bold] the P-6")
        console.print(f"  2. {instruction}")
        console.print()

        while True:
            mount_point = _wait_for_mount()
            if mount_point is None:
                retry = console.input("[red]Timed out.[/red] Retry? [y/n] ").strip().lower()
                if retry != "y":
                    console.print(f"[yellow]Skipping {label}.[/yellow]")
                    break
                continue

            with console.status(f"[cyan]Writing {label}…[/cyan]"):
                count = restore_fn(mount_point, backup_path)
                # Flush writes
                try:
                    import subprocess
                    subprocess.run(["sync"], check=False, capture_output=True)
                except Exception:
                    pass

            console.print(f"[green]✓[/green] Wrote {count} files")
            _eject(mount_point)

            if idx < len(steps):
                console.print()
                console.input("[dim]Power OFF the P-6, then press Enter for next step…[/dim]")
            break

    console.rule()
    console.print("\n[bold green]Restore complete![/bold green]  Power on the P-6 normally.\n")


# ── List command ──────────────────────────────────────────────────

def cmd_list(backup_dir: str = "") -> None:
    search_dir = backup_dir or _default_backup_dir()
    if not os.path.isdir(search_dir):
        console.print(f"[dim]No backups found in {search_dir}[/dim]")
        return

    table = Table(title=f"P-6 Backups — {search_dir}", box=box.SIMPLE_HEAVY)
    table.add_column("Name", style="bold")
    table.add_column("Date", style="dim")
    table.add_column("Steps", justify="center")
    table.add_column("Size", justify="right")
    table.add_column(".7z", justify="center")
    table.add_column("Folder", style="dim")

    step_keys = [s[0] for s in BACKUP_STEPS]

    for entry in sorted(os.listdir(search_dir), reverse=True):
        path = os.path.join(search_dir, entry)
        if not os.path.isdir(path):
            continue
        manifest_path = os.path.join(path, "manifest.json")
        name = entry
        date_str = ""
        completed = 0
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path) as f:
                    meta = json.load(f)
                name = meta.get("name", entry)
                ts = meta.get("timestamp", "")
                if ts:
                    date_str = ts.replace("T", " ")[:16]
                steps_done = meta.get("steps", {})
                completed = sum(1 for k in step_keys if steps_done.get(k) and not steps_done[k].get("skipped"))
            except Exception:
                pass

        size_mb = _dir_size_mb(path)
        steps_str = f"{completed}/{len(step_keys)}"
        color = "green" if completed == len(step_keys) else "yellow" if completed > 0 else "red"
        archive_path = os.path.join(search_dir, f"{entry}.7z")
        archive_str = "[green]✓[/green]" if os.path.exists(archive_path) else "[dim]—[/dim]"
        table.add_row(name, date_str, f"[{color}]{steps_str}[/{color}]",
                      f"{size_mb:.1f} MB", archive_str, path)

    console.print(table)


# ── Diagnostic command ────────────────────────────────────────────

def cmd_diagnostic() -> None:
    console.print("[bold]Scanning for P-6 volumes…[/bold]")
    volumes = _scan_volumes()
    if not volumes:
        console.print("[yellow]No P-6 volume detected.[/yellow]")
        console.print("Connect the P-6 in any USB storage mode and run again.")
        return

    for mount_point, label in volumes:
        console.print(f"\n[bold green]Found:[/bold green] {mount_point}  (label: {label})\n")
        for dirpath, dirnames, filenames in os.walk(mount_point):
            dirnames.sort()
            rel = os.path.relpath(dirpath, mount_point)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            indent = "  " * depth
            folder_name = os.path.basename(dirpath) if rel != "." else mount_point
            console.print(f"{indent}[cyan]{folder_name}/[/cyan]")
            for fname in sorted(filenames):
                fpath = os.path.join(dirpath, fname)
                try:
                    size = os.path.getsize(fpath)
                    size_str = f"{size / 1024:.1f} KB"
                except OSError:
                    size_str = "?"
                console.print(f"{indent}  {fname}  [dim]{size_str}[/dim]")


# ── MIDI status command ───────────────────────────────────────────

def cmd_midi_status() -> None:
    try:
        import rtmidi
    except ImportError:
        console.print("[red]python-rtmidi not installed.[/red] Run: pip install python-rtmidi")
        sys.exit(1)

    mid_in = rtmidi.MidiIn()
    mid_out = rtmidi.MidiOut()

    in_ports = mid_in.get_ports()
    out_ports = mid_out.get_ports()

    console.print("\n[bold]MIDI Ports:[/bold]")
    p6_in = p6_out = None
    for i, name in enumerate(in_ports):
        marker = ""
        if any(h in name.upper() for h in ("P-6", "AIRA", "ROLAND")):
            marker = "  [green]← P-6[/green]"
            p6_in = i
        console.print(f"  IN  {i}: {name}{marker}")
    for i, name in enumerate(out_ports):
        marker = ""
        if any(h in name.upper() for h in ("P-6", "AIRA", "ROLAND")):
            marker = "  [green]← P-6[/green]"
            p6_out = i
        console.print(f"  OUT {i}: {name}{marker}")

    if p6_in is None or p6_out is None:
        console.print("\n[yellow]P-6 MIDI port not found.[/yellow] Power on normally (no button held).")
        return

    mid_in.open_port(p6_in)
    mid_out.open_port(p6_out)
    mid_in.ignore_types(sysex=False)

    # Send Universal SysEx identity request
    mid_out.send_message([0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7])

    received = []
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        msg = mid_in.get_message()
        if msg:
            received.append(msg[0])
        time.sleep(0.01)

    mid_in.close_port()
    mid_out.close_port()

    if received:
        console.print("\n[bold]Identity reply:[/bold]")
        for msg in received:
            console.print("  " + " ".join(f"{b:02X}" for b in msg))
    else:
        console.print("\n[yellow]No identity reply received.[/yellow]")


# ── Entry point ───────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="p6_tool",
        description="Roland P-6 guided backup and restore utility",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="Guided 9-step full backup (prompts for folder + name)")

    p_restore = sub.add_parser("restore", help="Guided 2-step restore")
    p_restore.add_argument("--name", required=True, help="Backup name to restore from")
    p_restore.add_argument("--dir", default="", metavar="FOLDER",
                           help="Backup folder (default: ~/Music/P6 on macOS, ~/p6_backups on Linux)")

    p_list = sub.add_parser("list", help="List available backups")
    p_list.add_argument("--dir", default="", metavar="FOLDER",
                        help="Backup folder (default: ~/Music/P6 on macOS, ~/p6_backups on Linux)")

    sub.add_parser("diagnostic", help="Dump directory tree of mounted P-6 volume")
    sub.add_parser("midi-status", help="Show MIDI ports and query P-6 identity")

    args = parser.parse_args()

    if args.command == "backup":
        cmd_backup()
    elif args.command == "restore":
        cmd_restore(args.name, args.dir)
    elif args.command == "list":
        cmd_list(args.dir)
    elif args.command == "diagnostic":
        cmd_diagnostic()
    elif args.command == "midi-status":
        cmd_midi_status()


if __name__ == "__main__":
    main()
