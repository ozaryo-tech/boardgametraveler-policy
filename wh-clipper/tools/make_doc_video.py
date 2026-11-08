#!/usr/bin/env python3
"""
make_doc_video.py — 台本(md)から、歴史画像＋動きの演出のドキュメンタリー風動画(mp4)を作る。
Claude API は使わない。画面録画は使わず、パブリックドメインの絵画・写本・地図などを、
ゆっくりした拡大・移動（ケンバーンズ）、場面転換、字幕、左下注記、BGM で見せる。

  1) script  <台本フォルダ> <プロジェクト>   台本(ch*.md) → scenes.json / visuals.json
  2) images  search "<語>" / get "File:○○.jpg" --project <P> --chapter 3
             Wikimedia Commons から画像を探して取り込む（出典・権利は visuals.json に記録）
  2b) depth  <プロジェクト>                   絵ごとの奥行き画像を作る（2.5Dの立体的な動き用）
  3) voice   <プロジェクト>                   読み上げ音声（make_video.py の voice と同じ。VOICEVOX / 無音）
  4) render  <プロジェクト> [--chapter 3] [--preview] [--bgm 曲.mp3]

  プロジェクトの中:
    scenes.json   場面(章)とカット。text / note(左下注記) / visual(画像) / motion を手で直せる
    visuals.json  章ごとの画像の一覧（file, credit, commons, caption）
    images/       画像ファイル

必要なもの: ffmpeg, Pillow（pip install pillow）。画像取得にはインターネット接続。
"""
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_video as mv  # noqa: E402  (chunk_text / voice / find_font を再利用)

FPS = 30
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
XF = 0.5          # カット間のクロスフェード秒
GAP = 0.35        # 同じ画像の中で字幕が切り替わる間
MOTIONS = ["zoom_in", "pan_right", "zoom_out", "pan_left"]
UA = "wh-clipper/0.2 (personal study project; contact: ozaryo2000@gmail.com)"
OK_LICENSES = ("public domain", "pd", "cc0", "cc by", "cc-by", "cc by-sa", "cc-by-sa")


def die(m):
    print("エラー:", m, file=sys.stderr)
    sys.exit(1)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"ffmpeg が失敗しました:\n{' '.join(map(str, cmd))[:400]}\n{r.stderr[-1500:]}")
    return r


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def save_json(p, o):
    Path(p).write_text(json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================ 1) script
