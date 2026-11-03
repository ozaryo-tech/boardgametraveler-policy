#!/usr/bin/env python3
"""
make_video.py — 拡張機能が保存したフォルダ（page.json / images/ / recording.webm）から
解説動画(mp4)を、3つの段階に分けて作る。Claude API は使わない。

  1) plan    場面分けと字幕（ナレーション）の下書きを scenes.json に作る
             → ここで scenes.json を開いて、文章や使う映像の区間を自由に直せる
  2) voice   scenes.json の文章を読み上げ音声にする（VOICEVOX。なければ無音）
             → 文章を直したら、もう一度実行（変えた所だけ作り直す）
  3) render  録画の該当区間 ＋ 抽出した文章（字幕・音声）を合成して mp4 にする

  python make_video.py plan   <フォルダ>
  python make_video.py voice  <フォルダ>
  python make_video.py render <フォルダ> [--scene 2] [--out out.mp4]

必要なもの: ffmpeg, Pillow（pip install pillow）
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
HEAD_H = 130  # 上の見出し帯
PANEL_Y = 790  # 下の字幕帯の開始位置
BG = (18, 24, 38)
GOLD = (240, 200, 96)
WHITE = (240, 244, 250)
PANEL = (8, 12, 22)
PAD_SEC = 0.4  # 各カットの後ろに足す間

FONT_CANDIDATES = [
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
]


def die(msg):
    print("エラー: " + msg, file=sys.stderr)
    sys.exit(1)


def run(cmd):
    try:
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        die("ffmpeg の実行に失敗しました:\n" + e.stderr.decode("utf-8", "replace")[-800:])


def find_font(path):
    for p in ([path] if path else []) + FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    die("日本語フォントが見つかりません。--font でフォントファイル(.ttf/.ttc)を指定してください。")


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def save_json(p, obj):
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================ 1) plan
def split_sentences(text):
    return [p.strip() for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]


def chunk_text(text, max_chars):
    chunks, cur = [], ""
    for s in split_sentences(text):
        pieces = [s] if len(s) <= max_chars else [x for x in re.split(r"(?<=[、，,])", s) if x]
        for p in pieces:
            if cur and len(cur) + len(p) > max_chars:
                chunks.append(cur)
                cur = ""
            cur += p
    if cur:
        chunks.append(cur)
    return chunks


def fill_times(blocks):
    """録画の中でそのブロックが現れた時刻(ms)。取れなかった所は前後から補う。"""
    n = len(blocks)
    ts = [b.get("t") for b in blocks]
    known = [i for i, t in enumerate(ts) if t is not None]
    if not known:
        return [None] * n
    out = []
    for i in range(n):
        if ts[i] is not None:
            out.append(ts[i])
            continue
        prev = max([k for k in known if k < i], default=None)
        nxt = min([k for k in known if k > i], default=None)
        if prev is None:
            out.append(0)
        elif nxt is None:
            out.append(ts[prev])
        else:
            out.append(ts[prev] + (ts[nxt] - ts[prev]) * (i - prev) / (nxt - prev))
    for i in range(1, n):  # 時間は戻らない
        out[i] = max(out[i], out[i - 1])
    return out


def ollama_summarize(text, model, host, sentences):
    prompt = (
        "次は世界史の解説文です。ナレーション用に、重要な用語と因果関係を残して"
        f"{sentences}文以内の話し言葉で簡潔にまとめてください。"
        "出力はナレーション本文だけにしてください。\n\n" + text
    )
    req = urllib.request.Request(
        host.rstrip("/") + "/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["response"].strip() or text


def cmd_plan(a):
    folder = Path(a.folder)
    pj = folder / "page.json"
    if not pj.exists():
        die(f"{pj} がありません。")
    data = load_json(pj)
    blocks = data["blocks"]
    times = fill_times(blocks)

    rec = data.get("recording")
    rec_file = rec["file"] if rec and (folder / rec["file"]).exists() else None
    rec_dur = rec.get("durationMs") if rec else None

    # 見出しごとに場面へ
    raw, cur = [], None

    def new(heading, t):
        return {"heading": heading, "texts": [], "images": [], "t": t}

    cur = new(data.get("title", ""), times[0] if times[0] is not None else 0)
    for b, t in zip(blocks, times):
        k = b["type"]
        if k == "heading":
            if cur["texts"] or cur["images"]:
                raw.append(cur)
            cur = new(b["text"], t)
        elif k in ("paragraph", "list"):
            cur["texts"].append(b["text"])
        elif k == "image" and b.get("file") and (folder / b["file"]).exists():
            cur["images"].append({"file": b["file"], "caption": []})
        elif k == "caption" and cur["images"]:
            cur["images"][-1]["caption"].append(b["text"])
    if cur["texts"] or cur["images"]:
        raw.append(cur)

    # 場面ごとの録画区間。画面の最初に同時に見えていた場面（開始時刻がほぼ同じ）は、
    # 次に時刻が変わるまでの区間を、文章量に応じて分け合う。
    starts = [sc["t"] for sc in raw]
    spans = [None] * len(raw)
    i = 0
    while i < len(raw):
        if starts[i] is None:
            i += 1
            continue
        j = i + 1
        while j < len(raw) and starts[j] is not None and starts[j] <= starts[i] + 300:
            j += 1
        nxt = starts[j] if j < len(raw) and starts[j] is not None else (rec_dur if rec_dur else None)
        gend = max(nxt if nxt is not None else starts[i] + 4000 * (j - i), starts[i] + 1500 * (j - i))
        weights = [max(len("".join(raw[k]["texts"])), 1) for k in range(i, j)]
        acc = starts[i]
        for k, wt in zip(range(i, j), weights):
            e = acc + (gend - starts[i]) * wt / sum(weights)
            spans[k] = (acc, e)
            acc = e
        i = j

    scenes = []
    for si, sc in enumerate(raw, 1):
        start, end = spans[si - 1] if spans[si - 1] else (None, None)
        text = "".join(sc["texts"])
        if a.ollama and len(text) > a.max_chars:
            try:
                text = ollama_summarize(text, a.ollama, a.ollama_host, a.summary_sentences)
            except Exception as e:
                print(f"  要約に失敗（元の文章を使用）: {e}", file=sys.stderr)
        pieces = chunk_text(text, a.max_chars) or [""]
        total = sum(max(len(p), 1) for p in pieces)

        chunks, acc = [], 0
        for ci, p in enumerate(pieces, 1):
            c = {"id": f"{si}-{ci}", "text": p}
            img = sc["images"][min((ci - 1) * len(sc["images"]) // len(pieces), len(sc["images"]) - 1)] if sc["images"] else None
            if img:
                c["image"] = img["file"]
                c["image_note"] = " ".join(img["caption"])
            if rec_file and start is not None and not (a.prefer_images and img):
                c["source"] = "recording"
                c["start_ms"] = int(start + (end - start) * acc / total)
                acc += max(len(p), 1)
                c["end_ms"] = int(start + (end - start) * acc / total)
            elif img:
                c["source"] = "image"
            else:
                c["source"] = "card"
            chunks.append(c)
        scenes.append({"id": si, "heading": sc["heading"], "chunks": chunks})

    out = {
        "_説明": "text=字幕と読み上げ。source は recording(録画) / image(抽出画像) / card(見出しだけ)。"
                 "recording の start_ms〜end_ms が使う録画の区間。自由に書き換えてよい。",
        "title": data.get("title", ""),
        "recording": rec_file,
        "scenes": scenes,
    }
    save_json(folder / "scenes.json", out)
    n = sum(len(s["chunks"]) for s in scenes)
    print(f"{len(scenes)} 場面 / {n} カット → {folder / 'scenes.json'}")
    for s in scenes:
        print(f"  [{s['id']}] {s['heading']}  ({len(s['chunks'])}カット)")
    print("次: scenes.json を確認・修正してから `voice` を実行してください。")


# ============================================================ 2) voice
def voicevox_wav(text, speaker, host):
    q = urllib.parse.urlencode({"text": text, "speaker": speaker})
    with urllib.request.urlopen(urllib.request.Request(f"{host}/audio_query?{q}", method="POST"), timeout=60) as r:
        query = r.read()
    req = urllib.request.Request(
        f"{host}/synthesis?speaker={speaker}", data=query,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def silent_wav(path, seconds):
    rate = 24000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def check_voicevox(host):
    try:
        urllib.request.urlopen(host + "/version", timeout=3).read()
        return True
    except Exception:
        return False


def cmd_voice(a):
    folder = Path(a.folder)
    sj = folder / "scenes.json"
    if not sj.exists():
        die("scenes.json がありません。先に `plan` を実行してください。")
    plan = load_json(sj)
    use_voice = not a.no_voice
    if use_voice and not check_voicevox(a.voicevox):
        print(f"※ VOICEVOX({a.voicevox}) に接続できません。無音（字幕のみ）で作ります。", file=sys.stderr)
        use_voice = False
    adir = folder / "_work" / "audio"
    adir.mkdir(parents=True, exist_ok=True)

    made = skipped = 0
    for sc in plan["scenes"]:
        for c in sc["chunks"]:
            key = hashlib.sha1(f"{c['text']}|{a.speaker}|{use_voice}".encode()).hexdigest()[:12]
            wav = adir / f"{c['id']}.wav"
            if wav.exists() and c.get("audio_hash") == key:
                skipped += 1
                continue
            if use_voice and c["text"]:
                try:
                    wav.write_bytes(voicevox_wav(c["text"], a.speaker, a.voicevox))
                except Exception as e:
                    print(f"  音声生成に失敗（無音）: {c['id']} {e}", file=sys.stderr)
                    silent_wav(wav, max(2.5, len(c["text"]) / 6))
            else:
                silent_wav(wav, max(2.5, len(c["text"]) / 6))
            c["audio"] = str(wav.relative_to(folder)).replace("\\", "/")
            c["audio_hash"] = key
            c["duration"] = round(wav_seconds(wav), 2)
            made += 1
    save_json(sj, plan)
    total = sum(c.get("duration", 0) for s in plan["scenes"] for c in s["chunks"])
    print(f"音声: 新規{made}件 / 変更なし{skipped}件 / 合計 約{total / 60:.1f}分")
    print("次: `render` を実行してください（--scene 番号 で1場面だけ試せます）。")


# ============================================================ 3) render
def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def draw_chrome(d, heading, subtitle, font_path, transparent_mid):
    f_head = ImageFont.truetype(font_path, 56)
    f_sub = ImageFont.truetype(font_path, 46)
    d.rectangle((0, 0, W, HEAD_H), fill=BG)
    d.text((80, 48), heading, font=f_head, fill=GOLD)
    d.line((80, HEAD_H - 4, W - 80, HEAD_H - 4), fill=(70, 80, 100), width=2)
    d.rectangle((0, PANEL_Y, W, H), fill=PANEL)
    lines = wrap(d, subtitle, f_sub, W - 200)
    y = PANEL_Y + 25 + max(0, (250 - len(lines) * 66) // 2)
    for ln in lines[:4]:
        d.text((100, y), ln, font=f_sub, fill=WHITE)
        y += 66


def render_overlay(path, heading, subtitle, font_path):
    """録画の上に重ねる見出し帯＋字幕帯（真ん中は透明）。"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_chrome(ImageDraw.Draw(im), heading, subtitle, font_path, True)
    im.save(path)


