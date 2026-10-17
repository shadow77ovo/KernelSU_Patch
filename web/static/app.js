/* KernelSU LKM 修补工具 · 网页版前端 */

const SID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now())
  .replace(/-/g, '');

const $ = (id) => document.getElementById(id);

const state = {
  image: null,
  apk: null,
  imageInfo: null,
  imageBox: 'infoOta',
  apkInfo: null,
  busy: false,
};

/* ---------------- 日志 ---------------- */
function classify(line) {
  if (line.startsWith('[错误]')) return 'l-err';
  if (line.includes('[警告]') || line.includes('[提示]')) return 'l-warn';
  if (line.includes('[OK]')) return 'l-ok';
  if (line.startsWith('$ ')) return 'l-cmd';
  if (line.startsWith('==')) return 'l-dim';
  return '';
}

function logLine(text, cls) {
  const el = $('log');
  if (el.dataset.empty === '1') {
    el.textContent = '';
    el.dataset.empty = '0';
  }
  const span = document.createElement('span');
  span.className = cls !== undefined ? cls : classify(text);
  span.textContent = text + '\n';
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}

function logLines(lines) {
  lines.forEach((l) => logLine(l));
}

/* ---------------- 状态 / 进度 ---------------- */
function setStatus(text) {
  $('status').textContent = text;
}

function setBar(el, frac) {
  const wrap = el.parentElement;
  if (frac === null) {
    wrap.classList.add('active', 'indeterminate');
    el.style.width = '';
  } else {
    wrap.classList.remove('indeterminate');
    wrap.classList.add('active');
    el.style.width = Math.max(2, Math.min(100, frac * 100)) + '%';
    if (frac >= 1) setTimeout(() => wrap.classList.remove('active'), 350);
  }
}

function hideBar(el) {
  el.parentElement.classList.remove('active', 'indeterminate');
  el.style.width = '0';
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- 上传 ---------------- */
function uploadFile(file, kind) {
  return new Promise((resolve, reject) => {
    const bar = kind === 'image' ? $('progImage') : $('progApk');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-Session', SID);
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setBar(bar, e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status === 200) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch (err) {
          reject(new Error('返回数据解析失败'));
        }
      } else {
        let msg = `上传失败（HTTP ${xhr.status}）`;
        try {
          msg = JSON.parse(xhr.responseText).error || msg;
        } catch (e) {}
        reject(new Error(msg));
      }
    };
    xhr.onerror = () => reject(new Error('网络错误，上传中断'));
    xhr.send(file);
  });
}

/* ---------------- 渲染 ---------------- */
function renderImage(d, boxId) {
  const box = $(boxId || 'infoImage');
  box.className = 'info';
  if (!d) return;
  if (d.error) {
    box.classList.add('bad');
    box.textContent = '✘ ' + d.error;
    return;
  }
  const lines = [
    `格式：${d.kindLabel} v${d.version}    页大小：${d.pageSize}`,
    `内核：${d.kernelVersion || '无（init_boot 不含内核）'}    KMI：${d.kmi || '无法自动识别'}`,
    `ramdisk：${d.ramdiskLabel}（${d.compression}）    AVB：${d.avb ? '有签名尾' : '未检测到'}`,
  ];
  if (d.patched) {
    lines.push('状态：ramdisk 内已存在 kernelsu.ko（已打过补丁）');
    lines.push(
      d.ksuConfig ? `ksu_config = '${d.ksuConfig}'` : 'ksu_config：不存在（没有写入任何启动参数）'
    );
    if (d.ksuConfig && d.ksuConfig.indexOf('allow_shell=1') >= 0) {
      lines.push('→ allow_shell 已开启：adb shell 的 su 由内核直接放行');
    } else {
      lines.push('→ allow_shell 未开启：adb shell 的 su 需要管理器授权');
    }
  }
  (d.warnings || []).forEach((w) => lines.push('· ' + w));
  box.classList.add(d.warnings && d.warnings.length ? 'warn' : 'ok');
  box.textContent = lines.join('\n');

  const sel = $('kmi');
  if (d.kmi) {
    if (![...sel.options].some((o) => o.value === d.kmi)) {
      const opt = document.createElement('option');
      opt.value = opt.textContent = d.kmi;
      sel.appendChild(opt);
    }
    sel.value = d.kmi;
    $('kmiHint').textContent = '已由内核版本自动推断';
  } else if (d.kind === 'init_boot') {
    sel.value = '';
    $('kmiHint').textContent = 'init_boot 不含内核信息，请手动选择 KMI';
  }
}

