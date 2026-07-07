#!/usr/bin/env python3
"""
Duplicate Video Finder and Remover

Finds duplicate videos using perceptual hashing (content-based comparison),
keeps the highest quality version, and moves duplicates to macOS Trash.

Usage:
    # Dry-run (default) - shows report only
    uv run dedup_videos.py "/path/to/videos"

    # Execute - actually move duplicates to Trash
    uv run dedup_videos.py "/path/to/videos" --execute

Analysis results are cached in .dedup_videos_cache.json inside the scanned
directory, so a re-run (e.g. --execute after a dry-run) only analyzes new or
changed files. Use --no-cache to force a full re-analysis.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image


# Video file extensions to process
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".3gp",
}

# Perceptual hash similarity threshold (Hamming distance)
# Lower = stricter matching. For 64-bit hash, max distance is 64.
# Distance <= 6 is very strict (nearly identical frames)
HASH_THRESHOLD = 6

# Number of frames to sample from each video
NUM_SAMPLE_FRAMES = 5

# Scan cache: stored inside the scanned directory, keyed by path relative to it
CACHE_FILENAME = ".dedup_videos_cache.json"
CACHE_VERSION = 1  # bump when frame positions or entry schema change
CACHE_SAVE_INTERVAL = 20  # newly analyzed files between autosaves

# Sentinel returned by ScanCache.lookup for a cached analysis failure
CACHE_FAILED = object()


@dataclass
class VideoInfo:
    """Stores metadata and hashes for a video file."""

    path: Path
    size_bytes: int
    duration: float
    width: int
    height: int
    bitrate: int  # in kbps
    frame_hashes: list  # list of imagehash objects

    @property
    def resolution(self) -> int:
        """Total pixel count."""
        return self.width * self.height

    @property
    def quality_score(self) -> float:
        """
        Compute quality score for ranking.
        Priority: resolution > bitrate > filesize
        """
        return (
            (self.resolution * 1_000_000)
            + (self.bitrate * 1_000)
            + (self.size_bytes / 1_000_000)
        )

    @property
    def size_human(self) -> str:
        """Human-readable file size."""
        size = self.size_bytes
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    @property
    def resolution_label(self) -> str:
        """Human-readable resolution label."""
        if self.height >= 2160:
            return "4K"
        elif self.height >= 1440:
            return "1440p"
        elif self.height >= 1080:
            return "1080p"
        elif self.height >= 720:
            return "720p"
        elif self.height >= 480:
            return "480p"
        else:
            return f"{self.height}p"


class ScanCache:
    """
    Persistent cache of per-file analysis results.

    Lives as a JSON file inside the scanned directory, keyed by path relative
    to it, so results survive between runs (e.g. a dry-run followed by
    --execute) and volume remounts under a different name. Entries are
    validated against file size (exact) and mtime (second granularity, since
    sync tools may restore mtimes with reduced precision).
    """

    def __init__(self, root: Path, enabled: bool = True, retry_failed: bool = False):
        self.root = root
        self.enabled = enabled
        self.retry_failed = retry_failed
        self.entries: dict[str, dict] = {}
        self.hits = 0
        self._dirty = 0
        if not enabled:
            return
        cache_path = root / CACHE_FILENAME
        if not cache_path.exists():
            return
        try:
            with open(cache_path) as f:
                data = json.load(f)
            if (
                data.get("version") != CACHE_VERSION
                or data.get("hash_algo") != "phash"
                or data.get("num_frames") != NUM_SAMPLE_FRAMES
                or not isinstance(data.get("entries"), dict)
            ):
                print("Scan cache format changed - re-analyzing all files.")
                return
            self.entries = data["entries"]
        except (OSError, json.JSONDecodeError):
            print("Scan cache unreadable - re-analyzing all files.")

    def _key(self, video_path: Path) -> str:
        return video_path.relative_to(self.root).as_posix()

    def lookup(self, video_path: Path) -> "VideoInfo | None | object":
        """
        Return the cached result for a file, or None on a cache miss.
        A cached analysis failure is returned as the CACHE_FAILED sentinel
        (unless retry_failed is set, which turns it into a miss).
        """
        if not self.enabled:
            return None
        entry = self.entries.get(self._key(video_path))
        if entry is None:
            return None
        try:
            st = video_path.stat()
        except OSError:
            return None
        if (
            entry.get("size") != st.st_size
            or entry.get("mtime_ns", 0) // 1_000_000_000
            != st.st_mtime_ns // 1_000_000_000
        ):
            return None
        if entry.get("failed"):
            if self.retry_failed:
                return None
            self.hits += 1
            return CACHE_FAILED
        try:
            frame_hashes = [imagehash.hex_to_hash(h) for h in entry["frame_hashes"]]
            info = VideoInfo(
                path=video_path,
                size_bytes=st.st_size,
                duration=float(entry["duration"]),
                width=int(entry["width"]),
                height=int(entry["height"]),
                bitrate=int(entry["bitrate"]),
                frame_hashes=frame_hashes,
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not frame_hashes:
            return None
        self.hits += 1
        return info

    def store(self, video_path: Path, info: "VideoInfo | None") -> None:
        """Record an analysis result (info=None records a failure)."""
        if not self.enabled:
            return
        try:
            st = video_path.stat()
        except OSError:
            return
        entry = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "failed": info is None}
        if info is not None:
            entry.update(
                duration=info.duration,
                width=info.width,
                height=info.height,
                bitrate=info.bitrate,
                frame_hashes=[str(h) for h in info.frame_hashes],
            )
        self.entries[self._key(video_path)] = entry
        self._dirty += 1
        if self._dirty >= CACHE_SAVE_INTERVAL:
            self.save()

    def prune(self, existing_paths: list[Path]) -> None:
        """Drop entries for files no longer on disk (e.g. trashed by --execute)."""
        keys = {self._key(p) for p in existing_paths}
        stale = [k for k in self.entries if k not in keys]
        for k in stale:
            del self.entries[k]
        if stale:
            self._dirty += 1

    def save(self, force: bool = False) -> None:
        """Atomically write the cache to disk if there are unsaved changes."""
        if not self.enabled or (self._dirty == 0 and not force):
            return
        data = {
            "version": CACHE_VERSION,
            "hash_algo": "phash",
            "num_frames": NUM_SAMPLE_FRAMES,
            "entries": self.entries,
        }
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.root, prefix=CACHE_FILENAME + ".")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, self.root / CACHE_FILENAME)
            self._dirty = 0
        except OSError as e:
            print(f"  Warning: Could not save scan cache: {e}")
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def find_videos(directory: Path) -> list[Path]:
    """Recursively find all video files in directory."""
    videos = []
    for root, _, files in os.walk(directory):
        for file in files:
            if Path(file).suffix.lower() in VIDEO_EXTENSIONS:
                videos.append(Path(root) / file)
    return sorted(videos)


def get_video_metadata(video_path: Path) -> dict | None:
    """Extract video metadata using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"  Warning: Could not read metadata from {video_path.name}: {e}")
        return None


