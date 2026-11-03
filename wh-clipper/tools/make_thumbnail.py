#!/usr/bin/env python3
"""サムネイル生成（1280x720）。仕様は JSON で渡す。

  python make_thumbnail.py spec.json [--out thumb.png] [--font フォント.ttf]

spec.json の例は wh-work/thumbnails/ を参照。
  kicker : 上の小さな見出し（フック）
  main   : 中央の大きな文字列。[[ ]] で囲んだ部分は強調色
  sub    : 主題の下の小さめの文字列（省略可）
  accent : 強調色（#RRGGBB）／sub_color : sub の色
  bg     : 背景画像（省略時は暗い単色グラデーション。画像は暗く・ぼかして使う）
  images : [{"file":"x.jpg","box":[x,y,w,h],"rot":-3,"label":"…"}]  周囲に並べる歴史画像。
           file が無い場合は、label を書いた枠（差し替え用の仮置き）を描く
"""
import argparse, json, os, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1280, 720
FONTS = [
    r"C:\Windows\Fonts\YuGothB.ttc", r"C:\Windows\Fonts\meiryob.ttc", r"C:\Windows\Fonts\meiryo.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
]


def find_font(p):
    for c in ([p] if p else []) + FONTS:
        if c and os.path.exists(c):
            return c
    sys.exit("日本語フォントが見つかりません。--font で指定してください。")


def hexrgb(s, default=(255, 220, 60)):
    try:
        s = s.lstrip("#"); return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


def segments(text):
    """[[強調]] を分割して [(文字列, 強調か)] にする"""
    out, i = [], 0
    while i < len(text):
        a = text.find("[[", i)
        if a < 0:
            out.append((text[i:], False)); break
        if a > i:
            out.append((text[i:a], False))
        b = text.find("]]", a)
        if b < 0:
            out.append((text[a:], False)); break
        out.append((text[a + 2:b], True)); i = b + 2
    return out


def fit_font(path, draw, text, max_w, start, stroke):
    size = start
    while size > 20:
        f = ImageFont.truetype(path, size)
        if draw.textlength(text, font=f) + stroke * 2 <= max_w:
            return f
        size -= 4
    return ImageFont.truetype(path, 20)


def outlined(d, xy, text, font, fill, stroke, outline=(15, 15, 25)):
    d.text(xy, text, font=font, fill=fill, stroke_width=stroke, stroke_fill=outline)


def background(spec, folder):
    if spec.get("bg") and (folder / spec["bg"]).exists():
        im = Image.open(folder / spec["bg"]).convert("RGB")
        s = max(W / im.width, H / im.height)
        im = im.resize((int(im.width * s) + 1, int(im.height * s) + 1))
        im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
        im = im.filter(ImageFilter.GaussianBlur(6))
        return Image.blend(im, Image.new("RGB", (W, H), (10, 12, 25)), 0.45)
    im = Image.new("RGB", (W, H))
    px = im.load()
    for y in range(H):
        for x in range(0, W):
            t = (y / H) * 0.6 + (x / W) * 0.4
            px[x, y] = (int(28 + 30 * t), int(36 + 34 * t), int(66 + 40 * t))
    return im


def paste_image(base, spec_img, folder, font_path):
    x, y, w, h = spec_img["box"]
    rot = spec_img.get("rot", 0)
    f = folder / spec_img["file"] if spec_img.get("file") else None
    if f and f.exists():
        pic = Image.open(f).convert("RGB")
        s = max(w / pic.width, h / pic.height)
        pic = pic.resize((int(pic.width * s) + 1, int(pic.height * s) + 1))
        pic = pic.crop(((pic.width - w) // 2, (pic.height - h) // 2, (pic.width - w) // 2 + w, (pic.height - h) // 2 + h))
    else:  # 仮置き枠
        pic = Image.new("RGB", (w, h), (70, 78, 98))
        d = ImageDraw.Draw(pic)
        d.rectangle((0, 0, w - 1, h - 1), outline=(150, 160, 180), width=3)
        lab = spec_img.get("label", "画像")
        fnt = ImageFont.truetype(font_path, 20)
        yy = 10
        cur = ""
        for ch in lab + " ":
            if d.textlength(cur + ch, font=fnt) > w - 20 and cur:
                d.text((10, yy), cur, font=fnt, fill=(225, 230, 240)); yy += 24; cur = ch
            else:
                cur += ch
        d.text((10, yy), cur, font=fnt, fill=(225, 230, 240))
    framed = Image.new("RGBA", (w + 14, h + 14), (255, 255, 255, 255))
    framed.paste(pic, (7, 7))
    framed = framed.rotate(rot, expand=True, resample=Image.BICUBIC)
    shadow = Image.new("RGBA", framed.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), mask=framed.split()[3])
    base.paste(shadow, (x + 6, y + 8), shadow)
    base.paste(framed, (x, y), framed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec"); ap.add_argument("--out"); ap.add_argument("--font")
    a = ap.parse_args()
    sp = Path(a.spec); spec = json.loads(sp.read_text(encoding="utf-8")); folder = sp.parent
    fp = find_font(a.font)
    accent = hexrgb(spec.get("accent", "#FFD83C")); sub_c = hexrgb(spec.get("sub_color", "#FF4A3C"))
    im = background(spec, folder).convert("RGBA")
    for si in spec.get("images", []):
        paste_image(im, si, folder, fp)
    d = ImageDraw.Draw(im)

    k = spec.get("kicker")
    if k:
        f = fit_font(fp, d, k, W - 360, 46, 6)
        tw = d.textlength(k, font=f)
        outlined(d, ((W - tw) // 2, 34), k, f, (255, 255, 255), 6)

    segs = segments(spec["main"]); plain = "".join(s for s, _ in segs)
    f = fit_font(fp, d, plain, W - 120, 190, 12)
    tw = d.textlength(plain, font=f); x = (W - tw) // 2; y = spec.get("main_y", 250)
    for s, hl in segs:
        outlined(d, (x, y), s, f, accent if hl else (255, 255, 255), 12)
        x += d.textlength(s, font=f)

    sub = spec.get("sub")
    if sub:
        f2 = fit_font(fp, d, sub, W - 160, 110, 10)
        outlined(d, ((W - d.textlength(sub, font=f2)) // 2, spec.get("sub_y", 480)), sub, f2, sub_c, 10)

    out = Path(a.out) if a.out else sp.with_suffix(".png")
    im.convert("RGB").save(out)
    print("保存:", out)


if __name__ == "__main__":
    main()
