// サービスワーカー：抽出 → （録画）→ 保存 の流れを、状態機械として管理する。
// ワーカーは待機中に止まることがあるため、進行状況は chrome.storage.session に置く。
// 録画中はオフスクリーン文書が定期的に信号を送り、ワーカーを起こし続ける。

const OFFSCREEN_URL = 'offscreen.html';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- 状態
const getJob = async () => (await chrome.storage.session.get('job')).job || null;
const setJob = (job) => chrome.storage.session.set({ job });
const clearJob = () => chrome.storage.session.remove('job');

async function say(text) {
  await chrome.storage.session.set({ status: text });
  chrome.runtime.sendMessage({ type: 'status', text }).catch(() => {});
}
function badge(text, color) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

// ---------------------------------------------------------------- ファイル名
const sanitize = (s) =>
  String(s).replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').replace(/\s+/g, '_').slice(0, 60) || 'page';

function slugFromUrl(url, title) {
  try {
    const u = new URL(url);
    const v = u.searchParams.get('v');
    if (v) return sanitize('yt_' + v);
    const last = decodeURIComponent(u.pathname.split('/').filter(Boolean).pop() || u.hostname);
    return sanitize(last.replace(/\.[a-z0-9]+$/i, '')) || sanitize(title);
  } catch {
    return sanitize(title);
  }
}
const noHash = (u) => String(u).split('#')[0];
const sameOrigin = (a, b) => {
  try { return new URL(a).origin === new URL(b).origin; } catch { return false; }
};
const isYouTube = (url) => {
  try {
    const u = new URL(url);
    return (/(^|\.)youtube\.com$/.test(u.hostname) && u.pathname === '/watch') || u.hostname === 'youtu.be';
  } catch { return false; }
};

// ---------------------------------------------------------------- オフスクリーン（録画・Blob）
async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ['USER_MEDIA', 'BLOBS'],
    justification: 'タブの画面と音声の録画、保存用ファイルの作成のため'
  });
}
const toOffscreen = (msg) => chrome.runtime.sendMessage({ target: 'offscreen', ...msg });

// ---------------------------------------------------------------- ページ内スクリプト
function friendly(e) {
  const m = String(e && e.message ? e.message : e);
  if (/Cannot access|permission|host/i.test(m))
    return 'このページを操作する権限がありません。拡張機能のアイコンを押し直して、もう一度開始してください。';
  return m;
}
async function inject(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ['extractor.js'] });
}
async function callPage(tabId, func, args = [], world = 'ISOLATED') {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func, args, world });
  return res ? res.result : undefined;
}

