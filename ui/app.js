/**
 * Auto-Culling Tauri GUI Application Logic (Taubyte / Tau Cyber Edition)
 * 
 * Handles:
 * - IPC Communication with Tauri Rust / Resident Python Engine
 * - Dynamic Configuration Persistence with localStorage
 * - Real-time Progress, Speed (张/秒) & ETA Calculations
 * - Virtual/Incremental Table Rendering, Sorting & Filtering
 * - Asynchronous Image Preview with Detection/Crop Overlays
 * - Draggable Splitter Divider with Ratio Persistence
 */

(function () {
  'use strict';

  // --- State Management ---
  const state = {
    inputDir: '',
    isRunning: false,
    photos: [],         // Array of photo records: { name, path, rating, sharp, comp, raw, veto, status }
    photoMap: new Map(),// name -> photo record
    filter: 'all',      // 'all' | 'keep' | 'reject'
    sortField: 'name',
    sortAsc: true,
    selectedPhoto: null,
    totalFiles: 0,
    scoredCount: 0,
    keepCount: 0,
    rejectCount: 0,
    startTime: 0,
    tableRatio: 0.5,
  };

  const $ = (id) => document.getElementById(id);

  // --- DOM Elements ---
  const els = {
    inputDir: $('inputDir'),
    btnBrowse: $('btnBrowse'),
    btnRun: $('btnRun'),
    btnRunText: $('btnRunText'),
    btnExportCsv: $('btnExportCsv'),
    btnToggleLog: $('btnToggleLog'),
    stageStatus: $('stageStatus'),
    speedEtaStat: $('speedEtaStat'),
    progressBar: $('progressBar'),
    frameStat: $('frameStat'),
    countAll: $('countAll'),
    countKeep: $('countKeep'),
    countReject: $('countReject'),
    tableBody: $('tableBody'),
    tablePane: $('tablePane'),
    splitResizer: $('splitResizer'),
    previewPane: $('previewPane'),
    previewImg: $('previewImg'),
    previewEmpty: $('previewEmpty'),
    previewTitle: $('previewTitle'),
    previewMeta: $('previewMeta'),
    previewScoreDetails: $('previewScoreDetails'),
    pillRating: $('pillRating'),
    pillSharp: $('pillSharp'),
    pillComp: $('pillComp'),
    pillRaw: $('pillRaw'),
    pillReason: $('pillReason'),
    logDrawer: $('logDrawer'),
    logConsole: $('logConsole'),
    btnClearLog: $('btnClearLog'),
    systemPulse: $('systemPulse'),
  };

  // --- Parameter Bindings & Persistence ---
  const PARAMS = [
    { id: 'pTopN', key: 'top_n', type: 'int', default: 11 },
    { id: 'pWorkers', key: 'workers', type: 'int', default: 4 },
    { id: 'pP4', key: 'p4_policy', type: 'string', default: 'never' },
    { id: 'pRecursive', key: 'recursive', type: 'bool', default: false },
    { id: 'pForce', key: 'force', type: 'bool', default: false },
    { id: 'pSharp', key: 'sharp_thresh', type: 'float', default: 0.05 },
    { id: 'pWSharp', key: 'w_sharp', type: 'float', default: 1.5 },
    { id: 'pWComp', key: 'w_comp', type: 'float', default: 2.5 },
    { id: 'pMinRaw', key: 'min_raw', type: 'float', default: 3.1 },
    { id: 'pConf', key: 'conf', type: 'float', default: 0.25 },
    { id: 'pScale', key: 'scale_width', type: 'int', default: 1280 },
    { id: 'pRfKey', key: 'rf_api_key', type: 'string', default: '' },
    { id: 'pAutocrop', key: 'autocrop', type: 'bool', default: true },
    { id: 'pDryRun', key: 'dry_run', type: 'bool', default: false },
  ];

  function loadSavedParams() {
    PARAMS.forEach((p) => {
      const el = $(p.id);
      if (!el) return;
      const val = localStorage.getItem(`ac-param-${p.key}`);
      if (val !== null) {
        if (p.type === 'bool') el.checked = val === 'true';
        else el.value = val;
      }
      el.addEventListener('change', () => {
        const currentVal = p.type === 'bool' ? el.checked : el.value;
        localStorage.setItem(`ac-param-${p.key}`, currentVal);
      });
    });

    const savedRatio = localStorage.getItem('ac-table-ratio');
    if (savedRatio) {
      state.tableRatio = parseFloat(savedRatio);
      els.tablePane.style.setProperty('--table-width', `${(state.tableRatio * 100).toFixed(1)}%`);
    }

    const savedDir = localStorage.getItem('ac-last-dir');
    if (savedDir) {
      els.inputDir.value = savedDir;
      state.inputDir = savedDir;
      els.btnRun.disabled = false;
      triggerScan(savedDir);
    }
  }

  function getEngineConfig() {
    const config = {};
    PARAMS.forEach((p) => {
      const el = $(p.id);
      if (!el) return;
      if (p.type === 'int') config[p.key] = parseInt(el.value, 10);
      else if (p.type === 'float') config[p.key] = parseFloat(el.value);
      else if (p.type === 'bool') config[p.key] = el.checked;
      else config[p.key] = el.value || null;
    });
    return config;
  }

  // --- Tauri IPC Wrapper ---
  async function invokeTauri(cmd, args = {}) {
    if (window.__TAURI__ && window.__TAURI__.core) {
      return await window.__TAURI__.core.invoke(cmd, args);
    }
    console.warn(`[Tauri] invoke "${cmd}" fallback mock`, args);
    return null;
  }

  async function listenTauri(eventName, handler) {
    if (window.__TAURI__ && window.__TAURI__.event) {
      return await window.__TAURI__.event.listen(eventName, handler);
    }
    return () => {};
  }

  // --- Directory Selection & Scan ---
  async function chooseFolder() {
    try {
      const selected = await invokeTauri('select_folder');
      if (selected) {
        els.inputDir.value = selected;
        state.inputDir = selected;
        localStorage.setItem('ac-last-dir', selected);
        els.btnRun.disabled = false;
        await triggerScan(selected);
      }
    } catch (err) {
      appendLog(`[Error] 文件夹选择失败: ${err}`);
    }
  }

  async function triggerScan(dirPath) {
    if (!dirPath) return;
    els.stageStatus.textContent = '正在扫描目录...';
    const recursive = $('pRecursive')?.checked || false;
    await invokeTauri('scan', { dir: dirPath, recursive });
  }

  // --- Start / Cancel Culling Run ---
  async function handleRunToggle() {
    if (state.isRunning) {
      // User clicked Cancel
      els.stageStatus.textContent = '正在取消筛选...';
      await invokeTauri('cancel');
      return;
    }

    if (!state.inputDir) {
      alert('请先选择待筛照片目录');
      return;
    }

    // Start Run
    state.isRunning = true;
    state.startTime = performance.now();
    state.scoredCount = 0;
    state.keepCount = 0;
    state.rejectCount = 0;

    if (els.btnRunText) els.btnRunText.textContent = '取消筛选';
    els.btnRun.classList.remove('tau-btn-primary');
    els.btnRun.classList.add('tau-btn-cancel');
    els.progressBar.style.width = '0%';
    els.stageStatus.textContent = '正在启动引擎...';
    els.speedEtaStat.innerHTML = '<span class="tau-stat-label">SPEED:</span> <span class="tau-stat-val">CALCULATING...</span>';

    const config = getEngineConfig();
    try {
      await invokeTauri('run', { dir: state.inputDir, config });
    } catch (err) {
      appendLog(`[Error] 运行失败: ${err}`);
      finishRun('运行出错');
    }
  }

  function finishRun(statusText = '已完成') {
    state.isRunning = false;
    if (els.btnRunText) els.btnRunText.textContent = '⚡️开始筛选';
    els.btnRun.classList.add('tau-btn-primary');
    els.btnRun.classList.remove('tau-btn-cancel');
    els.btnExportCsv.disabled = state.photos.length === 0;
    els.stageStatus.textContent = statusText;
  }

  // --- Real-time Metrics: Speed (img/s) & ETA (预计剩余时间) ---
  function updateSpeedAndEta() {
    if (!state.isRunning || state.scoredCount <= 0) return;
    const elapsedSec = (performance.now() - state.startTime) / 1000;
    if (elapsedSec <= 0.1) return;

    const speed = state.scoredCount / elapsedSec; // img/s
    const speedText = speed.toFixed(1);

    const remainingPhotos = Math.max(0, state.totalFiles - state.scoredCount);
    let etaText = '--';
    if (speed > 0 && remainingPhotos > 0) {
      const remainingSec = Math.round(remainingPhotos / speed);
      const min = Math.floor(remainingSec / 60);
      const sec = remainingSec % 60;
      etaText = min > 0 ? `${min}m ${sec}s` : `${sec}s`;
    } else if (remainingPhotos === 0) {
      etaText = 'DONE';
    }

    els.speedEtaStat.innerHTML = `
      <span class="tau-stat-label">SPEED:</span> <span class="tau-stat-val">${speedText} img/s</span>
      <span class="tau-stat-sep">·</span>
      <span class="tau-stat-label">ETA:</span> <span class="tau-stat-val">${etaText}</span>
    `;
    els.frameStat.textContent = `SCORED ${state.scoredCount}/${state.totalFiles} · KEEP ${state.keepCount} · REJECT ${state.rejectCount}`;
  }

  // --- Event Handlers (Engine Stream) ---
  function setupEventListeners() {
    // 1. Directory Scanned
    listenTauri('scanned', ({ payload }) => {
      const paths = payload.paths || {};
      const count = payload.count || Object.keys(paths).length;
      state.totalFiles = count;
      state.photos = [];
      state.photoMap.clear();

      for (const [name, p] of Object.entries(paths)) {
        const item = {
          name,
          path: p,
          rating: 0,
          sharp: 0,
          comp: 0,
          raw: 0,
          veto: '',
          status: 'pending',
        };
        state.photos.push(item);
        state.photoMap.set(name, item);
      }

      state.scoredCount = 0;
      state.keepCount = 0;
      state.rejectCount = 0;

      els.stageStatus.textContent = `已发现 ${count} 张照片`;
      els.frameStat.textContent = `待筛选: 共 ${count} 张照片`;
      els.countAll.textContent = count;
      els.countKeep.textContent = '0';
      els.countReject.textContent = '0';
      els.progressBar.style.width = '0%';
      state.selectedPhoto = null;
      els.previewImg.style.display = 'none';
      els.previewImg.removeAttribute('src');
      els.previewEmpty.style.display = 'flex';
      els.previewTitle.textContent = '照片预览';
      els.previewScoreDetails.style.display = 'none';
      renderTable();
    });

    // 2. Stage updates
    listenTauri('stage', ({ payload }) => {
      const msg = payload.message || payload.msg || '处理中...';
      const pct = (payload.progress ?? payload.pct ?? 0) * 100;
      els.stageStatus.textContent = msg;
      if (!state.isRunning) return;
      if (pct > 0 && pct < 90) {
        els.progressBar.style.width = `${Math.max(pct, parseFloat(els.progressBar.style.width || 0))}%`;
      }
    });

    // 3. Scored Frame Event
    listenTauri('frame', ({ payload }) => {
      const item = state.photoMap.get(payload.name);
      if (!item) return;

      item.rating = payload.rating;
      item.sharp = payload.sharp;
      item.comp = payload.comp;
      item.raw = payload.raw;
      item.veto = payload.veto;
      item.status = payload.status;

      state.scoredCount++;
      if (payload.rating > 0) state.keepCount++;
      else state.rejectCount++;

      // Update Counts
      els.countKeep.textContent = state.keepCount;
      els.countReject.textContent = state.rejectCount;

      // Update Progress Bar
      if (state.totalFiles > 0) {
        const progressPct = 10 + (state.scoredCount / state.totalFiles) * 85;
        els.progressBar.style.width = `${Math.min(95, progressPct).toFixed(1)}%`;
      }

      updateSpeedAndEta();
      updateTableRow(item);
    });

    // 4. Run Done Event
    listenTauri('done', ({ payload }) => {
      els.progressBar.style.width = '100%';
      const ips = payload.total > 0 && payload.elapsed > 0 ? (payload.total / payload.elapsed).toFixed(1) : '--';
      els.speedEtaStat.innerHTML = `
        <span class="tau-stat-label">AVG:</span> <span class="tau-stat-val">${ips} img/s</span>
        <span class="tau-stat-sep">·</span>
        <span class="tau-stat-label">TIME:</span> <span class="tau-stat-val">${(payload.elapsed || 0).toFixed(1)}s</span>
      `;
      finishRun(`完成 · 保留 ${payload.keep} · 丢弃 ${payload.reject}`);
    });

    // 5. Cancelled Event
    listenTauri('cancelled', () => {
      finishRun('筛选已取消');
    });

    // 6. Log Events
    listenTauri('log', ({ payload }) => {
      appendLog(payload.line || JSON.stringify(payload));
    });

    // 7. Engine lifecycle errors (startup warmup failures etc.)
    listenTauri('engine-error', ({ payload }) => {
      appendLog(`[Engine Error] ${payload && payload.message ? payload.message : JSON.stringify(payload)}`);
      if (!state.isRunning) {
        els.stageStatus.textContent = '引擎启动失败（详见日志）';
      }
    });
  }

  function appendLog(line) {
    if (!els.logConsole) return;
    els.logConsole.textContent += `${line}\n`;
    els.logConsole.scrollTop = els.logConsole.scrollHeight;
  }

  // --- Table Rendering & In-Place Updates ---
  function renderTable() {
    const filtered = getFilteredAndSortedPhotos();
    if (filtered.length === 0) {
      els.tableBody.innerHTML = `
        <tr class="tau-empty-row">
          <td colspan="7">
            <div class="tau-empty-state">
              <span class="tau-empty-icon">📂</span>
              <p>${state.photos.length === 0 ? '请选择待筛照片目录并点击「开始筛选」' : '当前过滤条件下无匹配照片'}</p>
            </div>
          </td>
        </tr>
      `;
      return;
    }

    const html = filtered.map((item) => buildRowHtml(item)).join('');
    els.tableBody.innerHTML = html;
  }

  function buildRowHtml(item) {
    const isSelected = state.selectedPhoto && state.selectedPhoto.name === item.name;
    const ratingDisplay = item.status === 'pending'
      ? '<span style="color:#475569;">—</span>'
      : item.rating > 0
        ? `<span class="tau-stars">${'★'.repeat(item.rating)}</span>`
        : '<span class="tau-reject-tag">REJECT</span>';

    const reasonDisplay = item.veto
      ? `<span class="tau-veto-desc" title="${item.veto}">${item.veto}</span>`
      : item.rating > 0
        ? '<span class="tau-pass-tag">PASSED</span>'
        : '—';

    const statusDisplay = item.status === 'pending'
      ? '<span style="color:#64748b;">QUEUED</span>'
      : (item.status === 'scored' ? '<span style="color:#00e5ff;">SCORED</span>' : item.status);

    return `
      <tr id="row-${item.name.replace(/[^a-zA-Z0-9_-]/g, '_')}" data-name="${item.name}" class="${isSelected ? 'selected' : ''}">
        <td title="${item.name}" style="font-family: var(--tau-font-mono); font-weight: 500;">${item.name}</td>
        <td class="tau-th-num">${ratingDisplay}</td>
        <td class="tau-th-num" style="font-family: var(--tau-font-mono);">${item.sharp ? item.sharp.toFixed(3) : '—'}</td>
        <td class="tau-th-num" style="font-family: var(--tau-font-mono);">${item.comp ? item.comp.toFixed(3) : '—'}</td>
        <td class="tau-th-num" style="font-family: var(--tau-font-mono); font-weight: 600;">${item.raw ? item.raw.toFixed(2) : '—'}</td>
        <td>${reasonDisplay}</td>
        <td class="tau-th-center" style="font-family: var(--tau-font-mono); font-size: 10px;">${statusDisplay}</td>
      </tr>
    `;
  }

  function updateTableRow(item) {
    const rowId = `row-${item.name.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    const row = document.getElementById(rowId);
    if (!row) {
      renderTable();
      return;
    }

    // Check filter match
    if (state.filter === 'keep' && item.rating <= 0) {
      row.style.display = 'none';
      return;
    }
    if (state.filter === 'reject' && item.rating > 0) {
      row.style.display = 'none';
      return;
    }
    row.style.display = '';

    const newHtml = buildRowHtml(item);
    const temp = document.createElement('tbody');
    temp.innerHTML = newHtml;
    const newRow = temp.firstElementChild;
    row.replaceWith(newRow);
    newRow.classList.add('flash');

    if (state.selectedPhoto && state.selectedPhoto.name === item.name) {
      selectPhoto(item);
    }
  }

  function getFilteredAndSortedPhotos() {
    let list = state.photos.slice();
    if (state.filter === 'keep') {
      list = list.filter((p) => p.rating > 0);
    } else if (state.filter === 'reject') {
      list = list.filter((p) => p.rating === -1 || (p.status !== 'pending' && p.rating <= 0));
    }

    list.sort((a, b) => {
      let valA = a[state.sortField];
      let valB = b[state.sortField];
      if (typeof valA === 'string') return state.sortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA);
      valA = valA || 0;
      valB = valB || 0;
      return state.sortAsc ? valA - valB : valB - valA;
    });

    return list;
  }

  // --- Photo Selection & Thumbnail Preview ---
  async function selectPhoto(item) {
    if (!item) return;
    state.selectedPhoto = item;
    document.querySelectorAll('#photoTable tbody tr').forEach((r) => r.classList.remove('selected'));
    const rowId = `row-${item.name.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    const row = document.getElementById(rowId);
    if (row) row.classList.add('selected');

    els.previewTitle.textContent = item.name;
    els.previewScoreDetails.style.display = 'flex';
    els.pillRating.textContent = `RATING: ${item.rating > 0 ? `${item.rating}★` : (item.rating === -1 ? 'REJECT' : '-')}`;
    els.pillSharp.textContent = `SHARP: ${item.sharp ? item.sharp.toFixed(3) : '-'}`;
    els.pillComp.textContent = `COMP: ${item.comp ? item.comp.toFixed(3) : '-'}`;
    els.pillRaw.textContent = `RAW: ${item.raw ? item.raw.toFixed(2) : '-'}`;
    els.pillReason.textContent = `REASON: ${item.veto || (item.rating > 0 ? 'PASSED' : 'QUEUED')}`;

    // Request Base64 preview with bounding boxes
    const requestedPath = item.path;
    try {
      const res = await invokeTauri('preview', { path: requestedPath, size: 640 });
      if (state.selectedPhoto && state.selectedPhoto.path === requestedPath) {
        if (res && res.data) {
          const src = res.data.startsWith('data:') ? res.data : `data:image/png;base64,${res.data}`;
          els.previewImg.src = src;
          els.previewImg.style.display = 'block';
          els.previewEmpty.style.display = 'none';
        } else {
          els.previewImg.style.display = 'none';
          els.previewEmpty.style.display = 'flex';
          els.previewEmpty.querySelector('.tau-empty-title').textContent = `无法加载预览`;
          els.previewEmpty.querySelector('.tau-empty-desc').textContent = item.name;
        }
      }
    } catch (err) {
      if (state.selectedPhoto && state.selectedPhoto.path === requestedPath) {
        appendLog(`[Preview Error] 预览加载失败: ${err}`);
        els.previewImg.style.display = 'none';
        els.previewEmpty.style.display = 'flex';
        els.previewEmpty.querySelector('.tau-empty-title').textContent = `预览加载出错`;
        els.previewEmpty.querySelector('.tau-empty-desc').textContent = `${err}`;
      }
    }
  }

  // --- Draggable Splitter Divider ---
  function initSplitter() {
    let isDragging = false;

    els.splitResizer.addEventListener('mousedown', (e) => {
      isDragging = true;
      els.splitResizer.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
      if (!isDragging) return;
      const containerWidth = $('workspace').offsetWidth;
      const minW = 280;
      const maxW = containerWidth - minW;
      const newW = Math.max(minW, Math.min(maxW, e.clientX));
      const ratio = newW / containerWidth;

      state.tableRatio = ratio;
      els.tablePane.style.setProperty('--table-width', `${(ratio * 100).toFixed(1)}%`);
    });

    window.addEventListener('mouseup', () => {
      if (isDragging) {
        isDragging = false;
        els.splitResizer.classList.remove('dragging');
        document.body.style.cursor = '';
        localStorage.setItem('ac-table-ratio', state.tableRatio.toFixed(4));
      }
    });
  }

  // --- UI Event Handlers ---
  function initUI() {
    els.btnBrowse.addEventListener('click', chooseFolder);
    els.btnRun.addEventListener('click', handleRunToggle);

    els.inputDir.addEventListener('change', () => {
      const p = els.inputDir.value.trim();
      if (p) {
        state.inputDir = p;
        localStorage.setItem('ac-last-dir', p);
        els.btnRun.disabled = false;
        triggerScan(p);
      }
    });

    // Filter Buttons
    document.querySelectorAll('.tau-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tau-tab').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        state.filter = btn.getAttribute('data-filter');
        renderTable();
      });
    });

    // Table Header Sorting
    document.querySelectorAll('#photoTable th.sortable').forEach((th) => {
      th.addEventListener('click', () => {
        const field = th.getAttribute('data-sort');
        if (state.sortField === field) {
          state.sortAsc = !state.sortAsc;
        } else {
          state.sortField = field;
          state.sortAsc = true;
        }
        renderTable();
      });
    });

    // Row Click Delegation for Thumbnail Selection
    els.tableBody.addEventListener('click', (e) => {
      const row = e.target.closest('tr');
      if (!row || row.classList.contains('tau-empty-row')) return;
      const name = row.getAttribute('data-name');
      const item = state.photoMap.get(name);
      if (item) selectPhoto(item);
    });

    // Export CSV
    els.btnExportCsv.addEventListener('click', async () => {
      if (state.photos.length === 0) return;
      try {
        const res = await invokeTauri('export_csv', { dir: state.inputDir });
        alert(`打分结果 CSV 导出完成: ${res || 'scores.csv'}`);
      } catch (err) {
        alert(`导出失败: ${err}`);
      }
    });

    // Log Drawer Toggle
    els.btnToggleLog.addEventListener('click', () => {
      const isHidden = els.logDrawer.style.display === 'none';
      els.logDrawer.style.display = isHidden ? 'flex' : 'none';
    });

    els.btnClearLog.addEventListener('click', () => {
      els.logConsole.textContent = '';
    });

    // Keyboard Shortcuts (Cmd+O to browse, Space/Enter to run)
    window.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'o') {
        e.preventDefault();
        chooseFolder();
      }
    });

    initSplitter();
    loadSavedParams();
    setupEventListeners();
  }

  window.addEventListener('DOMContentLoaded', initUI);
})();