function renderApk(d) {
  const box = $('infoApk');
  box.className = 'info';
  if (!d) return;
  if (d.error) {
    box.classList.add('bad');
    box.textContent = '✘ ' + d.error;
    return;
  }
  const lines = [
    `版本：${d.version}${d.versionCode ? '（versionCode ' + d.versionCode + '）' : ''}    大小：${d.sizeLabel}`,
    `ABI：${(d.abis || []).join(', ') || '未知'}`,
    d.kmis && d.kmis.length
      ? `内置 KMI（${d.kmis.length} 个）：${d.kmis.join(', ')}`
      : '内置 KMI：未从 APK 读到（由引擎自带资源决定）',
  ];
  box.classList.add('ok');
  box.textContent = lines.join('\n');

  // 只保留「自动检测」，其余按元素删除（按索引删会错位）
  const sel = $('kmi');
  [...sel.options].forEach((o, i) => {
    if (i > 0) o.remove();
  });
  (d.kmis || []).forEach((k) => {
    const opt = document.createElement('option');
    opt.value = opt.textContent = k;
    sel.appendChild(opt);
  });
  // 镜像推断出的 KMI 若不在 APK 列表里，也补进去并选中
  const imgKmi = state.imageInfo && state.imageInfo.kmi;
  if (imgKmi) {
    if (![...sel.options].some((o) => o.value === imgKmi)) {
      const opt = document.createElement('option');
      opt.value = opt.textContent = imgKmi;
      sel.appendChild(opt);
    }
    sel.value = imgKmi;
  }
}

/* ---------------- 交互 ---------------- */
async function inspect() {
  const body = {};
  if (state.image) body.image = state.image;
  if (state.apk) body.apk = state.apk;
  if (!Object.keys(body).length) return;
  const res = await fetch('/api/inspect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Session': SID },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.error) {
    logLine('[错误] ' + data.error);
    return;
  }
  if (data.image) {
    state.imageInfo = data.image;
    renderImage(data.image, state.imageBox);
  }
  if (data.apk) {
    state.apkInfo = data.apk;
    renderApk(data.apk);
  }
}

async function handleFile(file, kind) {
  if (!file || state.busy) return;
  const drop = kind === 'image' ? $('dropImage') : $('dropApk');
  const info = kind === 'image' ? $('infoImage') : $('infoApk');
  const bar = kind === 'image' ? $('progImage') : $('progApk');

  info.className = 'info';
  info.textContent = '正在上传…';
  drop.classList.remove('done');
  setStatus(`上传 ${file.name}…`);

  try {
    const meta = await uploadFile(file, kind);
    state[kind] = meta.name;
    drop.classList.add('done');
    drop.querySelector('.drop-inner').innerHTML =
      `<strong>${meta.name}</strong><span>${meta.sizeLabel}</span>`;
    hideBar(bar);
    await inspect();
    setStatus(kind === 'image' ? '镜像已解析' : 'APK 已解析');
  } catch (err) {
    hideBar(bar);
    info.classList.add('bad');
    info.textContent = '✘ ' + err.message;
    logLine('[错误] ' + err.message);
    setStatus('上传失败');
  }
}

function wireDrop(dropId, inputId, kind) {
  const drop = $(dropId);
  const input = $(inputId);

  input.addEventListener('change', () => {
    if (input.files && input.files[0]) handleFile(input.files[0], kind);
  });
  ['dragenter', 'dragover'].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add('over');
    })
  );
  ['dragleave', 'drop'].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove('over');
    })
  );
  drop.addEventListener('drop', (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) handleFile(f, kind);
  });
}