// ---------------------------------------------------------------- YouTube（ページ内部のデータから取る）
// ページ本体の世界で動かす。YouTubeの画面の部品名には頼らない。
async function ytCollectMain() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const player = document.getElementById('movie_player');
  const pr = player && player.getPlayerResponse ? player.getPlayerResponse() : window.ytInitialPlayerResponse;
  if (!pr) return { ok: false, error: '動画の情報を読み取れませんでした。ページの表示が終わってからもう一度試してください。' };
  const vd = pr.videoDetails || {};
  const out = {
    ok: true, videoId: vd.videoId, title: vd.title || document.title, author: vd.author || '',
    lengthSeconds: Number(vd.lengthSeconds) || 0, description: vd.shortDescription || '',
    chapters: [], captions: [], transcript: [], transcriptStatus: 'none', transcriptLang: ''
  };

  // チャプター（ytInitialData の中の macroMarkersListItemRenderer を探す）
  try {
    let budget = 60000;
    const seen = new Set();
    const walk = (n) => {
      if (!n || typeof n !== 'object' || budget-- <= 0 || seen.has(n)) return;
      seen.add(n);
      const m = n.macroMarkersListItemRenderer;
      if (m && m.title && m.onTap && m.onTap.watchEndpoint) {
        out.chapters.push({ title: m.title.simpleText || (m.title.runs || []).map((r) => r.text).join(''), t: (m.onTap.watchEndpoint.startTimeSeconds || 0) * 1000 });
      }
      for (const k of Object.keys(n)) walk(n[k]);
    };
    walk(window.ytInitialData);
    const uniq = new Map(out.chapters.map((c) => [c.t, c]));
    out.chapters = [...uniq.values()].sort((a, b) => a.t - b.t);
  } catch { /* チャプターなし */ }

  // 字幕
  const tracks = (pr.captions && pr.captions.playerCaptionsTracklistRenderer && pr.captions.playerCaptionsTracklistRenderer.captionTracks) || [];
  out.captions = tracks.map((t) => ({ lang: t.languageCode, auto: t.kind === 'asr' }));
  const pick = tracks.find((t) => t.languageCode === 'ja' && t.kind !== 'asr') || tracks.find((t) => t.languageCode === 'ja') ||
    tracks.find((t) => t.kind !== 'asr') || tracks[0];
  if (pick) {
    try {
      const r = await fetch(pick.baseUrl + '&fmt=json3');
      const txt = await r.text();
      if (txt) {
        for (const e of JSON.parse(txt).events || []) {
          if (!e.segs) continue;
          const text = e.segs.map((s) => s.utf8).join('').replace(/\n/g, ' ').trim();
          if (text) out.transcript.push({ t: e.tStartMs || 0, text });
        }
      }
      out.transcriptLang = pick.languageCode + (pick.kind === 'asr' ? '（自動生成）' : '');
      out.transcriptStatus = out.transcript.length ? 'ok' : 'empty';
    } catch { out.transcriptStatus = 'error'; }
  }

  // 取れなかったとき：画面の「文字起こし」ボタンを探して開く（うまくいけば取れる程度の補助）
  if (!out.transcript.length) {
    try {
      const btn = [...document.querySelectorAll('button,[role=button]')].find((b) =>
        /transcript|文字起こし/i.test((b.getAttribute('aria-label') || '') + ' ' + b.textContent));
      if (btn) {
        btn.click();
        for (let i = 0; i < 16 && !out.transcript.length; i++) {
          await sleep(250);
          const segs = document.querySelectorAll('ytd-transcript-segment-renderer, transcript-segment-view-model');
          for (const s of segs) {
            const lines = (s.innerText || '').split('\n').map((x) => x.trim()).filter(Boolean);
            const m = lines[0] && lines[0].match(/^(?:(\d+):)?(\d{1,2}):(\d{2})$/);
            if (m && lines[1]) out.transcript.push({ t: ((+m[1] || 0) * 3600 + +m[2] * 60 + +m[3]) * 1000, text: lines.slice(1).join(' ') });
          }
        }
        if (out.transcript.length) out.transcriptStatus = 'ok(画面から)';
      }
    } catch { /* 補助なので無視 */ }
  }
  return out;
}

const hmsToMs = (s) => s.split(':').map(Number).reduce((a, v) => a * 60 + v, 0) * 1000;