def extract_frames(video_path: Path, num_frames: int = NUM_SAMPLE_FRAMES) -> list[Path]:
    """Extract sample frames from video at evenly distributed timestamps."""
    frames = []

    # Get duration first
    metadata = get_video_metadata(video_path)
    if not metadata:
        return frames

    duration = float(metadata.get("format", {}).get("duration", 0))
    if duration <= 0:
        return frames

    # Create temp directory for frames
    temp_dir = tempfile.mkdtemp(prefix="dedup_frames_")

    # Extract frames at 10%, 30%, 50%, 70%, 90% of duration
    positions = [0.1, 0.3, 0.5, 0.7, 0.9]

    for i, pos in enumerate(positions[:num_frames]):
        timestamp = duration * pos
        frame_path = Path(temp_dir) / f"frame_{i}.jpg"

        try:
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite
                "-ss",
                str(timestamp),
                "-i",
                str(video_path),
                "-vframes",
                "1",
                "-q:v",
                "2",
                str(frame_path),
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode == 0 and frame_path.exists():
                frames.append(frame_path)
        except subprocess.TimeoutExpired:
            continue

    return frames


def compute_frame_hash(frame_path: Path) -> imagehash.ImageHash | None:
    """Compute perceptual hash for a single frame."""
    try:
        with Image.open(frame_path) as img:
            # Convert to RGB if necessary
            if img.mode != "RGB":
                img = img.convert("RGB")
            # Use perceptual hash (pHash)
            return imagehash.phash(img)
    except Exception:
        return None


