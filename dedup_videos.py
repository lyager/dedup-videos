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

    # Analyze each video
    videos = []
    failed = []

    for i, video_path in enumerate(video_paths, 1):
        print_progress(i, len(video_paths), video_path.name)

        video_info = analyze_video(video_path)
        if video_info:
            videos.append(video_info)
        else:
            failed.append(video_path)

    print()  # New line after progress bar
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