function buildYouTubePage(d, url) {
  const blocks = [];
  const nid = () => 'b' + String(blocks.length + 1).padStart(3, '0');
  blocks.push({ id: nid(), type: 'heading', level: 1, text: d.title });

  // 概要欄：時刻つきの行はチャプターとして扱う
  const tsRe = /^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-–—:：]?\s*(.+)$/;
  const parsed = [];
  const rest = [];
  for (const ln of (d.description || '').split(/\r?\n/)) {
    const m = ln.match(tsRe);
    if (m) parsed.push({ title: m[2].trim(), t: hmsToMs(m[1]) });
    else rest.push(ln);
  }
  let chapters = d.chapters.length ? d.chapters : parsed.length >= 2 ? parsed : [];
  const descLines = (parsed.length >= 2 ? rest : (d.description || '').split(/\r?\n/))
    .map((l) => l.trim())
    .filter((l) => l && !/^https?:\/\/\S+$/.test(l) && !/^(#\S+\s*)+$/.test(l));
  const descText = descLines.join('\n').slice(0, 3000);
  for (const para of descText.split(/\n{1,}/)) {
    if (para.trim()) blocks.push({ id: nid(), type: 'paragraph', text: para.trim(), fromDescription: true });
  }

  // 字幕：チャプターごと（なければ全体）に、約200字ずつの段落へ
  const sep = /^ja/.test(d.transcriptLang) ? '' : ' ';
  const addTranscript = (segs) => {
    let buf = '';
    let t0 = null;
    const flush = () => {
      if (buf.trim()) blocks.push({ id: nid(), type: 'paragraph', text: buf.trim(), videoTime: t0 });
      buf = ''; t0 = null;
    };
    for (const s of segs) {
      if (t0 === null) t0 = s.t;
      buf += (buf ? sep : '') + s.text;
      if (buf.length >= 200) flush();
    }
    flush();
  };
  if (d.transcript.length) {
    if (chapters.length) {
      chapters = [...chapters].sort((a, b) => a.t - b.t);
      chapters.forEach((c, i) => {
        const end = i + 1 < chapters.length ? chapters[i + 1].t : Infinity;
        blocks.push({ id: nid(), type: 'heading', level: 2, text: c.title, videoTime: c.t });
        addTranscript(d.transcript.filter((s) => s.t >= c.t && s.t < end));
      });
    } else {
      blocks.push({ id: nid(), type: 'heading', level: 2, text: '字幕（' + d.transcriptLang + '）' });
      addTranscript(d.transcript);
    }
  } else if (chapters.length) {
    for (const c of chapters) blocks.push({ id: nid(), type: 'heading', level: 2, text: c.title, videoTime: c.t });
  }

  return {
    adapter: 'youtube', adapterLabel: 'YouTube', title: d.title, url, blocks, next: null,
    meta: { videoId: d.videoId, channel: d.author, lengthSeconds: d.lengthSeconds, chapters: chapters.length,
      captionTracks: d.captions, transcriptStatus: d.transcriptStatus, transcriptLang: d.transcriptLang }
  };
}

// ---------------------------------------------------------------- 開始
async function startJob(msg) {
  const cur = await getJob();
  if (cur && !cur.finishing) { await say('すでに実行中です。先に「停止して保存」を押してください。'); return; }

  const tab = await chrome.tabs.get(msg.tabId);
  let mode = msg.mode;
  let note = '';
  if (isYouTube(tab.url) && mode !== 'extract') {
    mode = 'extract';
    note = 'YouTubeの動画ページは録画せず、概要欄・チャプター・字幕の取得のみ行います。';
  }
  const job = {
    id: Date.now(), tabId: msg.tabId, mode, speed: msg.speed || 120,
    multi: !!msg.multi && mode !== 'manual', maxPages: Math.min(Math.max(msg.maxPages || 10, 1), 50),
    startUrl: tab.url, pages: [], phase: 'starting', startedAt: Date.now(), note
  };
  await setJob(job);
  badge('…', '#1a5fb4');

  if (mode !== 'extract') {
    await say('録画を開始します…');
    await ensureOffscreen();
    const rec = await toOffscreen({ cmd: 'start', streamId: msg.streamId });
    if (!rec || !rec.ok) {
      await clearJob();
      badge('');
      await say('録画を開始できませんでした：' + ((rec && rec.error) || '応答がありません') + '\n別のタブで同じ録画をしていないか確認してください。');
      return;
    }
    job.recStartEpoch = rec.startedEpoch;
    await setJob(job);
    badge('REC', '#c01c28');
  }
  await processPage();
}

// ---------------------------------------------------------------- 1ページ分の処理
async function processPage() {
  const job = await getJob();
  if (!job || job.finishing) return;
  const n = job.pages.length + 1;
  job.phase = 'extracting';
  await setJob(job);
  await say(`ページ ${n}${job.multi ? '/' + job.maxPages : ''} を抽出中…`);

  const tab = await chrome.tabs.get(job.tabId);
  let page;
  if (isYouTube(tab.url)) {
    const d = await callPage(job.tabId, ytCollectMain, [], 'MAIN');
    if (!d || !d.ok) throw new Error((d && d.error) || 'YouTubeの情報を取得できませんでした');
    page = buildYouTubePage(d, tab.url);
  } else {
    await inject(job.tabId);
    page = await callPage(job.tabId, () => window.__whClipper.extract());
  }
  if (!page || !page.blocks || !page.blocks.length) {
    if (job.pages.length) { await finishJob('本文が取れなかったため、ここまでで終了しました。'); return; }
    throw new Error('本文を取り出せませんでした。「本文範囲を指定」で範囲を選んでみてください。');
  }
  job.pages.push(page);

  if (job.mode === 'extract') {
    await setJob(job);
    await afterPage();
    return;
  }
  const info = await callPage(job.tabId, (s) => window.__whClipper.startTrack(s), [job.mode === 'auto' ? job.speed : 0]);
  job.viewport = job.viewport || (info && info.viewport) || null;
  job.phase = 'scrolling';
  await setJob(job);
  await say(
    job.mode === 'auto'
      ? `録画中：ページ ${n}${job.multi ? '/' + job.maxPages : ''}（自動スクロール）。このタブを触らないでください。`
      : '録画中（手動）。ページを自分でスクロールし、終わったら「停止して保存」を押してください。'
  );
}

async function onScrollDone(tabId) {
  const job = await getJob();
  if (!job || job.finishing || job.tabId !== tabId || job.phase !== 'scrolling' || job.mode !== 'auto') return;
  const page = job.pages[job.pages.length - 1];
  page.times = (await callPage(job.tabId, () => window.__whClipper.stopTrack())) || {};
  job.phase = 'between';
  await setJob(job);
  await afterPage();
}

async function afterPage() {
  const job = await getJob();
  if (!job || job.finishing) return;
  const page = job.pages[job.pages.length - 1];
  const next = job.multi ? page.next : null;
  const visited = job.pages.map((p) => noHash(p.url));
  if (next && job.pages.length < job.maxPages && !visited.includes(noHash(next)) && sameOrigin(next, job.startUrl)) {
    job.phase = 'loading';
    await setJob(job);
    await say(`次のページへ移動中…（${job.pages.length}/${job.maxPages} 完了）`);
    await chrome.tabs.update(job.tabId, { url: next });
  } else {
    await finishJob(job.multi && !next ? '最後のページまで取り込みました。' : '');
  }
}

// 次ページの読み込みが終わったら続きを処理する
chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (info.status !== 'complete') return;
  try {
    const job = await getJob();
    if (!job || job.finishing || job.tabId !== tabId || job.phase !== 'loading') return;
    job.phase = 'settling';
    await setJob(job);
    await sleep(900);
    await processPage();
  } catch (e) { await fail(e); }
});
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const job = await getJob();
  if (job && !job.finishing && job.tabId === tabId) await finishJob('取り込み中のタブが閉じられたため、ここまでで保存しました。');
});