def compute_video_hash(video_path: Path) -> list[imagehash.ImageHash]:
    """Extract frames and compute perceptual hashes for a video."""
    hashes = []
    frames = extract_frames(video_path)

    for frame_path in frames:
        h = compute_frame_hash(frame_path)
        if h is not None:
            hashes.append(h)
        # Clean up frame file
        try:
            frame_path.unlink()
        except Exception:
            pass

    # Clean up temp directory
    if frames:
        try:
            frames[0].parent.rmdir()
        except Exception:
            pass

    return hashes


def analyze_video(video_path: Path) -> VideoInfo | None:
    """Analyze a single video file and return its info."""
    metadata = get_video_metadata(video_path)
    if not metadata:
        return None

    # Get file size
    try:
        size_bytes = video_path.stat().st_size
    except Exception:
        return None

    # Extract video stream info
    width = 0
    height = 0
    bitrate = 0
    duration = 0

    # Get duration from format
    format_info = metadata.get("format", {})
    duration = float(format_info.get("duration", 0))

    # Get overall bitrate from format, fallback to calculation
    if "bit_rate" in format_info:
        bitrate = int(format_info["bit_rate"]) // 1000  # Convert to kbps
    elif duration > 0:
        bitrate = int(
            (size_bytes * 8) / duration / 1000
        )  # Calculate from size/duration

    # Find video stream for resolution
    for stream in metadata.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width", 0)
            height = stream.get("height", 0)
            # If we didn't get bitrate from format, try stream
            if bitrate == 0 and "bit_rate" in stream:
                bitrate = int(stream["bit_rate"]) // 1000
            break

    if width == 0 or height == 0:
        return None

    # Compute perceptual hashes
    frame_hashes = compute_video_hash(video_path)
    if not frame_hashes:
        return None

    return VideoInfo(
        path=video_path,
        size_bytes=size_bytes,
        duration=duration,
        width=width,
        height=height,
        bitrate=bitrate,
        frame_hashes=frame_hashes,
    )


def compute_hash_distance(hashes1: list, hashes2: list) -> float:
    """
    Compute average Hamming distance between two sets of frame hashes.
    Returns the minimum average distance (comparing corresponding frames).
    """
    if not hashes1 or not hashes2:
        return float("inf")

    # Compare frame by frame and compute average distance
    distances = []
    for h1, h2 in zip(hashes1, hashes2):
        distances.append(h1 - h2)  # Hamming distance

    if not distances:
        return float("inf")

    return sum(distances) / len(distances)


def are_duplicates(
    video1: VideoInfo, video2: VideoInfo, threshold: int = HASH_THRESHOLD
) -> bool:
    """
    Determine if two videos are duplicates based on perceptual hash similarity.
    Also checks if durations are within 10% of each other as additional verification.
    """
    # Check duration similarity first (quick filter)
    if video1.duration > 0 and video2.duration > 0:
        duration_diff = abs(video1.duration - video2.duration)
        max_duration = max(video1.duration, video2.duration)
        if duration_diff / max_duration > 0.1:  # More than 10% difference
            return False

    # Check perceptual hash similarity
    distance = compute_hash_distance(video1.frame_hashes, video2.frame_hashes)
    return distance <= threshold


