#!/usr/bin/env python3
"""
make_illustrations.py — 資料（絵画・写本など）が無い場面のイラストを、画像生成AIで作る。
Claude API は使わない。二段構え：PC内のStable Diffusion系 → だめならGemini（API）→ それも無ければ手動用の指示書。

  python make_illustrations.py prompts  <プロジェクト>      手動用の指示書(illustration_prompts.md)を書き出す
  python make_illustrations.py generate <プロジェクト> [--backend auto|a1111|comfyui|gemini]
                                                            [--chapter 3] [--only ill_ch03_b] [--force]
  python make_illustrations.py register <プロジェクト>      images/ に置いた画像を visuals.json に登録（手動生成した場合）

  プロジェクト/illustrations.json   各イラストの指示（chapter, file, ja=内容, prompt=英語, ai_video）

バックエンド
  a1111   Automatic1111 / Forge / SD.Next の API（--api で起動。既定 http://127.0.0.1:7860）
  comfyui ComfyUI（既定 http://127.0.0.1:8188）。--checkpoint にモデル名（例 sd_xl_base_1.0.safetensors）が必要
  gemini  Google Gemini の画像生成API。環境変数 GEMINI_API_KEY が必要（Google AI Studioで発行。
          ※ GeminiアプリのサブスクリプションとAPIキーは別物）
  auto    上から順に、使えるものを選ぶ

生成した画像は images/ に保存し、visuals.json の該当章に「AI生成イラスト」として登録します（出典表示用）。
実在の人物の肖像を再現するものではなく、場面のイメージ画です（動画の概要欄に「AI生成イラスト」と明記してください）。
"""
import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path


def die(m):
    print("エラー:", m, file=sys.stderr)
    sys.exit(1)


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def save_json(p, o):
    Path(p).write_text(json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")


def http_json(url, data=None, timeout=600, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def alive(url):
    try:
        urllib.request.urlopen(url, timeout=3).read()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- backends
def gen_a1111(host, prompt, negative, w, h, seed, a):
    d = http_json(f"{host}/sdapi/v1/txt2img", {
        "prompt": prompt, "negative_prompt": negative, "width": w, "height": h, "steps": a.steps,
        "cfg_scale": a.cfg, "sampler_name": a.sampler, "seed": seed, "batch_size": 1})
    return base64.b64decode(d["images"][0])


def gen_comfyui(host, prompt, negative, w, h, seed, a):
    if not a.checkpoint:
        die("ComfyUI では --checkpoint（モデルのファイル名）が必要です。")
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": a.checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed, "steps": a.steps, "cfg": a.cfg, "sampler_name": "euler", "scheduler": "normal",
            "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "wh"}},
    }
    pid = http_json(f"{host}/prompt", {"prompt": wf})["prompt_id"]
    for _ in range(600):
        time.sleep(2)
        hist = http_json(f"{host}/history/{pid}", timeout=30)
        if pid in hist:
            im = hist[pid]["outputs"]["7"]["images"][0]
            q = f"filename={im['filename']}&subfolder={im['subfolder']}&type={im['type']}"
            return urllib.request.urlopen(f"{host}/view?{q}", timeout=60).read()
    die("ComfyUI の生成が終わりませんでした。")


def gen_gemini(prompt, a):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        die("環境変数 GEMINI_API_KEY がありません。")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{a.gemini_model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}}}
    d = http_json(url, body, headers={"x-goog-api-key": key})
    for cand in d.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline:
                return base64.b64decode(inline["data"])
    die("Gemini から画像が返りませんでした（安全フィルタや無料枠の制限の可能性）: " + json.dumps(d, ensure_ascii=False)[:300])


def pick_backend(a):
    if a.backend != "auto":
        return a.backend
    if alive(f"{a.a1111}/sdapi/v1/options"):
        return "a1111"
    if a.checkpoint and alive(f"{a.comfyui}/system_stats"):
        return "comfyui"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    die("使える生成先がありません。Stable Diffusion(--api)を起動するか、GEMINI_API_KEY を設定するか、\n"
        "`prompts` で指示書を作って手動で生成してください。")


