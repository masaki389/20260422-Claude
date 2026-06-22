import subprocess

VIDEO = "/workspaces/20260422-Claude/assets/characters/無題動画 (2).mp4"

times = [float(l.strip()) for l in open('/workspaces/20260422-Claude/outputs/scene_cuts.txt') if l.strip()]
times = [0.0] + times

for i, t in enumerate(times, 1):
    extract_t = t + 0.3
    out = f"/workspaces/20260422-Claude/outputs/video_frames_telop/tframe_{i:03d}_{t:.1f}s.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(extract_t), "-i", VIDEO, "-frames:v", "1", out, "-v", "error"]
    )

print(f"{len(times)}枚抽出完了")
