// 本文範囲の手動指定。ページ上でクリックして、取り込む範囲と除外する要素を選ぶ。
// 結果はサイト（ホスト名）ごとに chrome.storage.local の wh_overrides に保存される。
(() => {
  if (window.__whPicker) {
    window.__whPicker.toggle();
    return;
  }

  const host = location.hostname;
  let active = false;
  let target = null; // いま枠が付いている要素
  const stack = []; // ↓キーで戻るための経路
  let lastMouse = { x: 0, y: 0 };
  let lock = null; // ↑↓キーで範囲を変えたあと、マウスが少し動いただけでは選び直さない

  // --- 画面の部品（ページのCSSに影響されないよう Shadow DOM に入れる）
  const holder = document.createElement('div');
  holder.style.cssText = 'all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  const shadow = holder.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      .box{position:fixed;border:2px solid #f0c860;background:rgba(240,200,96,.18);pointer-events:none;border-radius:2px}
      .bar{position:fixed;left:10px;bottom:10px;pointer-events:auto;max-width:min(440px,94vw);
        font:13px/1.6 system-ui,"Yu Gothic UI",sans-serif;background:#121826;color:#f0f4fa;border:1px solid #f0c860;border-radius:10px;padding:10px 14px;box-shadow:0 6px 24px rgba(0,0,0,.4)}
      .bar b{color:#f0c860}
      .bar .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;align-items:center}
      .bar button{font:inherit;border:1px solid #4a5675;background:#1c2539;color:#f0f4fa;border-radius:6px;padding:3px 10px;cursor:pointer}
      .bar button:hover{background:#2a3550}
      .msg{color:#8fe0b0;min-height:1.6em}
      .msg.err{color:#ff8a80}
    </style>
    <div class="box" id="hover" hidden></div>
    <div class="bar" id="bar">
      <div><b>本文範囲の指定</b>　クリック＝この範囲を取り込む ／ Alt+クリック＝この要素を除外 ／ ↑↓キー＝範囲を広げる・狭める ／ Esc＝終了</div>
      <div class="msg" id="msg"></div>
      <div class="row"><button id="reset">このサイトの設定を消す</button><button id="move">上へ移す</button><button id="close">終了</button></div>
    </div>`;
  const $ = (id) => shadow.getElementById(id);
  const hoverBox = $('hover');
  const msg = (t, err) => { const m = $('msg'); m.textContent = t; m.className = 'msg' + (err ? ' err' : ''); };

  // --- 選んだ要素を示す枠（ページ側の要素に outline を付ける。終了時に元へ戻す）
  const marked = new Map(); // element -> 元の outline
  function mark(el, color) {
    if (!marked.has(el)) marked.set(el, [el.style.outline, el.style.outlineOffset]);
    el.style.outline = `3px ${color === 'red' ? 'dashed #ff5a5a' : 'solid #3ddc84'}`;
    el.style.outlineOffset = '-3px';
  }
  function clearMarks() {
    for (const [el, [o, off]] of marked) { el.style.outline = o; el.style.outlineOffset = off; }
    marked.clear();
  }

  // --- セレクタを作る（一意に決まるまで親をたどる）
  function selectorFor(el) {
    const uniq = (sel) => { try { const l = document.querySelectorAll(sel); return l.length === 1 && l[0] === el; } catch { return false; } };
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && n !== document.documentElement) {
      let part = n.tagName.toLowerCase();
      if (n.id && /^[A-Za-z][\w-]*$/.test(n.id)) {
        parts.unshift('#' + CSS.escape(n.id));
        const s = parts.join(' > ');
        if (uniq(s)) return s;
        parts[0] = part + '#' + CSS.escape(n.id);
      } else {
        const cls = [...n.classList].filter((c) => !/^(js-|is-|has-|active|open|hover|focus)/.test(c)).slice(0, 2);
        if (cls.length) part += '.' + cls.map((c) => CSS.escape(c)).join('.');
        const sibs = n.parentElement ? [...n.parentElement.children].filter((s) => s.tagName === n.tagName) : [];
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(n) + 1})`;
        parts.unshift(part);
      }
      const s = parts.join(' > ');
      if (uniq(s)) return s;
      n = n.parentElement;
    }
    return parts.join(' > ');
  }

  // --- 保存
  async function getAll() {
    const { wh_overrides } = await chrome.storage.local.get('wh_overrides');
    return wh_overrides || {};
  }
  async function save(fn) {
    const all = await getAll();
    all[host] = fn(all[host] || { root: null, exclude: [] });
    if (!all[host].root && !(all[host].exclude || []).length) delete all[host];
    await chrome.storage.local.set({ wh_overrides: all });
    return all[host] || null;
  }
  async function restoreMarks() {
    clearMarks();
    const cfg = (await getAll())[host];
    if (!cfg) return;
    try { if (cfg.root) { const r = document.querySelector(cfg.root); if (r) mark(r, 'green'); } } catch { /* 無効なセレクタ */ }
    for (const s of cfg.exclude || []) { try { const e = document.querySelector(s); if (e) mark(e, 'red'); } catch { /* 無効 */ } }
  }

  // --- 枠の表示
  function setTarget(el) {
    target = el;
    if (!el) { hoverBox.hidden = true; return; }
    const r = el.getBoundingClientRect();
    Object.assign(hoverBox.style, { left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px' });
    hoverBox.hidden = false;
  }
  const inUI = (e) => e.composedPath().includes(holder);

  function onMove(e) {
    if (!active || inUI(e)) return;
    lastMouse = { x: e.clientX, y: e.clientY };
    if (lock && Math.hypot(e.clientX - lock.x, e.clientY - lock.y) < 24) return;
    lock = null;
    const el = e.target;
    if (el === document.documentElement || el === document.body) return setTarget(null);
    stack.length = 0;
    setTarget(el);
  }
  async function onClick(e) {
    if (!active || inUI(e)) return;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    if (!target) return;
    const sel = selectorFor(target);
    const chars = (target.innerText || '').length;
    try {
      if (e.altKey) {
        await save((c) => ({ root: c.root, exclude: [...new Set([...(c.exclude || []), sel])] }));
        msg(`除外に追加しました（${chars}文字）`);
      } else {
        await save((c) => ({ root: sel, exclude: c.exclude || [] }));
        msg(`本文範囲を保存しました（${chars}文字）。次回からこのサイトで使います。`);
      }
      await restoreMarks();
    } catch (err) {
      msg('保存できませんでした: ' + (err.message || err), true);
    }
  }
  function onKey(e) {
    if (!active) return;
    if (e.key === 'Escape') { e.preventDefault(); stop(); }
    else if (e.key === 'ArrowUp' && target) {
      e.preventDefault();
      const p = target.parentElement;
      if (p && p !== document.documentElement) { stack.push(target); lock = { ...lastMouse }; setTarget(p); }
    } else if (e.key === 'ArrowDown' && stack.length) {
      e.preventDefault();
      lock = { ...lastMouse };
      setTarget(stack.pop());
    }
  }
  const onScroll = () => target && setTarget(target);

  async function start() {
    active = true;
    document.documentElement.append(holder);
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', onScroll, true);
    await restoreMarks();
    const cfg = (await getAll())[host];
    msg(cfg ? `このサイトの設定あり（除外 ${(cfg.exclude || []).length}件）。緑＝本文範囲、赤＝除外。` : '本文にしたい部分をクリックしてください。');
  }
  function stop() {
    active = false;
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('scroll', onScroll, true);
    clearMarks();
    holder.remove();
    setTarget(null);
  }

  $('close').addEventListener('click', stop);
  $('move').addEventListener('click', () => {
    const bar = $('bar');
    const top = bar.style.top !== '';
    bar.style.top = top ? '' : '10px';
    bar.style.bottom = top ? '10px' : '';
    $('move').textContent = top ? '上へ移す' : '下へ移す';
  });
  $('reset').addEventListener('click', async () => {
    const all = await getAll();
    delete all[host];
    await chrome.storage.local.set({ wh_overrides: all });
    await restoreMarks();
    msg('このサイトの設定を消しました。');
  });

  window.__whPicker = { toggle: () => (active ? stop() : start()) };
  start();
})();
