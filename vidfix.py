#!/usr/bin/env python3
"""
vidfix.py - find broken videos, trim off the broken parts, and compress.

  python3 vidfix.py scan     DRIVE [--deep]
  python3 vidfix.py fix      DRIVE [--delete-originals]
  python3 vidfix.py compress DRIVE [--crf 28] [--dry-run]

scan      sorts flagged videos into 5 folders inside DRIVE/_REVIEW2 (moves only, deletes nothing)
fix       trims the broken tail off the 4 "partly plays" folders, saves the result under the
          ORIGINAL name in the ORIGINAL place, then removes the original (see --delete-originals)
compress  re-encodes every video with ffmpeg x264 "ultrafast" and replaces it, only if the
          result is verified and smaller

Needs: ffmpeg, ffprobe and the 'file' command   (sudo apt install ffmpeg file)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXT = set(
    ".mp4 .mkv .avi .mov .wmv .flv .f4v .webm .m4v .mpg .mpeg .mpe .m2v .m2ts .mts .ts "
    ".vob .ogv .ogm .3gp .3g2 .divx .rm .rmvb .asf .mxf .dv .wtv".split()
)
PARTIAL_EXT = set(
    ".part .crdownload .ytdl .aria2 .tmp .temp .download .opdownload .unconfirmed .!ut .bc!".split()
)
COMPRESS_EXT = {".mp4", ".mkv", ".mov", ".m4v"}   # containers that take x264 + copied audio safely
SKIP_CODECS = {"hevc", "av1", "vp9"}              # already efficient, x264 would make them bigger
MP4_FAMILY = {".mp4", ".mov", ".m4v"}

# (minimum fraction that plays, folder name)
BUCKETS = [
    (0.90, "1_playable_90plus"),
    (0.75, "2_playable_75plus"),
    (0.50, "3_playable_50plus"),
    (0.00, "4_playable_under50"),
]
BROKEN = "5_completely_broken"
OLD_REVIEW = re.compile(r"^_REVIEW/[0-9]_[^/]+/")   # folders made by the earlier bash script


# ---------------------------------------------------------------- helpers
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def probe(path):
    """(video codec, duration seconds) or None if the file can't be opened as video."""
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name:format=duration", "-of", "json", str(path)])
    if r.returncode != 0:
        return None
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return None
    if not j.get("streams"):
        return None
    try:
        dur = float(j.get("format", {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return j["streams"][0].get("codec_name", ""), dur


def last_packet_time(path):
    """Timestamp of the last video packet that actually exists in the file."""
    p = subprocess.Popen(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,dts_time", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
    best = 0.0
    for line in p.stdout:
        for tok in line.strip().split(","):
            try:
                v = float(tok)
            except ValueError:
                continue
            if v > best:
                best = v
    p.wait()
    return best


def first_decode_error_time(path):
    """Decode the whole video; return the time where decoding first fails, or None if clean."""
    r = run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-nostats", "-i", str(path),
             "-map", "0:v:0", "-f", "null", "-progress", "pipe:1", "-"])
    if r.returncode == 0:
        return None
    t = 0
    for line in r.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                t = int(line.split("=")[1])
            except ValueError:
                pass
    return t / 1e6


def classify(path, deep):
    info = probe(path)
    if info is None:
        return BROKEN, 0.0, 0.0, "cannot be opened / no video stream"
    _, dur = info
    if dur <= 0:
        return BROKEN, 0.0, 0.0, "no valid duration"
    playable = last_packet_time(path)
    if deep:
        t = first_decode_error_time(path)
        if t is not None:
            playable = min(playable, t)
    if playable <= 0:
        return BROKEN, 0.0, dur, "nothing plays"
    ratio = min(playable / dur, 1.0)
    if ratio >= 0.98 or dur - playable <= 2:
        return "ok", playable, dur, ""
    note = f"plays ~{ratio * 100:.0f}% ({playable:.0f}s of {dur:.0f}s)"
    for threshold, name in BUCKETS:
        if ratio >= threshold:
            return name, playable, dur, note


def kind(path):
    if path.name.startswith(".vidtmp_"):
        return None
    ext = path.suffix.lower()
    if ext in PARTIAL_EXT:
        return "partial"
    if ext in VIDEO_EXT:
        return "video"
    try:
        if path.stat().st_size >= 100 * 1024:
            r = run(["file", "-b", "--mime-type", str(path)])
            if r.stdout.startswith("video/"):
                return "video"
    except OSError:
        pass
    return None


def iter_files(src, review):
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if os.path.join(root, d) != str(review) and d != "lost+found"]
        for name in files:
            yield Path(root) / name


def orig_rel(rel):
    return OLD_REVIEW.sub("", rel)


def unique(p):
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.stem}__{i}{p.suffix}")
        if not q.exists():
            return q
        i += 1


def verify(path, expect_seconds):
    info = probe(path)
    if not info or info[1] < expect_seconds * 0.9:
        return False
    r = run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-sseof", "-5", "-i", str(path),
             "-f", "null", "-"])
    return r.returncode == 0 and not r.stderr.strip()


def paths(a):
    src = Path(a.drive).resolve()
    review = Path(a.review).resolve() if a.review else src / "_REVIEW2"
    return src, review