/* ---------------- 从固件包提取（云端 / 本地） ---------------- */
const OTA = {
  cloud: {
    src: 'otaSource',
    scan: 'btnScan',
    part: 'otaPart',
    run: 'btnExtract',
    prog: 'progOta',
    info: 'infoOta',
    box: 'infoOta',
  },
  local: {
    src: 'localPayload',
    scan: 'btnLocalScan',
    part: 'localPart',
    run: 'btnLocalExtract',
    prog: 'progLocal',
    info: 'infoImage',
    box: 'infoImage',
  },
};

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.pane === name);
  });
  $('paneLocal').classList.toggle('hidden', name !== 'local');
  $('paneOta').classList.toggle('hidden', name !== 'ota');
}

async function browsePayload(inputId) {
  try {
    const res = await fetch('/api/ota/browse');
    const d = await res.json();
    if (d.error) return logLine('[提示] ' + d.error);
    if (d.path) {
      $(inputId).value = d.path;
      setStatus('已选择：' + d.path);
    }
  } catch (err) {
    logLine('[错误] ' + err.message);
  }
}

async function scanOta(key) {
  const c = OTA[key];
  const src = $(c.src).value.trim();
  const btn = $(c.scan);
  const info = $(c.info);

  if (!src) {
    return alert(key === 'cloud' ? '请先填写 OTA 全量包直链' : '请先选择本地 payload.bin');
  }

  btn.disabled = true;
  btn.textContent = '扫描中…';
  info.className = 'info';
  info.textContent =
    key === 'cloud' ? '正在读取包信息…（云端要先取 manifest，可能需要十几秒）' : '正在读取 payload.bin…';
  setStatus('扫描分区…');

  try {
    const res = await fetch('/api/ota/list', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: src }),
    });
    const d = await res.json();
    if (d.error) throw new Error(d.error);

    const sel = $(c.part);
    sel.innerHTML = '';
    (d.partitions || []).forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.name;
      opt.textContent = `${p.name}  (${p.sizeLabel})`;
      sel.appendChild(opt);
    });
    const prefer =
      d.partitions.find((p) => p.name === 'init_boot') || d.partitions.find((p) => p.name === 'boot');
    if (prefer) sel.value = prefer.name;

    info.className = 'info ok';
    info.textContent = `已读取到 ${d.partitions.length} 个分区，默认选「${sel.value}」。点「提取」开始。`;
    setStatus('分区已读取');
  } catch (err) {
    info.className = 'info bad';
    info.textContent = '✘ ' + err.message;
    logLine('[错误] ' + err.message);
    setStatus('扫描失败');
  } finally {
    btn.disabled = false;
    btn.textContent = '扫描分区';
  }
}

async function extractOta(key) {
  const c = OTA[key];
  const src = $(c.src).value.trim();
  const part = $(c.part).value;
  const btn = $(c.run);

  if (!src) {
    return alert(key === 'cloud' ? '请先填写 OTA 全量包直链' : '请先选择本地 payload.bin');
  }
  if (!part) return alert('请先「扫描分区」并选择要提取的分区');

  btn.disabled = true;
  btn.textContent = '提取中…';
  setBar($(c.prog), 0);
  $('log').textContent = '';
  $('log').dataset.empty = '0';
  setStatus('提取 ' + part + '…');

  try {
    const res = await fetch('/api/ota/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session': SID },
      body: JSON.stringify({ source: src, partitions: [part] }),
    });
    const d = await res.json();
    if (d.error) throw new Error(d.error);

    const result = await pollJob(d.job, $(c.prog));
    if (result.status !== 'done') throw new Error(result.error || '提取失败');

    setBar($(c.prog), 1);
    state.image = result.result.name;
    state.imageBox = c.box;
    await inspect();
    setStatus(`已提取 ${result.result.name}（${result.result.sizeLabel}）`);
    logLine(`[OK] 已提取 ${result.result.name}（${result.result.sizeLabel}），可以直接修补`, 'l-ok');
    if (result.result.path && !result.result.onDesktop) {
      logLine(`     保存在本机：${result.result.path}`, 'l-dim');
    }
  } catch (err) {
    hideBar($(c.prog));
    $(c.info).className = 'info bad';
    $(c.info).textContent = '✘ ' + err.message;
    logLine('[错误] ' + err.message);
    setStatus('提取失败');
  } finally {
    btn.disabled = false;
    btn.textContent = '提取';
  }
}