def parse_chapter(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    head = re.sub(r"[（(][^）)]*[）)]\s*$", "", lines[0].lstrip("# ").strip()).strip()
    sec, narr, notes = None, [], []
    for ln in lines[1:]:
        if ln.startswith("## "):
            sec = ln[3:].strip()
            continue
        if sec == "ナレーション":
            narr.append(ln)
        elif sec == "左下注記" and ln.startswith("- ") and "→" in ln:
            k, v = ln[2:].split("→", 1)
            notes.append((k.strip(), v.strip()))
    paras = [p.replace("\n", "") for p in "\n".join(narr).split("\n\n") if p.strip()]
    return {"heading": head, "paras": paras, "notes": notes}


def grams(s, n=3):
    s = re.sub(r"[\s「」『』・／/＝→（）()、。]", "", s)
    return {s[i:i + n] for i in range(max(len(s) - n + 1, 0))} or {s}


def assign_notes(chunks, notes):
    for key, note in notes:
        kg = grams(key)
        best, score = 0, -1
        for i, c in enumerate(chunks):
            sc = len(kg & grams(c["text"]))
            if sc > score:
                best, score = i, sc
        c = chunks[best]
        c["note"] = (c["note"] + "　" if c.get("note") else "") + note


def cmd_script(a):
    src, proj = Path(a.scripts), Path(a.project)
    files = sorted(src.glob("ch*.md"))
    if not files:
        die(f"{src} に ch*.md がありません。")
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "images").mkdir(exist_ok=True)
    sj, vj = proj / "scenes.json", proj / "visuals.json"
    if sj.exists() and not a.force:
        die("scenes.json は既にあります（手で直した内容を守るため上書きしません）。作り直すなら --force。")
    vis = load_json(vj) if vj.exists() else {}
    scenes = []
    for i, f in enumerate(files, 1):
        ch = parse_chapter(f)
        key = f"ch{i:02d}"
        vis.setdefault(key, [])
        imgs = vis[key]
        chunks = []
        for p in ch["paras"]:
            for t in mv.chunk_text(p, a.max_chars):
                chunks.append({"id": f"{i}-{len(chunks) + 1}", "text": t})
        assign_notes(chunks, ch["notes"])
        for j, c in enumerate(chunks):
            if imgs:
                im = imgs[min(j * len(imgs) // len(chunks), len(imgs) - 1)]
                c["visual"] = im["file"]
                if im.get("credit"):
                    c["credit"] = im["credit"]
            else:
                c["visual"] = None
            c["motion"] = MOTIONS[(i + j) % len(MOTIONS)]
        scenes.append({"id": i, "heading": ch["heading"], "chunks": chunks})
        print(f"  {key} {ch['heading']}: {len(chunks)}カット / 画像{len(imgs)}枚")
    save_json(sj, {"title": a.title or proj.name, "scenes": scenes})
    vis["_説明"] = "章(ch01…)ごとに画像を並べる。file は images/ からの相対パス。credit は画面右下に小さく表示。"
    save_json(vj, vis)
    n = sum(len(s["chunks"]) for s in scenes)
    print(f"作成: {sj}（{len(scenes)}章 / {n}カット）")
    print("次: images で画像を集め、visuals.json に追加 → もう一度 `script --force`（または scenes.json の visual を直接編集）")


# ============================================================ 2) images
def api_get(url, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < retries - 1:
                wait = int(e.headers.get("Retry-After", "15") or 15)
                print(f"  混雑のため{wait}秒待ちます…", file=sys.stderr)
                time.sleep(max(wait, 5))
                continue
            raise
    raise RuntimeError("取得に失敗しました")


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def commons_api(params):
    q = urllib.parse.urlencode({**params, "format": "json", "formatversion": 2})
    last = None
    for host in ("commons.wikimedia.org", "en.wikipedia.org"):
        try:
            return json.loads(api_get(f"https://{host}/w/api.php?{q}"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def file_info(title):
    d = commons_api({"action": "query", "titles": title, "prop": "imageinfo",
                     "iiprop": "url|extmetadata|size|mime"})
    p = d["query"]["pages"][0]
    if "imageinfo" not in p:
        die(f"{title} が見つかりません。")
    ii = p["imageinfo"][0]
    em = ii.get("extmetadata", {})
    return {
        "title": p["title"], "url": ii["url"], "page": ii.get("descriptionurl"),
        "w": ii.get("width"), "h": ii.get("height"), "mime": ii.get("mime"),
        "license": em.get("LicenseShortName", {}).get("value", "不明"),
        "artist": strip_tags(em.get("Artist", {}).get("value")),
        "desc": strip_tags(em.get("ImageDescription", {}).get("value"))[:100],
    }


def cmd_images_entry(a):
    if a.sub == "get" and not (a.project and a.chapter):
        die("get には --project と --chapter が必要です。")
    cmd_images(a)


def cmd_images(a):
    if a.sub == "search":
        d = commons_api({"action": "query", "generator": "search", "gsrsearch": a.query, "gsrnamespace": 6,
                         "gsrlimit": a.limit, "prop": "imageinfo", "iiprop": "extmetadata|size"})
        for p in sorted(d.get("query", {}).get("pages", []), key=lambda x: x.get("index", 0)):
            ii = (p.get("imageinfo") or [{}])[0]
            em = ii.get("extmetadata", {})
            lic = em.get("LicenseShortName", {}).get("value", "不明")
            print(f"{p['title']}\n    {ii.get('width')}x{ii.get('height')}  {lic}  {strip_tags(em.get('Artist', {}).get('value'))[:40]}")
        print("\n使う画像は:  images get \"File:○○.jpg\" --project <P> --chapter N")
        return
    info = file_info(a.query)
    lic = info["license"]
    print(f"{info['title']}  {info['w']}x{info['h']}  権利: {lic}  作者: {info['artist'][:40]}")
    if not any(k in lic.lower() for k in OK_LICENSES) and not a.force_license:
        die(f"権利表示が「{lic}」です。使えるか確認できないため取り込みません（確認した上で強制する場合は --force-license）。")
    proj = Path(a.project)
    (proj / "images").mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\-]+", "_", info["title"].replace("File:", ""))
    dst = proj / "images" / name
    if not dst.exists():
        dst.write_bytes(api_get(info["url"]))
        print("保存:", dst)
    vj = proj / "visuals.json"
    vis = load_json(vj) if vj.exists() else {}
    key = f"ch{int(a.chapter):02d}"
    credit = a.credit or f"{info['artist'][:30] or '作者不明'}『{info['desc'][:24] or info['title'][5:30]}』（{lic}）"
    vis.setdefault(key, [])
    if not any(x["file"] == f"images/{name}" for x in vis[key]):
        vis[key].append({"file": f"images/{name}", "commons": info["title"], "credit": credit, "page": info["page"]})
    save_json(vj, vis)
    print(f"{key} に追加しました。反映するには `script --force`（または scenes.json の visual を編集）。")


# ============================================================ 4) render
class Style:
    def __init__(self, preview, font, parallax=True):
        self.parallax = parallax
        self.W, self.H = (1280, 720) if preview else (1920, 1080)
        self.k = self.W / 1920
        self.font = font

    def f(self, size):
        return ImageFont.truetype(self.font, max(int(size * self.k), 8))


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


def texture_bg(w, h, seed):
    """画像が無い場面用の、古い紙・地図台のような背景（無地の文字カードにしない）"""
    import random
    random.seed(seed)
    noise = Image.effect_noise((w // 4, h // 4), 40).resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
    base = Image.merge("RGB", [noise.point(lambda v, m=m: int(v * m)) for m in (0.55, 0.45, 0.32)])
    base = ImageChops.add(base, Image.new("RGB", (w, h), (38, 30, 22)))
    return base


def vignette(im, strength=0.55):
    w, h = im.size
    g = Image.radial_gradient("L").resize((w, h), Image.BICUBIC)  # 中心0→外側255
    mask = g.point(lambda v: int(255 - v * strength))
    return ImageChops.multiply(im, Image.merge("RGB", [mask] * 3))


def make_base(visual, folder, st, seed):
    """動かすための元画像（出力の1.5倍）。16:9に近い絵は全面に、それ以外はぼかし背景の上に配置。"""
    bw, bh = int(st.W * 1.5), int(st.H * 1.5)
    src = folder / visual if visual else None
    if not (src and src.exists()):
        return vignette(texture_bg(bw, bh, seed))
    pic = Image.open(src).convert("RGB")
    ar, tar = pic.width / pic.height, bw / bh
    if abs(ar - tar) / tar < 0.22:  # ほぼ16:9 → 全面
        s = max(bw / pic.width, bh / pic.height)
        pic = pic.resize((int(pic.width * s) + 1, int(pic.height * s) + 1), Image.LANCZOS)
        l, t = (pic.width - bw) // 2, (pic.height - bh) // 2
        return vignette(pic.crop((l, t, l + bw, t + bh)), 0.4)
    s = max(bw / pic.width, bh / pic.height)
    bg = pic.resize((int(pic.width * s) + 1, int(pic.height * s) + 1))
    l, t = (bg.width - bw) // 2, (bg.height - bh) // 2
    bg = bg.crop((l, t, l + bw, t + bh)).filter(ImageFilter.GaussianBlur(40 * st.k + 10))
    bg = Image.blend(bg, Image.new("RGB", (bw, bh), (8, 8, 12)), 0.55)
    fs = min(bw * 0.86 / pic.width, bh * 0.86 / pic.height)
    fg = pic.resize((max(int(pic.width * fs), 1), max(int(pic.height * fs), 1)), Image.LANCZOS)
    x, y = (bw - fg.width) // 2, (bh - fg.height) // 2
    sh = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rectangle((x + 14, y + 18, x + fg.width + 14, y + fg.height + 18), fill=(0, 0, 0, 150))
    sh = sh.filter(ImageFilter.GaussianBlur(24))
    bg = bg.convert("RGBA")
    bg.alpha_composite(sh)
    bg.paste(fg, (x, y))
    return vignette(bg.convert("RGB"), 0.45)


def overlay_text(path, st, subtitle, note=None, credit=None, tag=None):
    im = Image.new("RGBA", (st.W, st.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    band = int(300 * st.k)
    for y in range(band):  # 下から上へ消えるグラデーション
        a = int(190 * (y / band) ** 1.4)
        d.line((0, st.H - band + y, st.W, st.H - band + y), fill=(0, 0, 0, a))
    if tag:
        ft = st.f(34)
        tw = d.textlength(tag, font=ft)
        d.rounded_rectangle((int(40 * st.k), int(30 * st.k), int(40 * st.k) + tw + int(40 * st.k), int(30 * st.k) + int(60 * st.k)),
                            radius=int(14 * st.k), fill=(0, 0, 0, 140))
        d.text((int(60 * st.k), int(38 * st.k)), tag, font=ft, fill=(240, 200, 96))
    fs = st.f(46)
    lines = wrap(d, subtitle, fs, st.W - int(260 * st.k))
    lh = int(64 * st.k)
    y = st.H - int(100 * st.k) - lh * len(lines)
    for ln in lines:
        tw = d.textlength(ln, font=fs)
        d.text(((st.W - tw) / 2, y), ln, font=fs, fill=(255, 255, 255), stroke_width=max(int(5 * st.k), 2), stroke_fill=(0, 0, 0))
        y += lh
    if note:
        fn = st.f(26)
        txt = note
        while len(txt) > 1 and d.textlength(txt, font=fn) > st.W - int(80 * st.k) - (int(660 * st.k) if credit else 0):
            txt = txt[:-2] + "…"
        d.text((int(40 * st.k), st.H - int(46 * st.k)), txt, font=fn, fill=(190, 198, 212), stroke_width=1, stroke_fill=(0, 0, 0))
    if credit:
        fc = st.f(22)
        txt = "画像: " + credit
        while len(txt) > 1 and d.textlength(txt, font=fc) > int(640 * st.k):
            txt = txt[:-2] + "…"
        d.text((st.W - int(40 * st.k) - d.textlength(txt, font=fc), st.H - int(42 * st.k)), txt, font=fc, fill=(160, 168, 182))
    im.save(path)


def overlay_title(path, st, heading):
    im = Image.new("RGBA", (st.W, st.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, int(st.H * 0.36), st.W, int(st.H * 0.36) + int(190 * st.k)), fill=(0, 0, 0, 130))
    ft = st.f(84)
    while d.textlength(heading, font=ft) > st.W - int(160 * st.k) and ft.size > 30:
        ft = ImageFont.truetype(st.font, ft.size - 4)
    d.text(((st.W - d.textlength(heading, font=ft)) / 2, int(st.H * 0.36) + int(45 * st.k)), heading, font=ft,
           fill=(240, 200, 96), stroke_width=max(int(5 * st.k), 2), stroke_fill=(0, 0, 0))
    im.save(path)


def merge_wavs(paths, starts, total, out):
    """各音声を、指定の開始秒に置いて1本の wav にする（無音で埋める）。"""
    with wave.open(str(paths[0]), "rb") as w0:
        nch, sw, rate = w0.getnchannels(), w0.getsampwidth(), w0.getframerate()
    buf = bytearray(b"\x00" * (int(total * rate) * nch * sw))
    for p, s in zip(paths, starts):
        with wave.open(str(p), "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (nch, sw, rate):
                die(f"音声の形式が揃っていません: {p}")
            data = w.readframes(w.getnframes())
        off = int(s * rate) * nch * sw
        buf[off:off + len(data)] = data[: max(len(buf) - off, 0)]
    with wave.open(str(out), "wb") as w:
        w.setnchannels(nch); w.setsampwidth(sw); w.setframerate(rate)
        w.writeframes(bytes(buf))


def wav_sec(p):
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / w.getframerate()


def motion_expr(motion, n, bw=None):
    n = max(n, 2)
    if motion == "zoom_out":
        return f"1.12-0.12*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "pan_right":
        return "1.12", f"(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"
    if motion == "pan_left":
        return "1.12", f"(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"
    return f"1+0.12*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"


def depth_path(img):
    return img.with_name(img.stem + ".depth.png")


def near_16x9(path):
    with Image.open(path) as im:
        ar = im.width / im.height
    return abs(ar - 16 / 9) / (16 / 9) < 0.22


def cover_pair(img_path, dep_path, st):
    """画像と奥行き画像を、同じ範囲で16:9に切り抜く（視差用）。"""
    bw, bh = int(st.W * 1.5), int(st.H * 1.5)
    pic = Image.open(img_path).convert("RGB")
    dep = Image.open(dep_path).convert("L").resize(pic.size, Image.BICUBIC)
    s = max(bw / pic.width, bh / pic.height)
    nw, nh = int(pic.width * s) + 1, int(pic.height * s) + 1
    l, t = (nw - bw) // 2, (nh - bh) // 2
    pic = pic.resize((nw, nh), Image.LANCZOS).crop((l, t, l + bw, t + bh))
    dep = dep.resize((nw, nh), Image.BICUBIC).crop((l, t, l + bw, t + bh))
    return vignette(pic, 0.4), dep


def clip_seconds(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def build_shots(scene):
    """同じ画像が続くカットを1つのショットにまとめる（動きが途中で途切れないように）。"""
    shots = []
    for c in scene["chunks"]:
        if shots and shots[-1]["visual"] == c.get("visual") and c.get("visual") is not None:
            shots[-1]["chunks"].append(c)
        else:
            shots.append({"visual": c.get("visual"), "motion": c.get("motion", "zoom_in"), "chunks": [c]})
    return shots


def render_shot(folder, work, st, scene, shot, idx, first_in_scene, tag):
    cs = shot["chunks"]
    starts, t = [], XF
    for c in cs:
        starts.append(t)
        t += c["duration"] + GAP
    L = t - GAP + XF
    N = int(math.ceil(L * FPS))
    vis = folder / shot["visual"] if shot["visual"] else None
    mode = "kb"
    if vis and vis.exists() and vis.suffix.lower() in VIDEO_EXT:
        mode = "video"
    elif vis and vis.exists() and st.parallax and depth_path(vis).exists() and near_16x9(vis):
        mode = "par"
    base = work / f"base_{scene['id']}_{idx}.png"
    src0 = base
    if mode == "par":
        from parallax import write_parallax
        pic, dep = cover_pair(vis, depth_path(vis), st)
        pic.save(base); dep.save(work / f"depth_{scene['id']}_{idx}.png")
        src0 = work / f"par_{scene['id']}_{idx}.mp4"
        write_parallax(base, work / f"depth_{scene['id']}_{idx}.png", src0, L, st.W, st.H, FPS, shot["motion"])
    elif mode == "kb":
        make_base(shot["visual"], folder, st, seed=scene["id"] * 100 + idx).save(base)
    wav = work / f"shot_{scene['id']}_{idx}.wav"
    merge_wavs([folder / c["audio"] for c in cs], starts, L, wav)
    ovs, filt_in, enables = [], [], []
    for j, c in enumerate(cs):
        p = work / f"ov_{scene['id']}_{idx}_{j}.png"
        overlay_text(p, st, c["text"], c.get("note"), c.get("credit"), tag)
        a = starts[j] - (GAP / 2 if j else XF)
        b = starts[j] + c["duration"] + (GAP / 2 if j < len(cs) - 1 else XF)
        ovs.append(p); enables.append((max(a, 0), b))
    title = None
    if first_in_scene:
        title = work / f"title_{scene['id']}.png"
        overlay_title(title, st, scene["heading"])
    z, x, y = motion_expr(shot["motion"], N)
    cmd = ["ffmpeg", "-y"]
    if mode == "video":
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(src0 if mode != "video" else vis), "-i", str(wav)]
    for p in ovs:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{L:.3f}", "-i", str(p)]
    if title:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{L:.3f}", "-i", str(title)]
    if mode == "kb":
        chain = [f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={N}:s={st.W}x{st.H}:fps={FPS},format=yuv420p[v0]"]
    elif mode == "par":
        chain = ["[0:v]format=yuv420p[v0]"]
    else:  # AI動画などの素材：短ければ少しゆっくりにして、足りなければ繰り返す
        cd = clip_seconds(vis)
        slow = min(L / cd, 1.8) if 0 < cd < L else 1.0
        chain = [f"[0:v]scale={st.W}:{st.H}:force_original_aspect_ratio=increase,crop={st.W}:{st.H},"
                 f"setpts=PTS*{slow:.3f},fps={FPS},format=yuv420p[v0]"]
    cur = "v0"
    for j, (a, b) in enumerate(enables):
        chain.append(f"[{cur}][{j + 2}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})':format=auto[v{j + 1}]")
        cur = f"v{j + 1}"
    if title:
        ti = len(ovs) + 2
        chain.append(f"[{ti}:v]format=rgba,fade=t=in:st=0.5:d=0.5:alpha=1,fade=t=out:st=2.6:d=0.5:alpha=1[tt]")
        chain.append(f"[{cur}][tt]overlay=0:0:format=auto[vt]")
        cur = "vt"
    out = work / f"shot_{scene['id']}_{idx}.mp4"
    cmd += ["-filter_complex", ";".join(chain), "-map", f"[{cur}]", "-map", "1:a", "-t", f"{L:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-ar", "48000", "-ac", "2", str(out)]
    run(cmd)
    return out, L


def join_shots(clips, lengths, out, fade_io=0.6):
    """ショット同士をクロスフェードでつなぎ、章の頭と終わりを黒からフェード。"""
    if len(clips) == 1:
        total = lengths[0]
        fc = f"[0:v]fade=t=in:st=0:d={fade_io},fade=t=out:st={total - fade_io:.3f}:d={fade_io}[v];[0:a]afade=t=in:d={fade_io},afade=t=out:st={total - fade_io:.3f}:d={fade_io}[a]"
    else:
        parts, acc = [], lengths[0]
        vcur, acur = "0:v", "0:a"
        for k in range(1, len(clips)):
            off = acc - XF
            parts.append(f"[{vcur}][{k}:v]xfade=transition=fade:duration={XF}:offset={off:.3f}[vx{k}]")
            parts.append(f"[{acur}][{k}:a]acrossfade=d={XF}[ax{k}]")
            vcur, acur = f"vx{k}", f"ax{k}"
            acc = off + lengths[k]
        total = acc
        parts.append(f"[{vcur}]fade=t=in:st=0:d={fade_io},fade=t=out:st={total - fade_io:.3f}:d={fade_io}[v]")
        parts.append(f"[{acur}]afade=t=in:d={fade_io},afade=t=out:st={total - fade_io:.3f}:d={fade_io}[a]")
        fc = ";".join(parts)
    cmd = ["ffmpeg", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-ar", "48000", "-ac", "2", str(out)]
    run(cmd)
    return total


def cmd_voice(a):
    ns = argparse.Namespace(folder=a.project, speaker=a.speaker, voicevox=a.voicevox, no_voice=a.no_voice)
    mv.cmd_voice(ns)


def cmd_depth(a):
    """images/ の絵ごとに奥行き画像（○○.depth.png）を作る。明るい=手前。"""
    folder = Path(a.project) / "images"
    imgs = [p for p in sorted(folder.iterdir()) if p.suffix.lower() in IMG_EXT and ".depth" not in p.name]
    if not imgs:
        die(f"{folder} に画像がありません。")
    pipe = None
    if a.method == "model":
        try:
            from transformers import pipeline
            print("奥行き推定モデルを読み込み中…（初回はダウンロードあり）")
            pipe = pipeline("depth-estimation", model=a.model)
        except Exception as e:  # noqa: BLE001
            die("奥行き推定モデルを使えません（pip install transformers torch が必要）。\n"
                f"  {e}\n  簡易版で作るなら --method gradient（画面の下ほど手前とみなす）")
    for p in imgs:
        out = depth_path(p)
        if out.exists() and not a.force:
            continue
        im = Image.open(p).convert("RGB")
        if pipe:
            d = pipe(im)["depth"].convert("L")
        else:
            from parallax import gradient_depth
            import numpy as np
            d = Image.fromarray(gradient_depth(im.width, im.height))
        d.save(out)
        print("作成:", out.name)
    print("次: render で自動的に使われます（--no-parallax で無効化）。")


def cmd_render(a):
    if not shutil.which("ffmpeg"):
        die("ffmpeg が見つかりません。インストールして PATH を通してください。")
    folder = Path(a.project)
    plan = load_json(folder / "scenes.json")
    st = Style(a.preview, mv.find_font(a.font), not a.no_parallax)
    work = folder / ("_work_preview" if a.preview else "_work")
    work.mkdir(exist_ok=True)
    scenes = [s for s in plan["scenes"] if a.chapter is None or s["id"] == a.chapter]
    if not scenes:
        die(f"章 {a.chapter} がありません。")
    for s in scenes:
        for c in s["chunks"]:
            if "duration" not in c or not (folder / c.get("audio", "x")).exists():
                die(f"カット {c['id']} の音声がありません。先に `voice` を実行してください（--no-voice で無音）。")
            v = c.get("visual")
            if v and not (folder / v).exists():
                print(f"※ 画像が見つかりません: {v}（背景の質感で代用）", file=sys.stderr)
    chapters = []
    for sc in scenes:
        clips, lens = [], []
        for i, sh in enumerate(build_shots(sc)):
            clip, L = render_shot(folder, work, st, sc, sh, i, i == 0, sc["heading"])
            clips.append(clip); lens.append(L)
        out = work / f"chapter_{sc['id']}.mp4"
        total = join_shots(clips, lens, out)
        chapters.append(out)
        print(f"  {sc['heading']} ✓ （{total:.0f}秒）")
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in chapters), encoding="utf-8")
    name = f"chapter{a.chapter}" if a.chapter is not None else "video"
    out = Path(a.out) if a.out else folder / (name + ("_preview" if a.preview else "") + ".mp4")
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    if a.bgm:
        bgm = Path(a.bgm)
        if not bgm.exists():
            die(f"BGM が見つかりません: {bgm}")
        fc = (f"[1:a]volume={a.bgm_volume},afade=t=in:d=2[b];"
              "[b][0:a]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=400[bd];"
              "[0:a][bd]amix=inputs=2:duration=first:normalize=0[a]")
        run(["ffmpeg", "-y", "-i", str(joined), "-stream_loop", "-1", "-i", str(bgm), "-filter_complex", fc,
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)])
    else:
        shutil.copy(joined, out)
    cr = []
    for s in scenes:
        for c in s["chunks"]:
            if c.get("credit") and c["credit"] not in cr:
                cr.append(c["credit"])
    if cr:
        (out.parent / (out.stem + "_credits.txt")).write_text("使用画像:\n" + "\n".join("・" + x for x in cr) + "\n", encoding="utf-8")
    print(f"完了: {out}")


# ============================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("script", help="台本(ch*.md)から scenes.json を作る")
    p.add_argument("scripts"); p.add_argument("project")
    p.add_argument("--max-chars", type=int, default=70)
    p.add_argument("--title")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_script)

    p = sub.add_parser("images", help="Wikimedia Commons の画像を探す／取り込む")
    p.add_argument("sub", choices=["search", "get"])
    p.add_argument("query", help="search: 検索語 / get: File:○○.jpg")
    p.add_argument("--project"); p.add_argument("--chapter", type=int)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--credit")
    p.add_argument("--force-license", action="store_true")
    p.set_defaults(fn=cmd_images_entry)

    p = sub.add_parser("depth", help="絵ごとに奥行き画像を作る（立体的な動き用）")
    p.add_argument("project")
    p.add_argument("--method", choices=["model", "gradient"], default="model")
    p.add_argument("--model", default="depth-anything/Depth-Anything-V2-Small-hf")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_depth)

    p = sub.add_parser("voice", help="読み上げ音声を作る")
    p.add_argument("project")
    p.add_argument("--speaker", type=int, default=3)
    p.add_argument("--voicevox", default="http://127.0.0.1:50021")
    p.add_argument("--no-voice", action="store_true")
    p.set_defaults(fn=cmd_voice)

    p = sub.add_parser("render", help="動画を作る")
    p.add_argument("project")
    p.add_argument("--chapter", type=int, help="この章だけ作る（確認用）")
    p.add_argument("--preview", action="store_true", help="720pの軽量プレビュー")
    p.add_argument("--no-parallax", action="store_true", help="奥行きによる立体的な動きを使わない（単純な拡大・移動のみ）")
    p.add_argument("--bgm", help="BGMの音声ファイル（mp3/wav）")
    p.add_argument("--bgm-volume", type=float, default=0.12)
    p.add_argument("--font", help="日本語フォントのファイル（太めがおすすめ）")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_render)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