def render_still(path, heading, subtitle, image_path, note, font_path):
    """抽出した画像（または見出しだけ）を背景にした静止画フレーム。"""
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    f_note = ImageFont.truetype(font_path, 30)
    if image_path and Path(image_path).exists():
        pic = Image.open(image_path).convert("RGB")
        bottom = PANEL_Y - 55 if note else PANEL_Y - 20
        box = (80, HEAD_H + 20, W - 80, bottom)
        bw, bh = box[2] - box[0], box[3] - box[1]
        s = min(bw / pic.width, bh / pic.height)
        pic = pic.resize((max(1, int(pic.width * s)), max(1, int(pic.height * s))), Image.LANCZOS)
        im.paste(pic, (box[0] + (bw - pic.width) // 2, box[1] + (bh - pic.height) // 2))
        if note:
            note = note if d.textlength(note, font=f_note) < W - 160 else note[:60] + "…"
            d.text((W // 2 - d.textlength(note, font=f_note) // 2, PANEL_Y - 44), note, font=f_note, fill=(180, 190, 205))
    else:
        f_big = ImageFont.truetype(font_path, 110)
        d.text(((W - d.textlength(heading, font=f_big)) // 2, 380), heading, font=f_big, fill=WHITE)
    draw_chrome(d, heading, subtitle, font_path, False)
    im.save(path)


def prepare_recording(folder, rec_name, work):
    """MediaRecorder の webm は可変フレームレートでシークも不安定なため、一度 30fps の mp4 に直す。"""
    src = folder / rec_name
    dst = work / "recording_30fps.mp4"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    print("録画を30fpsに変換中…（初回のみ）")
    run(["ffmpeg", "-y", "-i", str(src), "-an", "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-pix_fmt", "yuv420p", str(dst)])
    return dst


def cmd_render(a):
    if not shutil.which("ffmpeg"):
        die("ffmpeg が見つかりません。インストールして PATH を通してください。")
    folder = Path(a.folder)
    sj = folder / "scenes.json"
    if not sj.exists():
        die("scenes.json がありません。先に `plan` → `voice` を実行してください。")
    plan = load_json(sj)
    font = find_font(a.font)
    work = folder / "_work"
    (work / "clips").mkdir(parents=True, exist_ok=True)

    rec = prepare_recording(folder, plan["recording"], work) if plan.get("recording") else None
    scenes = [s for s in plan["scenes"] if a.scene is None or s["id"] == a.scene]
    if not scenes:
        die(f"場面 {a.scene} がありません。")

    clips = []
    for sc in scenes:
        for c in sc["chunks"]:
            if "duration" not in c or not (folder / c.get("audio", "x")).exists():
                die(f"カット {c['id']} の音声がありません。先に `voice` を実行してください。")
            D = c["duration"] + PAD_SEC
            wav = folder / c["audio"]
            out = work / "clips" / f"{c['id']}.mp4"
            src = c.get("source", "card")
            if src == "recording" and not rec:
                src = "image" if c.get("image") else "card"

            if src == "recording":
                s = max(c["start_ms"], 0) / 1000
                L = max((c["end_ms"] - c["start_ms"]) / 1000, 0.5)
                ratio = min(max(D / L, 0.6), 2.5)  # 映像の速さは 0.4〜1.7 倍の範囲で合わせる
                ov = work / f"ov_{c['id']}.png"
                render_overlay(ov, sc["heading"], c["text"], font)
                vh = PANEL_Y - HEAD_H
                filt = (
                    f"[0:v]setpts=(PTS-STARTPTS)*{ratio:.4f},fps=30,"
                    f"scale={W}:{vh}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:{HEAD_H}+({vh}-ih)/2:color=0x{BG[0]:02x}{BG[1]:02x}{BG[2]:02x},"
                    f"tpad=stop_mode=clone:stop_duration={D:.3f},trim=duration={D:.3f},setpts=PTS-STARTPTS[v];"
                    "[v][1:v]overlay=0:0:format=auto[o]"
                )
                cmd = ["ffmpeg", "-y", "-ss", f"{s:.3f}", "-t", f"{L:.3f}", "-i", str(rec),
                       "-loop", "1", "-framerate", "30", "-t", f"{D:.3f}", "-i", str(ov),
                       "-i", str(wav), "-filter_complex", filt, "-map", "[o]", "-map", "2:a",
                       "-af", f"apad=pad_dur={PAD_SEC}"]
            else:
                png = work / f"f_{c['id']}.png"
                img = folder / c["image"] if src == "image" and c.get("image") else None
                render_still(png, sc["heading"], c["text"], img, c.get("image_note"), font)
                cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-t", f"{D:.3f}", "-i", str(png),
                       "-i", str(wav), "-af", f"apad=pad_dur={PAD_SEC}"]
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(out)]
            run(cmd)
            clips.append(out)
        print(f"  場面{sc['id']} {sc['heading']} ✓")

    lst = work / "list.txt"
    lst.write_text("".join(f"file 'clips/{c.name}'\n" for c in clips), encoding="utf-8")
    default = f"video_scene{a.scene}.mp4" if a.scene is not None else "video.mp4"
    out = Path(a.out) if a.out else folder / default
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    print(f"完了: {out}（{len(clips)}カット）")


# ============================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="場面分けと字幕の下書き(scenes.json)を作る")
    p.add_argument("folder")
    p.add_argument("--max-chars", type=int, default=90, help="1カットの字幕の最大文字数（既定90）")
    p.add_argument("--ollama", metavar="MODEL", help="ローカルLLM(Ollama)で場面ごとに要約（例: qwen2.5:7b）")
    p.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    p.add_argument("--summary-sentences", type=int, default=3)
    p.add_argument("--prefer-images", action="store_true", help="画像のある場面は、録画でなく抽出した画像を表示")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("voice", help="字幕を読み上げ音声にする")
    p.add_argument("folder")
    p.add_argument("--speaker", type=int, default=3, help="VOICEVOXの話者ID（既定3）")
    p.add_argument("--voicevox", default="http://127.0.0.1:50021")
    p.add_argument("--no-voice", action="store_true", help="無音（字幕のみ）にする")
    p.set_defaults(fn=cmd_voice)

    p = sub.add_parser("render", help="録画＋文章を合成して動画にする")
    p.add_argument("folder")
    p.add_argument("--scene", type=int, help="この場面だけ作る（確認用）")
    p.add_argument("--out", help="出力mp4")
    p.add_argument("--font", help="日本語フォントのファイル")
    p.set_defaults(fn=cmd_render)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
