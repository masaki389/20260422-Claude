import subprocess

VIDEO = "/workspaces/20260422-Claude/assets/characters/copy_36C94ACC-D4C3-47B0-A945-3BDD3D1B5A51.MOV"

times = [float(l.strip()) for l in open('/workspaces/20260422-Claude/outputs/scene_cuts.txt') if l.strip()]
times = [0.0] + times

for i, t in enumerate(times, 1):
    extract_t = t + 0.3
    out = f"/workspaces/20260422-Claude/outputs/video_frames/vframe_{i:03d}_{t:.1f}s.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(extract_t), "-i", VIDEO, "-frames:v", "1", out, "-v", "error"]
    )

print(f"{len(times)}枚抽出完了")
