"""
parallax.py — 1枚の絵＋奥行き画像(depth)から、立体的なカメラ移動の映像を作る（2.5D）。
奥行き画像は「明るい＝手前、暗い＝奥」のグレースケール。手前ほど大きく、奥ほど小さくずれる。
make_doc_video.py から呼ばれる。単体でも使える:
  python parallax.py 絵.png 奥行き.png out.mp4 --seconds 8 --motion pan_right
必要: numpy, opencv-python-headless, ffmpeg
"""
import argparse
import subprocess
import sys

import cv2
import numpy as np


def camera(motion, p):
    """進行度 p(0〜1) → (拡大率, 横ずれ(幅に対する割合)) 。横ずれは手前の物ほど大きく効く。"""
    e = p * p * (3 - 2 * p)  # ease in-out
    if motion == "zoom_out":
        return 1.12 - 0.10 * e, -0.020 * (e - 0.5) * 2
    if motion == "pan_right":
        return 1.10, 0.040 * (e - 0.5) * 2
    if motion == "pan_left":
        return 1.10, -0.040 * (e - 0.5) * 2
    return 1.02 + 0.10 * e, 0.020 * (e - 0.5) * 2  # zoom_in


def prepare_depth(depth, size):
    d = depth.astype(np.float32)
    d = (d - d.min()) / max(d.max() - d.min(), 1e-6)
    d = cv2.resize(d, size, interpolation=cv2.INTER_CUBIC)
    # 境界の破綻を減らすため、少しぼかす
    return cv2.GaussianBlur(d, (0, 0), max(size[0] / 400, 1.0))


def write_parallax(base_path, depth_path, out_path, seconds, W, H, fps=30, motion="zoom_in", strength=1.0,
                   crf=16):
    base = cv2.imread(str(base_path), cv2.IMREAD_COLOR)
    if base is None:
        raise RuntimeError(f"画像を読めません: {base_path}")
    bh, bw = base.shape[:2]
    dep = cv2.imread(str(depth_path), cv2.IMREAD_GRAYSCALE)
    if dep is None:
        raise RuntimeError(f"奥行き画像を読めません: {depth_path}")
    depth = prepare_depth(dep, (bw, bh))
    n = max(int(round(seconds * fps)), 2)

    # 出力座標 (u,v) → 元画像の座標（ずれなし）の基準格子
    u, v = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    sx0, sy0 = bw / W, bh / H  # 出力1pxあたりの元画像px（拡大率1のとき、全体が映る）

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-pix_fmt", "yuv420p", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for i in range(n):
            p = i / (n - 1)
            z, shift = camera(motion, p)
            # ずれなしの対応位置（中心基準で z 倍に拡大）
            mx = (u - W / 2) * sx0 / z + bw / 2
            my = (v - H / 2) * sy0 / z + bh / 2
            # その位置の奥行き（0=奥, 1=手前）。手前は大きく、奥は小さくずらす
            dl = cv2.remap(depth, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            disp = shift * bw * strength * (dl - 0.5) * 2.0
            frame = cv2.remap(base, mx - disp, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    finally:
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError("ffmpeg が失敗しました")


def gradient_depth(w, h):
    """奥行き推定モデルが無いときの代用：画面の下ほど手前（地面が手前に広がる風景向け）。"""
    y = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    d = np.repeat(y ** 1.2, w, axis=1)
    return (d * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image"); ap.add_argument("depth"); ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--motion", default="zoom_in", choices=["zoom_in", "zoom_out", "pan_left", "pan_right"])
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--strength", type=float, default=1.0)
    a = ap.parse_args()
    W, H = (int(x) for x in a.size.split("x"))
    write_parallax(a.image, a.depth, a.out, a.seconds, W, H, motion=a.motion, strength=a.strength)
    print("保存:", a.out)


if __name__ == "__main__":
    sys.exit(main())