// ---------------------------------------------------------------- 終了・保存
async function fail(e) {
  console.error(e);
  const job = await getJob();
  const msg = friendly(e);
  if (job && !job.finishing) {
    await finishJob('エラーのため中断しました：' + msg);
  } else {
    badge('');
    await say('エラー：' + msg);
  }
}

async function finishJob(reason) {
  const job = await getJob();
  if (!job || job.finishing) return;
  job.finishing = true;
  await setJob(job);
  await say('停止して保存の準備をしています…');
  badge('…', '#1a5fb4');

  // 録画中のページの時刻を確定
  if (job.phase === 'scrolling') {
    try {
      const page = job.pages[job.pages.length - 1];
      page.times = (await callPage(job.tabId, () => window.__whClipper.stopTrack())) || {};
    } catch { /* ページが閉じている等 */ }
  }
  let rec = null;
  if (job.mode !== 'extract' && job.recStartEpoch) {
    try { rec = await toOffscreen({ cmd: 'stop' }); } catch { rec = null; }
  }
  if (!job.pages.length) {
    await clearJob();
    badge('');
    await say(reason || '取り込めたページがありませんでした。');
    await closeOffscreenIfIdle();
    return;
  }
  try {
    const summary = await saveAll(job, rec);
    await say([reason, summary, job.note].filter(Boolean).join('\n'));
    badge('OK', '#17703f');
    setTimeout(() => badge(''), 8000);
  } catch (e) {
    console.error(e);
    badge('!', '#c01c28');
    await say('保存でエラーが起きました：' + friendly(e));
  }
  await clearJob();
  await closeOffscreenIfIdle();
}
async function closeOffscreenIfIdle() {
  try { if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument(); } catch { /* 無視 */ }
}

async function download(url, filename) {
  return chrome.downloads.download({ url, filename, conflictAction: 'overwrite' });
}

