#!/usr/bin/env sh
set -eu

output_path="${1:-t0-artifacts/t0-100mb.mp4}"
output_dir=$(dirname "$output_path")
mkdir -p "$output_dir"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "需要 ffmpeg 才能生成有效 MP4 测试文件。" >&2
    exit 1
fi

# 100 秒、约 8 Mbit/s 的测试图案视频，目标体积接近 100 MB。
ffmpeg \
    -hide_banner \
    -loglevel warning \
    -f lavfi \
    -i "testsrc2=size=1280x720:rate=25" \
    -t 100 \
    -c:v libx264 \
    -preset veryfast \
    -b:v 8M \
    -maxrate 8M \
    -bufsize 16M \
    -pix_fmt yuv420p \
    -movflags +faststart \
    -y \
    "$output_path"

size_bytes=$(wc -c < "$output_path" | tr -d ' ')
echo "generated=$output_path size_bytes=$size_bytes"
