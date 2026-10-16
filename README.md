# Mass-vidfix

CLI tool that finds broken or incomplete videos on a drive, trims off the unplayable tails, and optionally compresses the results with x264.

It works by probing with `ffprobe`, optionally decoding with `ffmpeg` to locate the first hard error, then stream-copying the playable prefix (or re-encoding for compression). Originals are moved aside rather than deleted by default.

See [docs/USAGE.md](docs/USAGE.md) for a deeper look at the manifest format, verification steps, and performance tips.

## Requirements

- Python 3.8+
- `ffmpeg` and `ffprobe` on PATH
- The `file` command (used only as a fallback for extension-less video files)

Install on Debian/Ubuntu:

```bash
sudo apt install ffmpeg file
```

## Installation

Clone and run directly (no install step):

```bash
git clone https://github.com/Spharax/Mass-vidfix.git
cd Mass-vidfix
python3 vidfix.py --help
```

Make it executable if you want:

```bash
chmod +x vidfix.py
./vidfix.py scan /path/to/drive
```

## Commands

### scan

Walks a drive, classifies videos, and moves the broken ones into `_REVIEW2/` (or a folder you specify). Nothing is deleted.

```bash
python3 vidfix.py scan DRIVE [--deep] [--review PATH]
```

Buckets written under the review folder:

| Folder                  | Meaning                                      |
|-------------------------|----------------------------------------------|
| `1_playable_90plus`     | Plays ≥90 % of its declared duration         |
| `2_playable_75plus`     | Plays ≥75 %                                 |
| `3_playable_50plus`     | Plays ≥50 %                                 |
| `4_playable_under50`    | Plays < 50 %                                 |
| `5_completely_broken`   | Cannot be opened, zero duration, or partial |

A `manifest.jsonl` is written next to the buckets so the later `fix` step knows what to restore and where.

`--deep` forces a full decode of every file (slow) so mid-file corruption is detected instead of only trailing damage.

### fix

Reads the manifest, trims each partly-playable video to its last good second, and writes the result back under the original path/name. The trimmed source is moved to `_originals_replaced/` unless you pass `--delete-originals`.

```bash
python3 vidfix.py fix DRIVE [--delete-originals] [--review PATH]
```

Only files that were not classified as completely broken are processed. The last second is always dropped to avoid half-written frames.

### compress

Re-encodes every eligible video (`.mp4`, `.mkv`, `.mov`, `.m4v`) with `libx264` (ultrafast preset) and replaces the original only if the result is verified, shorter, and at least 5 % smaller. HEVC/AV1/VP9 are skipped because re-encoding them to x264 usually grows the file.

```bash
python3 vidfix.py compress DRIVE [--crf 28] [--dry-run] [--review PATH]
```

`--crf` defaults to 28 (higher = smaller). Already-processed paths are recorded in `compressed.txt` so reruns are cheap.

## Typical workflow

```bash
# 1. Sort broken videos into the review folder (safe, moves only)
python3 vidfix.py scan /mnt/media --deep

# 2. Trim the playable ones and put them back
python3 vidfix.py fix /mnt/media

# 3. (Optional) compress everything that is still large
python3 vidfix.py compress /mnt/media --crf 26
```

You can interrupt any command with Ctrl-C; already-moved or already-fixed files stay that way and a later run continues from where it left off.

## How classification works

1. Probe with `ffprobe` for codec and declared duration.
2. Scan packet timestamps to find the last video packet that actually exists.
3. With `--deep`, also decode the file and stop at the first hard error.
4. The playable ratio decides the bucket. Files that play ≥98 % or are missing ≤2 s are treated as OK and left in place.

Partial-download extensions (`.part`, `.crdownload`, `.ytdl`, etc.) are always marked completely broken.

## Limitations & notes

- Only the first video stream and optional first audio stream are kept.
- Stream-copy trim (`-c copy`) can leave a few frames of garbage if the keyframe interval is large; the one-second safety margin helps.
- Compression always re-encodes video; audio is copied when possible, otherwise re-encoded to AAC.
- The tool is deliberately conservative: it never deletes an original unless you ask, and it verifies the output before replacing.
- Large drives: `--deep` is I/O- and CPU-heavy. Run it overnight or on a subset.

## License

No license file is present. Treat as all-rights-reserved until one is added.