async function saveAll(job, rec) {
  const first = job.pages[0];
  const slug = slugFromUrl(first.url, first.title) + (job.pages.length > 1 ? `_${job.pages.length}p` : '');
  const base = `WorldHistoryClips/${slug}`;
  const rs = job.recStartEpoch || null;

  // ページを順に結合（時刻は録画開始からのミリ秒に直す）
  const blocks = [];
  const pagesMeta = [];
  job.pages.forEach((page, pi) => {
    const from = blocks.length;
    const multi = job.pages.length > 1;
    let synthetic = null;
    if (multi && page.blocks[0].type !== 'heading') {
      synthetic = { id: `p${pi + 1}-head`, type: 'heading', level: 2, text: page.title, pageStart: true };
      blocks.push(synthetic);
    }
    for (const b of page.blocks) {
      const nb = { ...b, id: `p${pi + 1}-${b.id}` };
      if (rs && page.times && page.times[b.id]) nb.t = Math.max(0, page.times[b.id] - rs);
      blocks.push(nb);
    }
    if (synthetic) {
      const t = blocks.slice(from + 1).find((b) => b.t !== undefined);
      if (t) synthetic.t = t.t;
    }
    pagesMeta.push({ url: page.url, title: page.title, site: page.adapterLabel, blockFrom: from, blockTo: blocks.length - 1, ...(page.meta ? { meta: page.meta } : {}) });
  });

  // 画像
  const images = [...new Set(blocks.filter((b) => b.type === 'image').map((b) => b.src))];
  const fileOf = new Map();
  let ok = 0;
  let ng = 0;
  for (let i = 0; i < images.length; i++) {
    const src = images[i];
    await say(`画像を保存中… ${i + 1}/${images.length}`);
    let name = 'image';
    let ext = 'jpg';
    try {
      const p = decodeURIComponent(new URL(src).pathname).split('/').filter(Boolean);
      const last = /^\d+px-/.test(p[p.length - 1]) && p.length > 1 ? p[p.length - 2] : p[p.length - 1] || '';
      const m = last.match(/^(.*)\.([a-z0-9]{2,4})$/i);
      if (m) { name = sanitize(m[1]); ext = /svg/i.test(m[2]) ? 'png' : m[2].toLowerCase(); }
    } catch { /* 既定の名前 */ }
    const file = `images/${String(i + 1).padStart(3, '0')}_${name}.${ext}`;
    try {
      await download(src, `${base}/${file}`);
      fileOf.set(src, file);
      ok++;
    } catch (e) { console.warn('image failed', src, e); ng++; }
  }
  for (const b of blocks) if (b.type === 'image' && fileOf.has(b.src)) b.file = fileOf.get(b.src);

  // 録画
  let recording = null;
  let recNote = '';
  if (rec && rec.ok && rec.blobUrl && rec.size > 0) {
    await say('録画ファイルを保存中…');
    await download(rec.blobUrl, `${base}/recording.webm`);
    recording = { file: 'recording.webm', durationMs: rec.durationMs, mode: job.mode, speed: job.speed };
  } else if (job.mode !== 'extract') {
    recNote = '※録画ファイルは保存できませんでした（映像が取得できていません）。文章と画像だけ保存しています。';
  }

  const json = {
    title: first.title + (job.pages.length > 1 ? `（ほか${job.pages.length - 1}ページ）` : ''),
    url: first.url,
    capturedAt: new Date().toISOString(),
    viewport: job.viewport || null,
    recording,
    pages: pagesMeta,
    blocks
  };
  const text = JSON.stringify(json, null, 2);
  let jsonUrl;
  try {
    await ensureOffscreen();
    const r = await toOffscreen({ cmd: 'blob', text, mime: 'application/json' });
    jsonUrl = r && r.blobUrl;
  } catch { jsonUrl = null; }
  await download(jsonUrl || 'data:application/json;charset=utf-8,' + encodeURIComponent(text), `${base}/page.json`);

  const nText = blocks.filter((b) => b.type !== 'image').length;
  return [
    `保存しました：ダウンロード/${base}/`,
    `ページ${job.pages.length}件・文章${nText}件・画像${ok}枚${ng ? `（${ng}枚は保存できず）` : ''}${recording ? '・録画あり' : ''}`,
    recNote
  ].filter(Boolean).join('\n');
}

// ---------------------------------------------------------------- メッセージ
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target === 'offscreen') return;
  (async () => {
    const kind = msg.cmd || msg.type;
    if (kind === 'run') await startJob(msg);
    else if (kind === 'stop') await finishJob('停止しました。');
    else if (kind === 'scroll-done') { if (sender.tab) await onScrollDone(sender.tab.id); }
    else if (kind === 'rec-error') await finishJob('録画でエラーが起きたため、ここまでで保存しました：' + (msg.message || ''));
    else if (kind === 'pick') await chrome.scripting.executeScript({ target: { tabId: msg.tabId }, files: ['picker.js'] });
    else if (kind === 'status') {
      const { status } = await chrome.storage.session.get('status');
      const job = await getJob();
      sendResponse({
        status: status || '',
        job: job && !job.finishing ? {
          mode: job.mode, phase: job.phase, pages: job.pages.length, maxPages: job.maxPages, multi: job.multi,
          startedAt: job.startedAt, recording: !!job.recStartEpoch
        } : null
      });
      return;
    }
    // 'hb'（オフスクリーンからの生存信号）は何もしない
    sendResponse({ ok: true });
  })().catch(async (e) => {
    await fail(e);
    sendResponse({ ok: false });
  });
  return true;
});
