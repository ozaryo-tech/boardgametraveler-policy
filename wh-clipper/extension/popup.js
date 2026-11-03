const $ = (id) => document.getElementById(id);
const modeOf = () => document.querySelector('input[name=mode]:checked').value;
const isYouTube = (url) => {
  try {
    const u = new URL(url);
    return (/(^|\.)youtube\.com$/.test(u.hostname) && u.pathname === '/watch') || u.hostname === 'youtu.be';
  } catch { return false; }
};

let tab = null;
let host = '';
let running = false;
let startedAt = 0;

// ---- 画面の状態
function refreshUI() {
  const yt = tab && isYouTube(tab.url);
  $('ytNote').hidden = !yt;
  if (yt) document.querySelector('input[value=extract]').checked = true;
  $('mAuto').classList.toggle('off', !!yt || running);
  $('mManual').classList.toggle('off', !!yt || running);
  document.querySelectorAll('input[name=mode]').forEach((r) => { r.disabled = running || (yt && r.value !== 'extract'); });

  const mode = modeOf();
  $('speedRow').style.display = mode === 'auto' ? 'flex' : 'none';
  const multiOk = mode !== 'manual' && !yt;
  $('multiRow').style.display = multiOk ? 'flex' : 'none';
  if (!multiOk) $('multi').checked = false;
  $('multi').disabled = running;
  $('maxPages').disabled = running || !$('multi').checked;
  $('multiNote').hidden = !$('multi').checked || running;

  $('go').hidden = running;
  $('stop').hidden = !running;
  $('stop').textContent = mode === 'extract' ? '中止して、ここまでを保存' : '停止して保存';
  $('pick').disabled = running;
  $('timerRow').hidden = !running;
}
document.querySelectorAll('input[name=mode]').forEach((r) => r.addEventListener('change', refreshUI));
$('multi').addEventListener('change', refreshUI);
$('speed').addEventListener('input', () => ($('speedVal').textContent = $('speed').value));

// ---- 経過表示
const mmss = (ms) => {
  const s = Math.floor(ms / 1000);
  return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
};
setInterval(() => { if (running) $('timer').textContent = mmss(Date.now() - startedAt); }, 500);

async function poll() {
  try {
    const r = await chrome.runtime.sendMessage({ cmd: 'status' });
    if (!r) return;
    if (r.status && $('status').textContent !== r.status) $('status').textContent = r.status;
    const was = running;
    running = !!r.job;
    if (r.job) {
      startedAt = r.job.startedAt;
      $('progress').textContent = r.job.multi ? `　ページ ${r.job.pages}/${r.job.maxPages}` : '';
      document.querySelector(`input[value=${r.job.mode}]`).checked = true;
    }
    if (was !== running) refreshUI();
  } catch { /* ワーカー起動待ち */ }
}
chrome.runtime.onMessage.addListener((msg) => { if (msg.type === 'status') $('status').textContent = msg.text; });

// ---- サイトごとの本文範囲の指定
async function showOverride() {
  const { wh_overrides } = await chrome.storage.local.get('wh_overrides');
  const cfg = (wh_overrides || {})[host];
  $('ovInfo').textContent = cfg
    ? `${host} の指定あり（本文範囲 ${cfg.root ? '1' : '0'}件／除外 ${(cfg.exclude || []).length}件）`
    : host ? `${host}：指定なし（自動で本文を判定）` : '';
  $('resetSite').hidden = !cfg;
}
$('pick').addEventListener('click', async () => {
  if (!tab || !/^https?:/.test(tab.url || '')) { $('status').textContent = 'このページでは使えません。'; return; }
  await chrome.runtime.sendMessage({ cmd: 'pick', tabId: tab.id });
  window.close();
});
$('resetSite').addEventListener('click', async () => {
  const { wh_overrides } = await chrome.storage.local.get('wh_overrides');
  const all = wh_overrides || {};
  delete all[host];
  await chrome.storage.local.set({ wh_overrides: all });
  showOverride();
});

// ---- 開始・停止
$('go').addEventListener('click', async () => {
  if (!tab || !/^https?:/.test(tab.url || '')) {
    $('status').textContent = 'このページは取り込めません（http/https のページで使ってください）。';
    return;
  }
  const mode = modeOf();
  const multi = $('multi').checked;

  // 連続取り込みは、ページ移動のあとも操作できるよう、このサイトへの許可が必要
  if (multi) {
    const origins = [new URL(tab.url).origin + '/*'];
    let granted = await chrome.permissions.contains({ origins });
    if (!granted) granted = await chrome.permissions.request({ origins });
    if (!granted) { $('status').textContent = '許可されなかったため、連続取り込みはできません。'; return; }
  }

  let streamId = null;
  if (mode !== 'extract') {
    try {
      // activeTab の権限があるこのポップアップ内で取得する
      streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    } catch (e) {
      $('status').textContent = '録画の準備に失敗しました：' + (e.message || e);
      return;
    }
  }
  $('status').textContent = '開始しています…';
  running = true;
  startedAt = Date.now();
  refreshUI();
  chrome.runtime.sendMessage({
    cmd: 'run', tabId: tab.id, streamId, mode, multi,
    maxPages: Number($('maxPages').value) || 10, speed: Number($('speed').value)
  });
});
$('stop').addEventListener('click', () => {
  $('stop').disabled = true;
  chrome.runtime.sendMessage({ cmd: 'stop' }).finally(() => { $('stop').disabled = false; });
});

// ---- 起動
(async () => {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try { host = new URL(tab.url).hostname; } catch { host = ''; }
  $('site').textContent = host ? host : '';
  await showOverride();
  refreshUI();
  await poll();
  setInterval(poll, 1000);
})();
