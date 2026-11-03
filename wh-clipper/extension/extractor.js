// ページ内に注入されるスクリプト。
// ・サイトごとの抽出ルール（アダプタ）で文章・画像を順序つきで取り出す
// ・「本文範囲の手動指定」で保存された設定があれば、それを優先する
// ・各要素が画面に現れた時刻を記録する（録画との対応づけ用）
(() => {
  if (window.__whClipper) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const state = { ranges: [], blocks: [], seen: {}, timers: [] };
  const norm = (s) => s.replace(/[\s　]+/g, ' ').trim();
  const MIN_IMG = 100; // これより小さい画像（ロゴ・アイコン）は無視

  // どのサイトでも除外するもの
  const BASE_SKIP =
    '.non_print, .subTable, nav, header, footer, script, style, noscript, form, iframe, [hidden], ' +
    '#go-print, button, select, textarea, input, svg, link';
  const BLOCK_TAGS = new Set([
    'P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'UL', 'OL', 'TABLE', 'TR', 'TD', 'TH',
    'FIGURE', 'FIGCAPTION', 'SECTION', 'ARTICLE', 'BLOCKQUOTE', 'DL', 'DT', 'DD', 'PRE', 'CAPTION'
  ]);

  // ------------------------------------------------------------ サイト別ルール
  // root     : 本文の範囲を返す
  // skip     : 追加で除外する要素のセレクタ
  // classify : 要素の種類（見出し/キャプション等）を上書きする
  // stopAt   : 見出しの文字がこれに当てはまったら、そこで取り込みを終える
  // next     : 「次のページ」のURL（連続取り込み用）
  const ADAPTERS = [
    {
      id: 'y-history',
      label: '世界史の窓',
      match: (u) => /(^|\.)y-history\.net$/.test(u.hostname),
      root: () => document.querySelector('div.card'),
      next: () => {
        const a = document.querySelector('.prev-next .next a');
        return a ? a.href : null;
      }
    },
    {
      id: 'wikipedia',
      label: 'Wikipedia',
      match: (u) => /(^|\.)wikipedia\.org$/.test(u.hostname),
      root: () => document.querySelector('#mw-content-text .mw-parser-output'),
      title: () => {
        const h = document.querySelector('#firstHeading');
        return h ? h.textContent.trim() : null;
      },
      skip:
        '.mw-editsection, sup.reference, .reference, .navbox, .vertical-navbox, .sistersitebox, .ambox, ' +
        '.mbox-small, .noprint, .mw-empty-elt, .reflist, ol.references, #toc, .toc, .hatnote, ' +
        '.shortdescription, .infobox, .sidebar, .mw-cite-backlink, .Inline-Template, table.metadata',
      classify: (el) => (el.classList.contains('thumbcaption') ? { kind: 'caption' } : null),
      stopAt: (t) =>
        /^(脚注|注釈|出典|参考文献|関連項目|外部リンク|参照|関連文献|脚注・出典|注釈・出典|脚注と出典|Notes|References|See also|External links|Further reading)$/.test(t)
    },
    {
      id: 'generic',
      label: '汎用',
      match: () => true,
      root: () => genericRoot(),
      next: () => {
        const a = document.querySelector('a[rel~="next"]');
        return a ? a.href : null;
      }
    }
  ];
  const pickAdapter = () => {
    const u = new URL(location.href);
    return ADAPTERS.find((a) => a.match(u));
  };

  function genericRoot() {
    const cands = document.querySelectorAll(
      'article, main, [role=main], #content, #main, #main-content, .post, .entry-content, .article, .content'
    );
    let best = null;
    let len = 0;
    for (const c of cands) {
      const l = (c.innerText || '').length;
      if (l > len) { best = c; len = l; }
    }
    return best && len > 200 ? best : document.body;
  }

  // ------------------------------------------------------------ 手動指定の設定
  async function loadOverride() {
    try {
      const { wh_overrides } = await chrome.storage.local.get('wh_overrides');
      return (wh_overrides || {})[location.hostname] || null;
    } catch {
      return null;
    }
  }
  const validSelectors = (list) =>
    (list || []).filter((s) => {
      try { document.querySelector(s); return true; } catch { return false; }
    });

  // ------------------------------------------------------------ 画像
  function bestSrc(img) {
    let src = img.currentSrc || img.src;
    const set = img.getAttribute('srcset');
    if (set) {
      let best = null;
      for (const part of set.split(',')) {
        const [u, d] = part.trim().split(/\s+/);
        if (!u) continue;
        const score = d ? parseFloat(d) : 1;
        if (!best || score > best.score) best = { u, score };
      }
      if (best) {
        try { src = new URL(best.u, location.href).href; } catch { /* そのまま */ }
      }
    }
    return src;
  }
  async function waitImages(root) {
    const imgs = [...root.querySelectorAll('img')].filter((i) => !i.complete);
    if (!imgs.length) return;
    imgs.forEach((i) => { if (i.loading === 'lazy') i.loading = 'eager'; });
    await Promise.race([
      Promise.all(imgs.map((i) => new Promise((r) => {
        i.addEventListener('load', r, { once: true });
        i.addEventListener('error', r, { once: true });
      }))),
      sleep(4000)
    ]);
  }

  // ------------------------------------------------------------ 抽出
  function contextOf(el, parent, adapter) {
    const custom = adapter.classify && adapter.classify(el, parent);
    if (custom) return custom;
    const tag = el.tagName;
    const cls = el.classList;
    if (/^H[1-6]$/.test(tag)) return { kind: 'heading', level: Number(tag[1]) };
    if (cls.contains('midasi')) return { kind: 'heading', level: 4 };
    if (tag === 'FIGCAPTION' || tag === 'CAPTION' || cls.contains('komoji') || cls.contains('caption'))
      return { kind: 'caption' };
    if (tag === 'LI') return { kind: 'list' };
    if (cls.contains('lead')) return { kind: 'paragraph', lead: true };
    return parent;
  }

  async function extract() {
    const adapter = pickAdapter();
    const ov = await loadOverride();

    let root = null;
    if (ov && ov.root) {
      try { root = document.querySelector(ov.root); } catch { root = null; }
    }
    const usedOverride = !!root;
    root = root || (adapter.root && adapter.root()) || genericRoot();
    const skip = [BASE_SKIP, adapter.skip, ...validSelectors(ov && ov.exclude)].filter(Boolean).join(', ');

    await waitImages(root);

    const blocks = [];
    const ranges = [];
    let buf = null;
    let stopped = false;

    const flush = () => {
      if (!buf) return;
      const text = norm(buf.text);
      const ctx = buf.ctx;
      const start = buf.start;
      const end = buf.end;
      buf = null;
      if (!text) return;
      if (ctx.kind === 'heading' && adapter.stopAt && adapter.stopAt(text)) {
        stopped = true;
        return;
      }
      const r = document.createRange();
      r.setStartBefore(start);
      r.setEndAfter(end);
      const b = { id: 'b' + String(blocks.length + 1).padStart(3, '0'), type: ctx.kind, text };
      if (ctx.level) b.level = ctx.level;
      if (ctx.lead) b.lead = true;
      blocks.push(b);
      ranges.push(r);
    };

    const pushImage = (img) => {
      const w = img.naturalWidth || img.width;
      const h = img.naturalHeight || img.height;
      if (w && w < MIN_IMG && h < MIN_IMG) return;
      const src = bestSrc(img);
      if (!src || src.startsWith('data:')) return;
      const r = document.createRange();
      r.selectNode(img);
      const b = {
        id: 'b' + String(blocks.length + 1).padStart(3, '0'),
        type: 'image',
        src,
        alt: img.alt || '',
        width: w || null,
        height: h || null
      };
      const a = img.closest('a');
      if (a && a.href && /\/(wiki\/(File|ファイル):|File:)/i.test(decodeURIComponent(a.href))) b.sourcePage = a.href; // 出典・ライセンス確認用
      blocks.push(b);
      ranges.push(r);
    };

    const walk = (node, ctx) => {
      for (const child of node.childNodes) {
        if (stopped) return;
        if (child.nodeType === Node.TEXT_NODE) {
          if (!buf) {
            if (!child.nodeValue.trim()) continue;
            buf = { text: '', start: child, end: child, ctx };
          }
          buf.text += child.nodeValue;
          buf.end = child;
        } else if (child.nodeType === Node.ELEMENT_NODE) {
          if (child.matches(skip)) continue;
          const tag = child.tagName;
          if (tag === 'BR') {
            flush();
          } else if (tag === 'IMG') {
            flush();
            pushImage(child);
          } else if (BLOCK_TAGS.has(tag)) {
            flush();
            walk(child, contextOf(child, ctx, adapter));
            flush();
          } else if (child.querySelector('img')) {
            flush();
            walk(child, ctx);
            flush();
          } else {
            walk(child, ctx);
          }
        }
      }
    };

    walk(root, contextOf(root, { kind: 'paragraph' }, adapter));
    flush();

    state.ranges = ranges;
    state.blocks = blocks;
    return {
      adapter: adapter.id,
      adapterLabel: adapter.label,
      usedOverride,
      title: (adapter.title && adapter.title()) || document.title,
      url: location.href,
      blocks,
      next: adapter.next ? adapter.next() : null
    };
  }

  // ------------------------------------------------------------ 録画との時刻合わせ
  // 各ブロックが画面内（上から70%より上）に入った最初の時刻（epoch ms）を記録する
  function check() {
    const h = window.innerHeight;
    state.ranges.forEach((r, i) => {
      if (state.seen[i] !== undefined) return;
      const rect = r.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;
      if (rect.top < h * 0.7 && rect.bottom > 0) state.seen[i] = Date.now();
    });
  }
  function stopTimers() {
    state.timers.forEach(clearInterval);
    state.timers = [];
  }
  function startTrack(speed) {
    stopTimers();
    state.seen = {};
    window.scrollTo({ top: 0, behavior: 'instant' });
    check();
    state.timers.push(setInterval(check, 100));
    if (speed > 0) {
      let y = 0;
      let last = performance.now();
      const id = setInterval(() => {
        const now = performance.now();
        y += (speed * (now - last)) / 1000;
        last = now;
        window.scrollTo({ top: y, behavior: 'instant' });
        const atBottom = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
        if (atBottom) {
          clearInterval(id);
          setTimeout(() => chrome.runtime.sendMessage({ type: 'scroll-done' }).catch(() => {}), 2000);
        }
      }, 16);
      state.timers.push(id);
    }
    return { viewport: { width: window.innerWidth, height: window.innerHeight } };
  }
  function stopTrack() {
    check();
    stopTimers();
    const times = {};
    for (const [i, t] of Object.entries(state.seen)) times[state.blocks[i].id] = t;
    return times;
  }

  window.__whClipper = { extract, startTrack, stopTrack };
})();
