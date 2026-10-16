# Usage Guide

## Environment

The script expects `ffmpeg`, `ffprobe`, and `file` on `PATH`. On a headless server you can use a static ffmpeg build if the distro package is too old.

Python 3.8+ is enough; the only standard-library modules used are `argparse`, `json`, `os`, `pathlib`, `re`, `shutil`, and `subprocess`.

## Scan details

- Files smaller than 100 KiB with unknown extensions are ignored unless `file` reports `video/*`.
- The review folder itself and any `lost+found` directory are skipped.
- Relative paths are cleaned of any previous `_REVIEW/...` prefix so a second scan does not nest folders.
- The manifest is append-only JSON Lines. Each line contains:

  ```json
  {
    "bucket": "2_playable_75plus",
    "current": "/drive/_REVIEW2/2_playable_75plus/movies/foo.mkv",
    "original": "/drive/movies/foo.mkv",
    "playable": 1234.5,
    "duration": 1800.0,
    "note": "plays ~69% (1234s of 1800s)"
  }
  ```

## Fix details

- Only non-broken buckets are processed.
- The cut point is `playable - 1.0` seconds. Anything shorter than 2 s is skipped.
- Temporary files are named `.vidtmp_<original>` so they are ignored by later scans.
- After a successful verify the temp is atomically renamed into the original location.
- Unless `--delete-originals` is given, the source that lived in the review folder is moved to `_originals_replaced/` (preserving relative path).

## Compress details

- Only containers that safely accept x264 + copied audio are considered (`.mp4`, `.mkv`, `.mov`, `.m4v`).
- Codecs `hevc`, `av1`, and `vp9` are skipped.
- The video is scaled to even dimensions and forced to `yuv420p`.
- Audio is first tried with `-c:a copy`. If that fails the command is retried with AAC.
- Replacement happens only when:
  - ffmpeg exits 0,
  - duration matches within 1 % (or 1 s),
  - new size is < 95 % of the old size.
- Paths that were attempted (success or skip) are written to `compressed.txt` so they are not retried.

## Safety

- No file is ever deleted unless you explicitly pass `--delete-originals`.
- Every output is probed and (for fix) partially decoded before it replaces the original.
- Ctrl-C is caught; the script exits cleanly and tells you that already-finished work is kept.

## Performance tips

- Run `scan` without `--deep` first to get a quick picture, then re-run with `--deep` only on the review folder if you want mid-file damage detection.
- Compression with `ultrafast` is I/O-bound more than CPU-bound on modern SSDs; you can raise the preset if you have spare cores and want smaller files.
- For multi-terabyte drives consider pointing `--review` at a different disk so the moves are cross-device copies rather than renames.