/* ---------------- 修补 ---------------- */
async function startPatch() {
  if (state.busy) return;
  if (!state.image) return alert('请先选择引导镜像');
  if (!state.apk) return alert('请先选择 KernelSU APK');

  const kmi = $('kmi').value;
  if (!kmi && state.imageInfo && state.imageInfo.kind === 'init_boot') {
    return alert('init_boot 不含内核信息，无法自动识别 KMI，请手动选择。');
  }

  state.busy = true;
  $('btnPatch').disabled = true;
  $('btnPatch').textContent = '修补中…';
  $('btnDownload').classList.add('hidden');
  $('log').textContent = '';
  $('log').dataset.empty = '0';
  setBar($('progMain'), null);

  try {
    const res = await fetch('/api/patch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session': SID },
      body: JSON.stringify({
        image: state.image,
        apk: state.apk,
        kmi,
        arch: $('arch').value,
        allowShell: $('allowShell').checked,
      }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    const result = await pollJob(data.job);
    if (result.status === 'done') {
      setBar($('progMain'), 1);
      const dl = $('btnDownload');
      dl.href = result.result.download;
      dl.setAttribute('download', result.result.name);
      dl.classList.remove('hidden');
      dl.classList.add('ok');
      dl.textContent = `下载 ${result.result.name}`;
      setStatus(
        result.result.onDesktop
          ? `完成 · 已保存到桌面（${result.result.name}）`
          : `完成 · ${result.result.name}（${result.result.sizeLabel}）`
      );
      logLine('');
      if (result.result.path && !result.result.onDesktop) {
        logLine(`保存在本机：${result.result.path}`, 'l-dim');
      }
      logLine(`刷入：fastboot flash ${result.result.partition} "${result.result.name}"`, 'l-dim');
      (result.result.warnings || []).forEach((w) => logLine('注意：' + w, 'l-warn'));
    } else {
      hideBar($('progMain'));
      setStatus('修补失败');
      logLine('[错误] ' + (result.error || '未知错误'));
    }
  } catch (err) {
    hideBar($('progMain'));
    setStatus('修补失败');
    logLine('[错误] ' + err.message);
  } finally {
    state.busy = false;
    $('btnPatch').disabled = false;
    $('btnPatch').textContent = '开始修补';
  }
}

async function pollJob(jobId, bar) {
  let since = 0;
  for (;;) {
    const res = await fetch(`/api/job/${jobId}?since=${since}`);
    if (!res.ok) throw new Error('任务状态查询失败');
    const d = await res.json();
    if (d.log && d.log.length) {
      logLines(d.log);
      since = d.total;
    }
    if (d.stage) setStatus(d.stage + '…');
    if (bar && typeof d.progress === 'number' && d.progress > 0) setBar(bar, d.progress);
    if (d.status !== 'running') return d;
    await sleep(500);
  }
}

/* ---------------- 引擎状态 ---------------- */
async function loadEngine() {
  try {
    const res = await fetch('/api/engine');
    const d = await res.json();
    const chip = $('engineChip');
    if (d.ready) {
      chip.className = 'chip ok';
      chip.textContent = `引擎：已缓存 ${d.cached[0] || ''}`;
    } else {
      chip.className = 'chip';
      chip.textContent = '引擎：首次修补时自动下载';
    }
  } catch (e) {
    $('engineChip').textContent = '引擎：状态未知';
  }
}

/* ---------------- 初始化 ---------------- */
document.querySelectorAll('.tab').forEach((t) => {
  t.addEventListener('click', () => switchTab(t.dataset.pane));
});
$('btnScan').addEventListener('click', () => scanOta('cloud'));
$('btnExtract').addEventListener('click', () => extractOta('cloud'));
$('btnLocalBrowse').addEventListener('click', () => browsePayload('localPayload'));
$('btnLocalScan').addEventListener('click', () => scanOta('local'));
$('btnLocalExtract').addEventListener('click', () => extractOta('local'));

// 支持 #local / #ota 直达标签页
if (location.hash === '#local') switchTab('local');

wireDrop('dropImage', 'fileImage', 'image');
wireDrop('dropApk', 'fileApk', 'apk');
$('btnPatch').addEventListener('click', startPatch);
$('btnClear').addEventListener('click', () => {
  $('log').textContent = '等待操作…';
  $('log').dataset.empty = '1';
});
$('log').dataset.empty = '1';
loadEngine();