def find_duplicate_groups(
    videos: list[VideoInfo], threshold: int = HASH_THRESHOLD
) -> list[list[VideoInfo]]:
    """Find groups of duplicate videos."""
    # Track which videos have been assigned to a group
    assigned = set()
    groups = []

    for i, video1 in enumerate(videos):
        if i in assigned:
            continue

        # Start a new group with this video
        group = [video1]
        assigned.add(i)

        # Find all videos similar to this one
        for j, video2 in enumerate(videos[i + 1 :], start=i + 1):
            if j in assigned:
                continue

            if are_duplicates(video1, video2, threshold):
                group.append(video2)
                assigned.add(j)

        # Only keep groups with duplicates (more than 1 video)
        if len(group) > 1:
            groups.append(group)

    return groups


def select_best_quality(group: list[VideoInfo]) -> tuple[VideoInfo, list[VideoInfo]]:
    """
    From a group of duplicates, select the best quality video to keep.
    Returns (keeper, list of videos to trash).
    """
    # Sort by quality score (highest first)
    sorted_group = sorted(group, key=lambda v: v.quality_score, reverse=True)
    keeper = sorted_group[0]
    to_trash = sorted_group[1:]
    return keeper, to_trash


def get_trash_reason(keeper: VideoInfo, video: VideoInfo) -> str:
    """Generate a human-readable reason why this video is being trashed."""
    reasons = []

    if video.resolution < keeper.resolution:
        reasons.append(
            f"Lower resolution ({video.resolution_label} vs {keeper.resolution_label})"
        )
    elif video.bitrate < keeper.bitrate:
        reasons.append(f"Lower bitrate ({video.bitrate:,} vs {keeper.bitrate:,} kbps)")
    elif video.size_bytes < keeper.size_bytes:
        reasons.append(f"Smaller file ({video.size_human} vs {keeper.size_human})")
    else:
        reasons.append("Duplicate of higher-ranked file")

    return "; ".join(reasons)


def move_to_trash(file_path: Path) -> bool:
    """Move a file to macOS Trash using osascript."""
    try:
        # Use AppleScript to move to Trash (macOS native way)
        script = f'''
        tell application "Finder"
            delete POSIX file "{file_path}"
        end tell
        '''
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception as e:
        print(f"  Error moving to trash: {e}")
        return False


def print_progress(current: int, total: int, filename: str, width: int = 40):
    """Print a progress bar."""
    percent = current / total
    filled = int(width * percent)
    bar = "█" * filled + "░" * (width - filled)
    # Truncate filename if too long
    max_name_len = 50
    if len(filename) > max_name_len:
        filename = filename[: max_name_len - 3] + "..."
    print(
        f"\rAnalyzing [{bar}] {current}/{total} - {filename:<{max_name_len}}",
        end="",
        flush=True,
    )