# ---------------------------------------------------------------- commands
def register(proj, items, note="AI生成イラスト"):
    vj = proj / "visuals.json"
    vis = load_json(vj) if vj.exists() else {}
    n = 0
    for it in items:
        if not (proj / it["file"]).exists():
            continue
        key = f"ch{int(it['chapter']):02d}"
        vis.setdefault(key, [])
        if not any(x["file"] == it["file"] for x in vis[key]):
            vis[key].append({"file": it["file"], "credit": note, "ai_generated": True, "ja": it.get("ja", "")})
            n += 1
    save_json(vj, vis)
    return n


def cmd_prompts(a):
    proj = Path(a.project)
    spec = load_json(proj / "illustrations.json")
    lines = ["# イラスト生成の指示書（手動用）", "",
             "Gemini / ChatGPT / Midjourney などに、下の「プロンプト」を貼って生成し、",
             "指定のファイル名で `images/` に保存してください。保存後に `register` を実行します。", "",
             f"- 共通の画風: {spec['style']}", f"- 避けたいもの: {spec['negative']}", "- 縦横比: 16:9（横長）", ""]
    for it in spec["items"]:
        mark = "（AI動画化の候補）" if it.get("ai_video") else ""
        lines += [f"## {it['id']}　第{it['chapter']}章　{it['ja']}{mark}", f"- 保存名: `{it['file']}`",
                  f"- プロンプト: {it['prompt']}, {spec['style']}", ""]
    out = proj / "illustration_prompts.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"書き出し: {out}（{len(spec['items'])}件）")


def cmd_generate(a):
    proj = Path(a.project)
    spec = load_json(proj / "illustrations.json")
    (proj / "images").mkdir(exist_ok=True)
    backend = pick_backend(a)
    w, h = (int(x) for x in (a.size or spec.get("size", "1344x768")).split("x"))
    todo = [it for it in spec["items"]
            if (a.chapter is None or it["chapter"] == a.chapter) and (not a.only or it["id"] == a.only)
            and (a.force or not (proj / it["file"]).exists())]
    print(f"生成先: {backend} / {len(todo)}件 / {w}x{h}")
    rnd = random.Random(a.seed)
    done = []
    for i, it in enumerate(todo, 1):
        prompt = f"{it['prompt']}, {spec['style']}"
        seed = rnd.randint(1, 2**31 - 1)
        print(f"  [{i}/{len(todo)}] {it['id']} {it['ja']}")
        try:
            if backend == "a1111":
                data = gen_a1111(a.a1111, prompt, spec["negative"], w, h, seed, a)
            elif backend == "comfyui":
                data = gen_comfyui(a.comfyui, prompt, spec["negative"], w, h, seed, a)
            else:
                data = gen_gemini(f"{prompt}. Avoid: {spec['negative']}.", a)
        except Exception as e:  # noqa: BLE001
            print(f"    失敗: {e}", file=sys.stderr)
            continue
        (proj / it["file"]).write_bytes(data)
        done.append(it)
    n = register(proj, done)
    print(f"完了: {len(done)}枚生成 / visuals.json に{n}件登録")
    print("次: script --force で各カットに割り当て → depth で奥行きを作る → render")


def cmd_register(a):
    proj = Path(a.project)
    n = register(proj, load_json(proj / "illustrations.json")["items"])
    print(f"visuals.json に{n}件登録しました。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompts"); p.add_argument("project"); p.set_defaults(fn=cmd_prompts)
    p = sub.add_parser("register"); p.add_argument("project"); p.set_defaults(fn=cmd_register)
    p = sub.add_parser("generate")
    p.add_argument("project")
    p.add_argument("--backend", choices=["auto", "a1111", "comfyui", "gemini"], default="auto")
    p.add_argument("--chapter", type=int); p.add_argument("--only"); p.add_argument("--force", action="store_true")
    p.add_argument("--size"); p.add_argument("--seed", type=int, default=7)
    p.add_argument("--steps", type=int, default=28); p.add_argument("--cfg", type=float, default=6.5)
    p.add_argument("--sampler", default="DPM++ 2M Karras")
    p.add_argument("--a1111", default="http://127.0.0.1:7860")
    p.add_argument("--comfyui", default="http://127.0.0.1:8188")
    p.add_argument("--checkpoint", help="ComfyUI のモデルファイル名")
    p.add_argument("--gemini-model", default="gemini-2.5-flash-image")
    p.set_defaults(fn=cmd_generate)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