# ---------------------------------------------------------------- scan
def cmd_scan(a):
    src, review = paths(a)
    review.mkdir(parents=True, exist_ok=True)
    manifest = review / "manifest.jsonl"
    n = moved = 0
    for f in iter_files(src, review):
        k = kind(f)
        if not k:
            continue
        n += 1
        if k == "partial":
            bucket, playable, dur, note = BROKEN, 0.0, 0.0, "partial-download file"
        else:
            bucket, playable, dur, note = classify(f, a.deep)
        if bucket == "ok":
            print(f"\r[{n} videos checked | {moved} flagged]", end="", flush=True)
            continue
        rel = orig_rel(str(f.relative_to(src)))
        dest = unique(review / bucket / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        with open(manifest, "a") as m:
            m.write(json.dumps({"bucket": bucket, "current": str(dest), "original": str(src / rel),
                                "playable": playable, "duration": dur, "note": note}) + "\n")
        moved += 1
        print(f"\nMOVED -> {bucket}: {rel}  ({note})")
    print(f"\nDone. {n} videos checked, {moved} flagged. Review folder: {review}")


# ---------------------------------------------------------------- fix
def cmd_fix(a):
    src, review = paths(a)
    manifest = review / "manifest.jsonl"
    if not manifest.exists():
        sys.exit("No manifest found. Run the scan first.")
    entries = [json.loads(line) for line in open(manifest) if line.strip()]
    fixed = failed = 0
    for e in entries:
        if e["bucket"] == BROKEN:
            continue
        cur, orig = Path(e["current"]), Path(e["original"])
        if not cur.exists():
            continue                      # already handled
        if orig.exists():
            print(f"SKIP (name already taken): {orig}")
            continue
        end = e["playable"] - 1.0         # drop the possibly half-written last second
        if end < 2:
            print(f"SKIP (too little playable): {orig.name}")
            continue
        orig.parent.mkdir(parents=True, exist_ok=True)
        tmp = orig.with_name(".vidtmp_" + orig.name)
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
               "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
               "-i", str(cur), "-t", f"{end:.3f}",
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
               "-avoid_negative_ts", "make_zero"]
        if orig.suffix.lower() in MP4_FAMILY:
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(tmp))
        r = run(cmd)
        if r.returncode == 0 and tmp.exists() and verify(tmp, end):
            os.replace(tmp, orig)         # same name, same place as the original
            if a.delete_originals:
                cur.unlink()
            else:
                keep = unique(review / "_originals_replaced" / orig.relative_to(src))
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cur), str(keep))
            fixed += 1
            print(f"FIXED: {orig.name}  (kept first {end:.0f}s)")
        else:
            tmp.unlink(missing_ok=True)
            failed += 1
            print(f"FAILED, original left untouched: {cur}")
    print(f"\nDone. Fixed {fixed}, failed {failed}.")


# ---------------------------------------------------------------- compress
def cmd_compress(a):
    src, review = paths(a)
    review.mkdir(parents=True, exist_ok=True)
    done_file = review / "compressed.txt"
    done = set(done_file.read_text().splitlines()) if done_file.exists() else set()
    saved = count = 0
    for f in iter_files(src, review):
        rel = f.relative_to(src)
        if any(part.startswith("_REVIEW") for part in rel.parts) or f.name.startswith(".vidtmp_"):
            continue
        if f.suffix.lower() not in COMPRESS_EXT or str(f) in done:
            continue
        info = probe(f)
        if not info or info[1] <= 0 or info[0] in SKIP_CODECS:
            continue
        dur = info[1]
        before = f.stat().st_size
        if a.dry_run:
            print(f"would compress: {rel}")
            continue
        tmp = f.with_name(".vidtmp_" + f.name)
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(f),
               "-map", "0:v:0", "-map", "0:a?",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(a.crf),
               "-c:a", "copy"]
        if f.suffix.lower() in MP4_FAMILY:
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(tmp))
        r = run(cmd)
        if r.returncode != 0:             # audio may not fit the container, retry with AAC
            tmp.unlink(missing_ok=True)
            cmd[cmd.index("copy")] = "aac"
            r = run(cmd)
        ok = False
        if r.returncode == 0 and tmp.exists():
            after = tmp.stat().st_size
            new = probe(tmp)
            ok = bool(new) and abs(new[1] - dur) <= max(1.0, dur * 0.01) and after < before * 0.95
        if ok:
            os.replace(tmp, f)            # same filename, original is gone
            gain = before - f.stat().st_size
            saved += gain
            count += 1
            with open(done_file, "a") as d:
                d.write(str(f) + "\n")
            print(f"COMPRESSED: {rel}  (-{gain / 1e6:.0f} MB)")
        else:
            tmp.unlink(missing_ok=True)
            with open(done_file, "a") as d:
                d.write(str(f) + "\n")    # don't retry files that don't shrink
            print(f"SKIPPED (no real saving or failed): {rel}")
    print(f"\nDone. Compressed {count} files, saved {saved / 1e9:.2f} GB.")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "fix", "compress"):
        p = sub.add_parser(name)
        p.add_argument("drive", help="top folder of the drive")
        p.add_argument("--review", help="review folder (default: DRIVE/_REVIEW2)")
        if name == "scan":
            p.add_argument("--deep", action="store_true",
                           help="decode everything to find damage in the middle (slow)")
        if name == "fix":
            p.add_argument("--delete-originals", action="store_true",
                           help="delete originals instead of moving them to _originals_replaced")
        if name == "compress":
            p.add_argument("--crf", type=int, default=28, help="quality, higher = smaller (default 28)")
            p.add_argument("--dry-run", action="store_true", help="only list what would be compressed")
    a = ap.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found. Run: sudo apt install ffmpeg")
    {"scan": cmd_scan, "fix": cmd_fix, "compress": cmd_compress}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Anything already moved or fixed stays that way; rerun to continue.")