def format_duration(seconds: float) -> str:
    """Format duration in human readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def main():
    parser = argparse.ArgumentParser(
        description="Find and remove duplicate videos, keeping the highest quality version.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "/path/to/videos"              # Dry-run (default)
  %(prog)s "/path/to/videos" --execute    # Actually move duplicates to Trash
  %(prog)s "/path/to/videos" --threshold 8  # Use looser matching threshold
  %(prog)s "/path/to/videos" --no-cache     # Ignore cached scan results

Analysis results are cached in .dedup_videos_cache.json inside the scanned
directory; re-runs only analyze new or changed files.
        """,
    )
    parser.add_argument(
        "directory", type=str, help="Directory to scan for duplicate videos"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually move duplicates to Trash (default is dry-run)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output with quality info for each file",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=HASH_THRESHOLD,
        help=f"Hash similarity threshold (default: {HASH_THRESHOLD}, lower=stricter)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore the scan cache and re-analyze every file (cache is neither read nor written)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-attempt files whose previous analysis failed (successful cache entries are still used)",
    )

    args = parser.parse_args()

    directory = Path(args.directory).resolve()

    if not directory.exists():
        print(f"Error: Directory does not exist: {directory}")
        sys.exit(1)

    if not directory.is_dir():
        print(f"Error: Not a directory: {directory}")
        sys.exit(1)

    # Find all videos
    print("Scanning for videos...")
    video_paths = find_videos(directory)

    if not video_paths:
        print("No video files found.")
        sys.exit(0)

    print(f"Found {len(video_paths)} video files\n")

    cache = ScanCache(
        directory, enabled=not args.no_cache, retry_failed=args.retry_failed
    )

    # Analyze each video
    videos = []
    failed = []
    analyzed_count = 0

    try:
        for i, video_path in enumerate(video_paths, 1):
            print_progress(i, len(video_paths), video_path.name)

            cached = cache.lookup(video_path)
            if cached is CACHE_FAILED:
                failed.append(video_path)
                continue
            if cached is not None:
                videos.append(cached)
                continue

            video_info = analyze_video(video_path)
            analyzed_count += 1
            cache.store(video_path, video_info)
            if video_info:
                videos.append(video_info)
            else:
                failed.append(video_path)
    except KeyboardInterrupt:
        cache.save(force=True)
        print("\n\nInterrupted - scan progress saved to cache.")
        sys.exit(130)

    cache.prune(video_paths)
    cache.save(force=True)

    print()  # New line after progress bar
    if not args.no_cache:
        print(f"{cache.hits} loaded from cache, {analyzed_count} analyzed")
    print()

    if failed:
        print(f"Warning: Could not analyze {len(failed)} files")
        for f in failed[:5]:  # Show first 5
            print(f"  - {f.name}")
        if len(failed) > 5:
            print(f"  ... and {len(failed) - 5} more")
        print()

    # Find duplicate groups
    print("Finding duplicates...")
    duplicate_groups = find_duplicate_groups(videos, threshold=args.threshold)

    if not duplicate_groups:
        print("\n" + "=" * 60)
        print("No duplicate videos found!")
        print("=" * 60)
        sys.exit(0)

    # Calculate statistics
    total_trash = sum(len(group) - 1 for group in duplicate_groups)
    total_space = sum(
        sum(v.size_bytes for v in select_best_quality(group)[1])
        for group in duplicate_groups
    )

    # Print report
    print(f"\n{'═' * 70}")
    print(f" DUPLICATE VIDEO REPORT")
    print(
        f" {len(videos)} analyzed | {len(duplicate_groups)} groups | {total_trash} to trash | {total_space / (1024**3):.2f} GB recoverable"
    )
    print(f"{'═' * 70}")

    if args.verbose:
        # Verbose output: show keeper and all details
        for group_num, group in enumerate(duplicate_groups, 1):
            keeper, to_trash = select_best_quality(group)
            print(f"\n[{group_num}] ✓ {keeper.path.name}")
            print(
                f"      {keeper.resolution_label} {keeper.bitrate:,}kbps {keeper.size_human}"
            )
            for video in to_trash:
                reason = get_trash_reason(keeper, video)
                print(f"    ✗ {video.path.name}")
                print(
                    f"      {video.resolution_label} {video.bitrate:,}kbps {video.size_human} → {reason}"
                )
    else:
        # Compact output: one line per file to delete
        print("\nFiles to delete:")
        for group in duplicate_groups:
            keeper, to_trash = select_best_quality(group)
            for video in to_trash:
                reason = get_trash_reason(keeper, video)
                print(
                    f"  ✗ {video.path.name} ({video.resolution_label} {video.size_human}) → {reason}"
                )

    print(f"\n{'═' * 70}")
    if args.execute:
        print("Moving duplicates to Trash...")
        success_count = 0
        fail_count = 0
        for group in duplicate_groups:
            _, to_trash = select_best_quality(group)
            for video in to_trash:
                if move_to_trash(video.path):
                    print(f"  ✓ {video.path.name}")
                    success_count += 1
                else:
                    print(f"  ✗ {video.path.name}")
                    fail_count += 1
        print(
            f"\nMoved {success_count} files to Trash"
            + (f" ({fail_count} failed)" if fail_count else "")
        )
    else:
        print("DRY RUN - Run with --execute to move duplicates to Trash.")
    print(f"{'═' * 70}")


if __name__ == "__main__":
    main()
