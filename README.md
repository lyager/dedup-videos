# dedup-videos

Find and remove duplicate videos using perceptual hashing. Keeps the highest quality version.

## Features

- **Content-based detection** - Uses perceptual hashing (pHash) to find duplicates even with different encodings/resolutions
- **Quality-aware** - Keeps the best version based on: Resolution > Bitrate > Filesize
- **Safe by default** - Dry-run mode shows what would be deleted without making changes
- **macOS Trash** - Moves files to Trash (recoverable) instead of permanent deletion

## Requirements

- Python 3.11+
- ffmpeg/ffprobe (for video analysis)
- macOS (for Trash functionality)

## Installation

```bash
# Clone or download this project
cd dedup-videos

# Dependencies are managed by uv
uv sync
```

## Usage

```bash
# Dry-run (default) - shows files that would be deleted
uv run dedup_videos.py "/path/to/videos"

# Verbose output - shows duplicate groups with quality details
uv run dedup_videos.py "/path/to/videos" --verbose

# Actually move duplicates to Trash
uv run dedup_videos.py "/path/to/videos" --execute

# Adjust matching strictness (default: 6, lower = stricter)
uv run dedup_videos.py "/path/to/videos" --threshold 8
```

## Output Examples

**Default (compact):**
```
══════════════════════════════════════════════════════════════════════
 DUPLICATE VIDEO REPORT
 116 analyzed | 13 groups | 14 to trash | 9.54 GB recoverable
══════════════════════════════════════════════════════════════════════

Files to delete:
  ✗ video_720p.mp4 (720p 351.6 MB) → Lower resolution (720p vs 1080p)
  ✗ video_copy.mp4 (1080p 421.9 MB) → Lower bitrate (2,983 vs 7,003 kbps)
```

**Verbose (`-v`):**
```
[1] ✓ video_1080p.mp4
      1080p 5,642kbps 1.0 GB
    ✗ video_720p.mp4
      720p 1,835kbps 351.6 MB → Lower resolution (720p vs 1080p)
```

## How It Works

1. Scans directory recursively for video files
2. Extracts 5 frames from each video (at 10%, 30%, 50%, 70%, 90% positions)
3. Generates perceptual hashes for each frame
4. Compares all videos - flags as duplicates if hash distance ≤ threshold AND duration within 10%
5. Ranks duplicates by quality score and keeps the best one

## Options

| Flag | Description |
|------|-------------|
| `--execute` | Actually move duplicates to Trash |
| `--verbose`, `-v` | Show detailed output with groups |
| `--threshold N` | Hash similarity threshold (default: 6) |
