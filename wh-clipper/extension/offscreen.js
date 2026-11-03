// オフスクリーン文書：タブの映像・音声を録画する。保存用のファイル(Blob)も作る。
// 録画中は20秒おきに信号を送り、サービスワーカーが眠らないようにする。
let recorder = null;
let chunks = [];
let stream = null;
let audioCtx = null;
let startedEpoch = 0;
let heartbeat = null;
let bytes = 0;

const report = (message) => chrome.runtime.sendMessage({ type: 'rec-error', message }).catch(() => {});

async function start(streamId) {
  if (recorder) throw new Error('すでに録画中です');
  const base = { chromeMediaSource: 'tab', chromeMediaSourceId: streamId };
  stream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: base },
    video: { mandatory: { ...base, maxWidth: 1920, maxHeight: 1080, maxFrameRate: 30 } }
  });

  // タブを録画すると元のタブが無音になるので、音を流し続ける
  audioCtx = new AudioContext();
  audioCtx.createMediaStreamSource(stream).connect(audioCtx.destination);

  const mime = MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus') ? 'video/webm;codecs=vp9,opus' : 'video/webm';
  chunks = [];
  bytes = 0;
  recorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 6_000_000 });
  recorder.ondataavailable = (e) => {
    if (e.data.size) { chunks.push(e.data); bytes += e.data.size; }
  };
  recorder.onerror = (e) => report((e.error && e.error.message) || '録画エラー');
  const vt = stream.getVideoTracks()[0];
  if (vt) vt.onended = () => report('タブの映像が止まりました（ウィンドウの最小化やタブの切り替えが原因のことがあります）');

  recorder.start(1000);
  startedEpoch = Date.now();
  heartbeat = setInterval(() => chrome.runtime.sendMessage({ type: 'hb' }).catch(() => {}), 20000);
  return { ok: true, startedEpoch };
}

function stop() {
  return new Promise((resolve) => {
    clearInterval(heartbeat);
    if (!recorder) return resolve({ ok: false, error: '録画していません' });
    const durationMs = Date.now() - startedEpoch;
    const finish = () => {
      const blob = new Blob(chunks, { type: 'video/webm' });
      const out = { ok: true, blobUrl: blob.size ? URL.createObjectURL(blob) : null, durationMs, size: blob.size };
      try { stream && stream.getTracks().forEach((t) => t.stop()); } catch { /* 既に停止 */ }
      try { audioCtx && audioCtx.close(); } catch { /* 無視 */ }
      recorder = null; stream = null; audioCtx = null; chunks = [];
      resolve(out);
    };
    if (recorder.state === 'inactive') return finish();
    recorder.onstop = finish;
    try { recorder.stop(); } catch { finish(); }
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.target !== 'offscreen') return;
  let job = null;
  if (msg.cmd === 'start') job = start(msg.streamId);
  else if (msg.cmd === 'stop') job = stop();
  else if (msg.cmd === 'blob') job = Promise.resolve({ ok: true, blobUrl: URL.createObjectURL(new Blob([msg.text], { type: msg.mime || 'application/octet-stream' })) });
  if (!job) return;
  job.then(sendResponse).catch((e) => sendResponse({ ok: false, error: String((e && e.message) || e) }));
  return true;
});
