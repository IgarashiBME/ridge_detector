// Ridge Detector v2 - PWA Application
'use strict';

const App = {
  ws: null,
  wsConnected: false,
  currentMode: 'IDLE',
  currentPage: 'dashboard',
  timeSynced: false,
  // Annotation state
  annSession: null,
  annFrame: null,
  annFrameList: [],    // ordered list of frame filenames in the current session
  annPoints: [],       // legacy (kept for computed polygon output)
  annLeftLine: [],     // 2 points defining left edge line
  annRightLine: [],    // 2 points defining right edge line
  annLoadedPoly: null, // non-null when loaded polygon can't be reconstructed into lines
  annMode: 'line',     // 'line' (2+2 points) or 'curve' (N+N points)
  annCurveSide: 'left', // which side is being edited in curve mode
  annImage: null,
  annCanvas: null,
  annCtx: null,

  // ----------------------------------------------------------------
  // Init
  // ----------------------------------------------------------------
  init() {
    this.setupRouter();
    this.syncTime(); // Push browser time to Jetson before anything else.
    this.connectWebSocket();
    this.setupAnnotationCanvas();
    this.setupAnnotationKeys();

    // Initial route
    this.handleRoute();
    this.loadModels();

    // Periodic status fetch as fallback
    setInterval(() => this.fetchStatus(), 3000);
    // Re-push time periodically so the most recently active client wins.
    setInterval(() => this.syncTime(), 60000);
  },

  // ----------------------------------------------------------------
  // Time sync (Jetson has no NTP/RTC — browser is the time source)
  // ----------------------------------------------------------------
  async syncTime() {
    try {
      const res = await fetch('/api/time/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_epoch_ms: Date.now() }),
      });
      if (res.ok) {
        this.timeSynced = true;
        this.refreshRecordButtonState();
      }
    } catch (e) { /* offline — retry on next interval */ }
  },

  refreshRecordButtonState() {
    const btnRec = document.getElementById('btn-record');
    if (!btnRec) return;
    const isIdle = this.currentMode === 'IDLE';
    btnRec.disabled = !isIdle || !this.timeSynced;
    btnRec.title = this.timeSynced ? '' : 'Waiting for time sync…';
  },

  // ----------------------------------------------------------------
  // Router
  // ----------------------------------------------------------------
  setupRouter() {
    window.addEventListener('hashchange', () => this.handleRoute());
    document.querySelectorAll('nav a').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        window.location.hash = a.getAttribute('href');
      });
    });
  },

  handleRoute() {
    const hash = window.location.hash || '#/';
    const parts = hash.slice(2).split('/'); // Remove '#/'

    // Update nav
    document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));

    if (parts[0] === '' || parts[0] === undefined) {
      this.showPage('dashboard');
    } else if (parts[0] === 'sessions' && parts[1]) {
      this.showPage('annotation');
      this.loadAnnotationView(parts[1]);
    } else if (parts[0] === 'sessions') {
      this.showPage('sessions');
      this.loadSessions();
    } else if (parts[0] === 'training') {
      this.showPage('training');
      this.loadTrainingInfo();
    } else if (parts[0] === 'evaluation') {
      this.showPage('evaluation');
      this.loadEvaluationPage();
    } else if (parts[0] === 'playback') {
      this.showPage('playback');
      this.loadPlaybackPage();
    } else {
      this.showPage('dashboard');
    }
  },

  showPage(name) {
    this.currentPage = name;
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));

    const pageMap = {
      dashboard: 'page-dashboard',
      sessions: 'page-sessions',
      annotation: 'page-annotation',
      training: 'page-training',
      evaluation: 'page-evaluation',
      playback: 'page-playback',
    };

    const el = document.getElementById(pageMap[name]);
    if (el) el.classList.add('active');

    // Highlight nav
    const navMap = {
      dashboard: '[data-page="dashboard"]',
      sessions: '[data-page="sessions"]',
      annotation: '[data-page="sessions"]',
      training: '[data-page="training"]',
      evaluation: '[data-page="evaluation"]',
      playback: '[data-page="playback"]',
    };
    const navEl = document.querySelector(navMap[name]);
    if (navEl) navEl.classList.add('active');
  },

  navigate(page) {
    if (page === 'sessions') window.location.hash = '#/sessions';
    else if (page === 'dashboard') window.location.hash = '#/';
    else if (page === 'training') window.location.hash = '#/training';
    else if (page === 'evaluation') window.location.hash = '#/evaluation';
    else if (page === 'playback') window.location.hash = '#/playback';
  },

  // ----------------------------------------------------------------
  // WebSocket
  // ----------------------------------------------------------------
  connectWebSocket() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws`;

    try {
      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        this.wsConnected = true;
        this.updateConnectionDot(true);
        this.ws.send(JSON.stringify({
          type: 'subscribe',
          channels: ['status', 'detection', 'training', 'evaluation', 'playback', 'log', 'frame']
        }));
        // Re-sync time on every (re)connection.
        this.syncTime();
      };

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          this.handleWsMessage(msg);
        } catch (e) { /* ignore parse errors */ }
      };

      this.ws.onclose = () => {
        this.wsConnected = false;
        this.updateConnectionDot(false);
        setTimeout(() => this.connectWebSocket(), 2000);
      };

      this.ws.onerror = () => {
        this.ws.close();
      };
    } catch (e) {
      setTimeout(() => this.connectWebSocket(), 2000);
    }
  },

  handleWsMessage(msg) {
    switch (msg.type) {
      case 'status':
        this.updateStatus(msg.data);
        break;
      case 'detection':
        this.updateDetection(msg.data);
        break;
      case 'training':
        this.updateTraining(msg.data);
        break;
      case 'evaluation':
        this.updateEvaluation(msg.data);
        break;
      case 'playback':
        this.updatePlayback(msg.data);
        break;
      case 'log':
        this.appendLogs(msg.data);
        break;
      case 'frame':
        this.updateFrame(msg.data);
        break;
    }
  },

  updateConnectionDot(connected) {
    const dot = document.getElementById('ws-dot');
    if (dot) {
      dot.className = connected
        ? 'connection-dot connected'
        : 'connection-dot disconnected';
    }
  },

  // ----------------------------------------------------------------
  // Status updates
  // ----------------------------------------------------------------
  async fetchStatus() {
    try {
      const res = await fetch('/api/status');
      if (res.ok) {
        const data = await res.json();
        this.updateStatus(data);
      }
    } catch (e) { /* offline */ }
  },

  updateStatus(data) {
    if (!data) return;
    const mode = data.mode || 'IDLE';
    const prevMode = this.currentMode;
    this.currentMode = mode;

    // Reload model list when transitioning from TRAINING to IDLE
    if (prevMode === 'TRAINING' && mode === 'IDLE') {
      this.loadModels();
    }

    // Mode badge
    const badge = document.getElementById('mode-badge');
    if (badge) {
      badge.textContent = mode;
      badge.className = `mode-badge mode-${mode}`;
    }

    // Control buttons
    const isIdle = mode === 'IDLE';
    const btnRec = document.getElementById('btn-record');
    const btnDet = document.getElementById('btn-detect');
    const btnStop = document.getElementById('btn-stop');
    if (btnRec) {
      btnRec.disabled = !isIdle || !this.timeSynced;
      btnRec.title = this.timeSynced ? '' : 'Waiting for time sync…';
    }
    if (btnDet) btnDet.disabled = !isIdle;
    if (btnStop) btnStop.disabled = isIdle;

    // Reflect server-known sync state too (e.g. after reload).
    if (data.time_sync && data.time_sync.synced) {
      this.timeSynced = true;
    }

    // Conf threshold
    if (data.conf !== undefined) {
      const confInput = document.getElementById('conf-input');
      if (confInput && document.activeElement !== confInput) {
        confInput.value = data.conf;
      }
    }

    // EMA alpha
    if (data.ema_alpha !== undefined) {
      this.updateEmaAlpha(data.ema_alpha);
    }

    // Detection data if present
    if (data.detection) {
      this.updateDetection(data.detection);
    }

    // Training data if present
    if (data.training) {
      this.updateTraining(data.training);
    }

    // Evaluation data if present
    if (data.evaluation) {
      this.updateEvaluation(data.evaluation);
    }

    // Playback data if present
    if (data.playback) {
      this.updatePlayback(data.playback);
    }
  },

  updateDetection(data) {
    if (!data) return;
    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    const aText = data.a != null ? Number(data.a).toFixed(4) : '--';
    const bText = data.b != null ? Number(data.b).toFixed(1) : '--';
    const fpsText = data.fps != null ? Number(data.fps).toFixed(1) : '--';
    const serialText = data.serial_count != null
      ? `${data.serial_status || ''} ${data.serial_count}` : '--';

    setVal('stat-a', aText);
    setVal('stat-b', bText);
    setVal('stat-fps', fpsText);
    setVal('stat-serial', serialText);
    setVal('pb-stat-a', aText);
    setVal('pb-stat-b', bText);
    setVal('pb-stat-fps', fpsText);
    setVal('pb-stat-serial', serialText);
  },

  updateTraining(data) {
    if (!data) return;
    const phase = document.getElementById('training-phase');
    const progress = document.getElementById('training-progress');
    const detail = document.getElementById('training-detail');
    const btnStart = document.getElementById('btn-train-start');
    const btnStop = document.getElementById('btn-train-stop');

    if (data.running) {
      const pct = data.total_epochs > 0
        ? Math.round((data.epoch / data.total_epochs) * 100) : 0;
      if (phase) phase.textContent = `Training: ${data.phase || 'running'}`;
      if (progress) progress.style.width = `${pct}%`;
      if (detail) detail.textContent =
        `Epoch ${data.epoch}/${data.total_epochs} | Loss: ${Number(data.loss || 0).toFixed(4)}`;
      if (btnStart) btnStart.disabled = true;
      if (btnStop) btnStop.disabled = false;
    } else {
      if (phase) phase.textContent = data.phase || 'Not running';
      if (progress) progress.style.width = '0%';
      if (detail) detail.textContent = '';
      if (btnStart) btnStart.disabled = false;
      if (btnStop) btnStop.disabled = true;
      if (data.phase === 'completed') this.loadModels();
    }
  },

  updateFrame(base64Data) {
    if (!base64Data) return;
    const src = `data:image/jpeg;base64,${base64Data}`;
    let imgId = null;
    if (this.currentPage === 'dashboard') imgId = 'live-preview';
    else if (this.currentPage === 'playback') imgId = 'playback-preview';
    if (!imgId) return;
    const img = document.getElementById(imgId);
    if (img) img.src = src;
  },

  appendLogs(logs) {
    const area = document.getElementById('log-area');
    if (!area || !logs) return;
    for (const line of logs) {
      area.textContent += line + '\n';
    }
    area.scrollTop = area.scrollHeight;
  },

  // ----------------------------------------------------------------
  // Mode control
  // ----------------------------------------------------------------
  async setMode(mode) {
    try {
      // Re-sync browser time right before RECORDING so the directory name
      // reflects the time of whichever client actually pressed Record.
      if (mode === 'RECORDING') {
        await this.syncTime();
      }
      const res = await fetch('/api/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  // ----------------------------------------------------------------
  // Model selection
  // ----------------------------------------------------------------
  async loadModels() {
    const select = document.getElementById('model-select');
    if (!select) return;
    try {
      const res = await fetch('/api/models');
      if (!res.ok) return;
      const data = await res.json();
      const models = data.models || [];
      select.innerHTML = '';
      for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m.path;
        opt.textContent = `${m.name} (${m.size_mb}MB)`;
        if (m.path === data.current) opt.selected = true;
        select.appendChild(opt);
      }
      if (models.length === 0) {
        select.innerHTML = '<option value="">No models</option>';
      }
    } catch (e) {
      // Keep existing options on error
    }
  },

  async selectModel(path) {
    if (!path) return;
    try {
      const res = await fetch('/api/models/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed');
      }
    } catch (e) {
      alert('Connection error');
    }
  },

  // ----------------------------------------------------------------
  // Conf Threshold
  // ----------------------------------------------------------------
  async setConf(value) {
    const conf = parseFloat(value);
    if (isNaN(conf)) return;
    try {
      await fetch('/api/conf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conf }),
      });
    } catch (e) { /* offline */ }
  },

  // ----------------------------------------------------------------
  // EMA Alpha
  // ----------------------------------------------------------------
  onEmaSliderInput(value) {
    const label = document.getElementById('ema-value');
    if (label) label.textContent = parseFloat(value).toFixed(2);
  },

  async setEmaAlpha(value) {
    const alpha = parseFloat(value);
    try {
      await fetch('/api/ema-alpha', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alpha }),
      });
    } catch (e) { /* offline */ }
  },

  updateEmaAlpha(alpha) {
    const slider = document.getElementById('ema-slider');
    const label = document.getElementById('ema-value');
    if (slider) slider.value = alpha;
    if (label) label.textContent = parseFloat(alpha).toFixed(2);
  },

  // ----------------------------------------------------------------
  // Sessions
  // ----------------------------------------------------------------
  async loadSessions() {
    const container = document.getElementById('session-list');
    if (!container) return;
    container.innerHTML = '<p style="color:var(--text2)">Loading...</p>';

    try {
      const res = await fetch('/api/sessions');
      const sessions = await res.json();

      if (sessions.length === 0) {
        container.innerHTML = '<div class="empty-state">No recording sessions yet</div>';
        return;
      }

      container.innerHTML = sessions.map(s => `
        <div class="session-item" onclick="window.location.hash='#/sessions/${s.name}'">
          <div class="session-info">
            <h4>${s.name}</h4>
            <span>${s.frame_count} frames | ${s.annotated_count} annotated</span>
          </div>
          <div class="session-meta">
            ${s.svo2_size_mb} MB
            <br>
            ${s.downloadable && s.frame_count > 0 ? `<a class="btn btn-secondary" style="font-size:11px; padding:4px 8px; margin-top:4px; text-decoration:none; display:inline-block;"
              href="/api/sessions/${s.name}/download" download
              onclick="event.stopPropagation()">Download</a>` : ''}
            <button class="btn btn-danger" style="font-size:11px; padding:4px 8px; margin-top:4px;"
              onclick="event.stopPropagation(); App.deleteSession('${s.name}')">Delete</button>
          </div>
        </div>
      `).join('');
    } catch (e) {
      container.innerHTML = '<div class="empty-state">Failed to load sessions</div>';
    }

    // Also update training page stats
    this.updateDatasetStats();
  },

  async deleteSession(name) {
    if (!confirm(`Delete session ${name}? This cannot be undone.`)) return;
    try {
      await fetch(`/api/sessions/${name}`, { method: 'DELETE' });
      this.loadSessions();
    } catch (e) {
      alert('Failed to delete session');
    }
  },

  // ----------------------------------------------------------------
  // Annotation view
  // ----------------------------------------------------------------
  async loadAnnotationView(sessionName) {
    this.annSession = sessionName;
    this.annFrame = null;
    this.annFrameList = [];
    this.annPoints = [];
    this.annLeftLine = [];
    this.annRightLine = [];
    this.annLoadedPoly = null;

    document.getElementById('ann-session-name').textContent = sessionName;
    document.getElementById('ann-editor').style.display = 'none';

    const grid = document.getElementById('frame-grid');
    grid.innerHTML = '<p style="color:var(--text2)">Loading...</p>';

    try {
      const res = await fetch(`/api/sessions/${sessionName}/frames`);
      const frames = await res.json();

      if (frames.length === 0) {
        grid.innerHTML = '<div class="empty-state">No frames in this session</div>';
        return;
      }

      this.annFrameList = frames.map(f => f.filename);

      grid.innerHTML = frames.map(f => `
        <div class="frame-thumb ${f.annotated ? 'annotated' : ''}"
             id="thumb-${f.filename}"
             onclick="App.selectFrame('${f.filename}')">
          <img src="/api/sessions/${sessionName}/frames/${f.filename}"
               loading="lazy" alt="${f.filename}">
          ${f.annotated ? '<span class="badge-dot"></span>' : ''}
        </div>
      `).join('');
    } catch (e) {
      grid.innerHTML = '<div class="empty-state">Failed to load frames</div>';
    }
  },

  async selectFrame(filename) {
    this.annFrame = filename;
    this.annPoints = [];
    this.annLeftLine = [];
    this.annRightLine = [];
    this.annLoadedPoly = null;
    this.annCurveSide = 'left';
    this._updateCurveConfirmBtn();

    // Highlight selection
    document.querySelectorAll('.frame-thumb').forEach(el => el.classList.remove('selected'));
    const thumb = document.getElementById(`thumb-${filename}`);
    if (thumb) thumb.classList.add('selected');

    document.getElementById('ann-frame-name').textContent = filename;
    const editor = document.getElementById('ann-editor');
    editor.style.display = 'block';
    // Bring the editor into view (esp. on first frame selection from the grid below)
    editor.scrollIntoView({ block: 'start', behavior: 'smooth' });
    this._updateFrameNav();

    // Load image into canvas
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      this.annImage = img;
      this.resizeCanvas();
      this.drawAnnotation();
    };
    img.src = `/api/sessions/${this.annSession}/frames/${filename}`;

    // Load existing annotation
    try {
      const res = await fetch(
        `/api/sessions/${this.annSession}/frames/${filename}/annotation`
      );
      const data = await res.json();
      if (data.exists && data.points && data.points.length >= 3) {
        const p = data.points;
        if (p.length === 4) {
          // Reconstruct left/right lines from saved quadrilateral
          this.annLeftLine = [p[0], p[3]];
          this.annRightLine = [p[1], p[2]];
        } else {
          // Variable-length polygon (has corners) — store for display only
          this.annLoadedPoly = p;
        }
      }
    } catch (e) { /* no annotation */ }
    this.drawAnnotation();
    this._updateStepIndicator();
  },

  // Frame navigation (prev/next within the current session)
  _currentFrameIndex() {
    if (!this.annFrame || this.annFrameList.length === 0) return -1;
    return this.annFrameList.indexOf(this.annFrame);
  },

  _updateFrameNav() {
    const posEl = document.getElementById('ann-position');
    const prevBtn = document.getElementById('ann-prev-btn');
    const nextBtn = document.getElementById('ann-next-btn');
    if (!posEl || !prevBtn || !nextBtn) return;
    const idx = this._currentFrameIndex();
    const total = this.annFrameList.length;
    if (idx < 0 || total === 0) {
      posEl.textContent = '- / -';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      return;
    }
    posEl.textContent = `${idx + 1} / ${total}`;
    prevBtn.disabled = idx <= 0;
    nextBtn.disabled = idx >= total - 1;
  },

  prevFrame() {
    const idx = this._currentFrameIndex();
    if (idx > 0) this.selectFrame(this.annFrameList[idx - 1]);
  },

  nextFrame() {
    const idx = this._currentFrameIndex();
    if (idx >= 0 && idx < this.annFrameList.length - 1) {
      this.selectFrame(this.annFrameList[idx + 1]);
    }
  },

  // ----------------------------------------------------------------
  // Annotation Mode
  // ----------------------------------------------------------------

  setAnnMode(mode) {
    if (mode === this.annMode) return;
    this.annMode = mode;
    this.annLeftLine = [];
    this.annRightLine = [];
    this.annPoints = [];
    this.annLoadedPoly = null;
    this.annCurveSide = 'left';

    document.getElementById('ann-mode-line').classList.toggle('active', mode === 'line');
    document.getElementById('ann-mode-curve').classList.toggle('active', mode === 'curve');

    const helpEl = document.getElementById('ann-help');
    if (helpEl) {
      helpEl.textContent = mode === 'line'
        ? 'Left edge 2 points (cyan), then right edge 2 points (orange). Lines extend to image boundary.'
        : 'Tap points along LEFT edge top→bottom (cyan), press confirm, then RIGHT edge (orange). Curves extend via tangent.';
    }
    this._updateCurveConfirmBtn();
    this._updateStepIndicator();
    this.drawAnnotation();
  },

  confirmCurveSide() {
    if (this.annMode !== 'curve') return;
    if (this.annCurveSide === 'left' && this.annLeftLine.length >= 2) {
      this.annCurveSide = 'right';
    }
    this._updateCurveConfirmBtn();
    this._updateStepIndicator();
  },

  _updateCurveConfirmBtn() {
    const wrap = document.getElementById('ann-curve-confirm');
    const btn = document.getElementById('ann-curve-confirm-btn');
    if (!wrap || !btn) return;
    if (this.annMode !== 'curve') {
      wrap.style.display = 'none';
      return;
    }
    wrap.style.display = 'block';
    if (this.annCurveSide === 'left') {
      btn.textContent = 'Left done \u2192 Right';
      btn.disabled = this.annLeftLine.length < 2;
    } else {
      btn.textContent = 'Right done';
      btn.disabled = true;
    }
  },

  // ----------------------------------------------------------------
  // Annotation Canvas
  // ----------------------------------------------------------------

  // Geometry: extend a line through two normalized points to the image boundary [0,1]x[0,1].
  // Returns the 2 intersection points with the boundary, sorted top-to-bottom (by y).
  _lineImageIntersections(p1, p2) {
    const dx = p2[0] - p1[0];
    const dy = p2[1] - p1[1];
    const hits = [];

    // Intersect with x=0 (left edge)
    if (dx !== 0) {
      const t = (0 - p1[0]) / dx;
      const y = p1[1] + t * dy;
      if (y >= 0 && y <= 1) hits.push([0, y]);
    }
    // Intersect with x=1 (right edge)
    if (dx !== 0) {
      const t = (1 - p1[0]) / dx;
      const y = p1[1] + t * dy;
      if (y >= 0 && y <= 1) hits.push([1, y]);
    }
    // Intersect with y=0 (top edge)
    if (dy !== 0) {
      const t = (0 - p1[1]) / dy;
      const x = p1[0] + t * dx;
      if (x >= 0 && x <= 1) hits.push([x, 0]);
    }
    // Intersect with y=1 (bottom edge)
    if (dy !== 0) {
      const t = (1 - p1[1]) / dy;
      const x = p1[0] + t * dx;
      if (x >= 0 && x <= 1) hits.push([x, 1]);
    }

    // Deduplicate (corner hits can appear twice)
    const unique = [];
    for (const h of hits) {
      if (!unique.some(u => Math.abs(u[0] - h[0]) < 1e-9 && Math.abs(u[1] - h[1]) < 1e-9)) {
        unique.push(h);
      }
    }
    // Sort by y, then by x
    unique.sort((a, b) => a[1] - b[1] || a[0] - b[0]);
    return unique.slice(0, 2);
  },

  // Which side of directed line p1→p2 is point on? Positive = left, negative = right.
  _sideOfLine(p1, p2, pt) {
    return (p2[0] - p1[0]) * (pt[1] - p1[1]) - (p2[1] - p1[1]) * (pt[0] - p1[0]);
  },

  // Intersection of segment a1-a2 with infinite line b1-b2.
  _segLineIntersect(a1, a2, b1, b2) {
    const d1x = a2[0] - a1[0], d1y = a2[1] - a1[1];
    const d2x = b2[0] - b1[0], d2y = b2[1] - b1[1];
    const cross = d1x * d2y - d1y * d2x;
    if (Math.abs(cross) < 1e-12) return a1;
    const t = ((b1[0] - a1[0]) * d2y - (b1[1] - a1[1]) * d2x) / cross;
    return [a1[0] + t * d1x, a1[1] + t * d1y];
  },

  // Sutherland-Hodgman: clip polygon by half-plane (keep side where keepSign matches).
  _clipPolygonByLine(polygon, lp1, lp2, keepSign) {
    if (polygon.length === 0) return [];
    const out = [];
    for (let i = 0; i < polygon.length; i++) {
      const cur = polygon[i];
      const nxt = polygon[(i + 1) % polygon.length];
      const cSide = this._sideOfLine(lp1, lp2, cur) * keepSign;
      const nSide = this._sideOfLine(lp1, lp2, nxt) * keepSign;
      if (cSide >= 0) {
        out.push(cur);
        if (nSide < 0) out.push(this._segLineIntersect(cur, nxt, lp1, lp2));
      } else if (nSide >= 0) {
        out.push(this._segLineIntersect(cur, nxt, lp1, lp2));
      }
    }
    return out;
  },

  // ----------------------------------------------------------------
  // Catmull-Rom spline helpers (for curve mode)
  // ----------------------------------------------------------------

  // Evaluate Catmull-Rom spline at parameter t in [0,1] between p1 and p2,
  // using p0 and p3 as neighbouring control points.
  _catmullRom(p0, p1, p2, p3, t) {
    const t2 = t * t, t3 = t2 * t;
    const x = 0.5 * ((2 * p1[0]) +
      (-p0[0] + p2[0]) * t +
      (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
      (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3);
    const y = 0.5 * ((2 * p1[1]) +
      (-p0[1] + p2[1]) * t +
      (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
      (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3);
    return [x, y];
  },

  // Generate dense points along a Catmull-Rom spline through pts (sorted top→bottom).
  // segmentsPerSpan: number of line segments per span between consecutive control points.
  _splinePoints(pts, segmentsPerSpan) {
    if (pts.length < 2) return pts.slice();
    const segs = segmentsPerSpan || 20;
    const result = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[Math.min(pts.length - 1, i + 2)];
      for (let s = 0; s < segs; s++) {
        result.push(this._catmullRom(p0, p1, p2, p3, s / segs));
      }
    }
    result.push(pts[pts.length - 1]);
    return result;
  },

  // Compute tangent direction at endpoints of a Catmull-Rom spline.
  // Returns { top: [dx,dy], bottom: [dx,dy] } (unnormalized, pointing outward from curve).
  _splineTangents(pts) {
    if (pts.length < 2) return null;
    const n = pts.length;
    // Catmull-Rom tangent at t=0 of span (p0,p1,p2,p3) = 0.5*(p2-p0)
    // First span: p0 clamped to pts[0], so tangent = 0.5*(pts[1] - pts[0])
    const topDx = 0.5 * (pts[1][0] - pts[0][0]);
    const topDy = 0.5 * (pts[1][1] - pts[0][1]);

    // Catmull-Rom tangent at t=1 of span (p0,p1,p2,p3) = 0.5*(p3-p1)
    // Last span: p3 clamped to pts[n-1], so tangent = 0.5*(pts[n-1] - pts[n-2])
    const botDx = 0.5 * (pts[n - 1][0] - pts[n - 2][0]);
    const botDy = 0.5 * (pts[n - 1][1] - pts[n - 2][1]);

    return {
      top: [-topDx, -topDy],   // outward = opposite to curve direction (curve goes top→bottom)
      bottom: [botDx, botDy],  // outward = same as curve direction at end
    };
  },

  // Extend a spline curve to the image boundary [0,1]x[0,1] using tangent at endpoints.
  // Returns the full array of points from top boundary → spline → bottom boundary.
  _extendSplineToBounds(pts) {
    if (pts.length < 2) return pts.slice();
    const dense = this._splinePoints(pts, 20);
    const tangents = this._splineTangents(pts);
    if (!tangents) return dense;

    const result = [];

    // Extend top: from pts[0] along -tangent direction until y=0 (or x boundary)
    const topPt = dense[0];
    const td = tangents.top;
    if (topPt[1] > 0.001 && (Math.abs(td[0]) > 1e-9 || Math.abs(td[1]) > 1e-9)) {
      // Find t where y = 0: topPt[1] + t*td[1] = 0
      const candidates = [];
      if (Math.abs(td[1]) > 1e-9) {
        const t = -topPt[1] / td[1];
        if (t > 0) {
          const x = topPt[0] + t * td[0];
          if (x >= 0 && x <= 1) candidates.push([x, 0]);
        }
      }
      // Also check x=0 and x=1
      if (Math.abs(td[0]) > 1e-9) {
        for (const xb of [0, 1]) {
          const t = (xb - topPt[0]) / td[0];
          if (t > 0) {
            const y = topPt[1] + t * td[1];
            if (y >= 0 && y <= 1) candidates.push([xb, y]);
          }
        }
      }
      if (candidates.length > 0) {
        // Pick closest intersection
        candidates.sort((a, b) => {
          const da = (a[0] - topPt[0]) ** 2 + (a[1] - topPt[1]) ** 2;
          const db = (b[0] - topPt[0]) ** 2 + (b[1] - topPt[1]) ** 2;
          return da - db;
        });
        result.push(candidates[0]);
      }
    }

    result.push(...dense);

    // Extend bottom: from pts[last] along tangent direction until y=1 (or x boundary)
    const botPt = dense[dense.length - 1];
    const bd = tangents.bottom;
    if (botPt[1] < 0.999 && (Math.abs(bd[0]) > 1e-9 || Math.abs(bd[1]) > 1e-9)) {
      const candidates = [];
      if (Math.abs(bd[1]) > 1e-9) {
        const t = (1 - botPt[1]) / bd[1];
        if (t > 0) {
          const x = botPt[0] + t * bd[0];
          if (x >= 0 && x <= 1) candidates.push([x, 1]);
        }
      }
      if (Math.abs(bd[0]) > 1e-9) {
        for (const xb of [0, 1]) {
          const t = (xb - botPt[0]) / bd[0];
          if (t > 0) {
            const y = botPt[1] + t * bd[1];
            if (y >= 0 && y <= 1) candidates.push([xb, y]);
          }
        }
      }
      if (candidates.length > 0) {
        candidates.sort((a, b) => {
          const da = (a[0] - botPt[0]) ** 2 + (a[1] - botPt[1]) ** 2;
          const db = (b[0] - botPt[0]) ** 2 + (b[1] - botPt[1]) ** 2;
          return da - db;
        });
        result.push(candidates[0]);
      }
    }

    return result;
  },

  _isOnBoundary(pt, eps = 1e-6) {
    return pt[0] <= eps || pt[0] >= 1 - eps || pt[1] <= eps || pt[1] >= 1 - eps;
  },

  _perimeterPos(pt) {
    const x = Math.max(0, Math.min(1, pt[0]));
    const y = Math.max(0, Math.min(1, pt[1]));
    const eps = 1e-6;
    if (Math.abs(y) <= eps) return x;
    if (Math.abs(x - 1) <= eps) return 1 + y;
    if (Math.abs(y - 1) <= eps) return 3 - x;
    if (Math.abs(x) <= eps) return 4 - y;
    return null;
  },

  _boundaryCorner(pos) {
    if (pos === 1) return [1, 0];
    if (pos === 2) return [1, 1];
    if (pos === 3) return [0, 1];
    return [0, 0];
  },

  _dedupePathPoints(pts, eps = 1e-9) {
    const out = [];
    for (const p of pts) {
      if (!out.length) {
        out.push(p);
        continue;
      }
      const q = out[out.length - 1];
      if (Math.abs(p[0] - q[0]) > eps || Math.abs(p[1] - q[1]) > eps) out.push(p);
    }
    return out;
  },

  _boundaryPath(a, b, dir) {
    const pa = this._perimeterPos(a);
    const pb = this._perimeterPos(b);
    if (pa === null || pb === null) return [a, b];

    if (dir === 'ccw') {
      const rev = this._boundaryPath(b, a, 'cw').slice().reverse();
      return this._dedupePathPoints(rev);
    }

    let end = pb;
    if (end < pa) end += 4;
    const pts = [a];
    for (const c0 of [1, 2, 3, 4]) {
      let c = c0;
      if (c <= pa) c += 4;
      if (c > pa && c < end) pts.push(this._boundaryCorner(c0));
    }
    pts.push(b);
    return this._dedupePathPoints(pts);
  },

  _pointInPolygon(pt, poly) {
    if (!poly || poly.length < 3) return false;
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i][0], yi = poly[i][1];
      const xj = poly[j][0], yj = poly[j][1];
      const intersect = ((yi > pt[1]) !== (yj > pt[1])) &&
        (pt[0] < ((xj - xi) * (pt[1] - yi)) / ((yj - yi) || 1e-12) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  },

  _curveReferencePoint(pts) {
    if (!pts || pts.length === 0) return [0.5, 0.5];
    let sx = 0, sy = 0;
    for (const p of pts) {
      sx += p[0];
      sy += p[1];
    }
    return [sx / pts.length, sy / pts.length];
  },

  _halfRegionFromCurve(curvePts, refPt) {
    if (!curvePts || curvePts.length < 2) return null;
    const start = curvePts[0];
    const end = curvePts[curvePts.length - 1];
    if (!this._isOnBoundary(start) || !this._isOnBoundary(end)) return null;

    const cwArc = this._boundaryPath(end, start, 'cw').slice(1);
    const ccwArc = this._boundaryPath(end, start, 'ccw').slice(1);
    const polyA = this._dedupePathPoints(curvePts.concat(cwArc));
    const polyB = this._dedupePathPoints(curvePts.concat(ccwArc));
    const inA = this._pointInPolygon(refPt, polyA);
    const inB = this._pointInPolygon(refPt, polyB);

    if (inA && !inB) return polyA;
    if (inB && !inA) return polyB;
    return polyA;
  },

  _drawPath(ctx, poly, scale) {
    if (!poly || poly.length < 3) return;
    const s = scale || 1;
    ctx.beginPath();
    ctx.moveTo(poly[0][0] * s, poly[0][1] * s);
    for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i][0] * s, poly[i][1] * s);
    ctx.closePath();
  },

  _traceMaskContour(alpha, size) {
    const edgeMap = new Map();
    const addEdge = (x1, y1, x2, y2) => {
      const start = `${x1},${y1}`;
      const end = `${x2},${y2}`;
      if (!edgeMap.has(start)) edgeMap.set(start, []);
      edgeMap.get(start).push(end);
    };
    const inside = (x, y) => x >= 0 && y >= 0 && x < size && y < size && alpha[y * size + x] > 0;

    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (!inside(x, y)) continue;
        if (!inside(x, y - 1)) addEdge(x, y, x + 1, y);
        if (!inside(x + 1, y)) addEdge(x + 1, y, x + 1, y + 1);
        if (!inside(x, y + 1)) addEdge(x + 1, y + 1, x, y + 1);
        if (!inside(x - 1, y)) addEdge(x, y + 1, x, y);
      }
    }

    const loops = [];
    while (edgeMap.size > 0) {
      const start = edgeMap.keys().next().value;
      const loop = [];
      let cur = start;
      let guard = 0;

      while (cur && guard < size * size * 8) {
        const [xs, ys] = cur.split(',').map(Number);
        loop.push([xs / size, ys / size]);
        const nexts = edgeMap.get(cur);
        if (!nexts || nexts.length === 0) break;
        const next = nexts.pop();
        if (nexts.length === 0) edgeMap.delete(cur);
        cur = next;
        if (cur === start) break;
        guard++;
      }
      if (loop.length >= 3) loops.push(loop);
    }

    if (loops.length === 0) return null;
    loops.sort((a, b) => b.length - a.length);
    return loops[0];
  },

  // Compute polygon from left and right edge lines clipped to image boundary.
  // Returns array of normalized [x,y] points (4-6 vertices), or null.
  _computePolygonFromLines() {
    if (this.annLeftLine.length < 2 || this.annRightLine.length < 2) return null;

    const L1 = this.annLeftLine[0], L2 = this.annLeftLine[1];
    const R1 = this.annRightLine[0], R2 = this.annRightLine[1];

    // Start with image rectangle
    let poly = [[0, 0], [1, 0], [1, 1], [0, 1]];

    // Determine which side of the left line the right line is on
    const rMid = [(R1[0] + R2[0]) / 2, (R1[1] + R2[1]) / 2];
    const leftKeep = this._sideOfLine(L1, L2, rMid) >= 0 ? 1 : -1;
    poly = this._clipPolygonByLine(poly, L1, L2, leftKeep);

    // Determine which side of the right line the left line is on
    const lMid = [(L1[0] + L2[0]) / 2, (L1[1] + L2[1]) / 2];
    const rightKeep = this._sideOfLine(R1, R2, lMid) >= 0 ? 1 : -1;
    poly = this._clipPolygonByLine(poly, R1, R2, rightKeep);

    if (poly.length < 3) return null;

    // Clamp to [0,1]
    for (let i = 0; i < poly.length; i++) {
      poly[i] = [Math.max(0, Math.min(1, poly[i][0])), Math.max(0, Math.min(1, poly[i][1]))];
    }
    return poly;
  },

  // Sample a spline curve (with tangent extensions) at a given y coordinate.
  // Returns the x coordinate, or null if no intersection found.
  _sampleSplineAtY(extPts, y) {
    for (let i = 0; i < extPts.length - 1; i++) {
      const y0 = extPts[i][1], y1 = extPts[i + 1][1];
      const yMin = Math.min(y0, y1), yMax = Math.max(y0, y1);
      if (y >= yMin && y <= yMax && Math.abs(y1 - y0) > 1e-12) {
        const t = (y - y0) / (y1 - y0);
        return extPts[i][0] + t * (extPts[i + 1][0] - extPts[i][0]);
      }
    }
    return null;
  },

  // Compute polygon from left and right spline curves (curve mode).
  // Builds the image-space band between the two curves, closed by image boundaries.
  _computePolygonFromCurves() {
    if (this.annLeftLine.length < 2 || this.annRightLine.length < 2) return null;

    const leftExt = this._extendSplineToBounds(this.annLeftLine);
    const rightExt = this._extendSplineToBounds(this.annRightLine);
    if (leftExt.length < 2 || rightExt.length < 2) return null;

    const leftRegion = this._halfRegionFromCurve(
      leftExt,
      this._curveReferencePoint(rightExt)
    );
    const rightRegion = this._halfRegionFromCurve(
      rightExt,
      this._curveReferencePoint(leftExt)
    );
    if (!leftRegion || !rightRegion) return null;

    const size = 1024;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return null;

    ctx.clearRect(0, 0, size, size);
    ctx.fillStyle = '#fff';
    this._drawPath(ctx, leftRegion, size);
    ctx.fill();
    ctx.globalCompositeOperation = 'destination-in';
    this._drawPath(ctx, rightRegion, size);
    ctx.fill();
    ctx.globalCompositeOperation = 'source-over';

    const img = ctx.getImageData(0, 0, size, size).data;
    const alpha = new Uint8Array(size * size);
    for (let i = 0; i < size * size; i++) alpha[i] = img[i * 4 + 3] > 0 ? 1 : 0;

    const poly = this._traceMaskContour(alpha, size);
    if (!poly || poly.length < 3) return null;

    for (let i = 0; i < poly.length; i++) {
      poly[i] = [Math.max(0, Math.min(1, poly[i][0])), Math.max(0, Math.min(1, poly[i][1]))];
    }
    return this._dedupePathPoints(poly);
  },

  // Get annotation step label
  _annStepLabel() {
    if (this.annMode === 'curve') {
      const lN = this.annLeftLine.length;
      const rN = this.annRightLine.length;
      if (this.annCurveSide === 'left') {
        if (lN === 0) return 'Tap points along LEFT edge (top→bottom)';
        return `LEFT edge: ${lN} points — add more or press confirm`;
      }
      if (rN === 0) return 'Tap points along RIGHT edge (top→bottom)';
      if (rN < 2) return `RIGHT edge: ${rN} point — add at least 1 more`;
      return `Done — ${lN}L + ${rN}R points — Save or adjust`;
    }
    const total = this.annLeftLine.length + this.annRightLine.length;
    if (total === 0) return 'Tap 1st point of LEFT edge';
    if (total === 1) return 'Tap 2nd point of LEFT edge';
    if (total === 2) return 'Tap 1st point of RIGHT edge';
    if (total === 3) return 'Tap 2nd point of RIGHT edge';
    return 'Done — Save or adjust';
  },

  _updateStepIndicator() {
    const el = document.getElementById('ann-step');
    if (el) el.textContent = this._annStepLabel();
  },

  // Find the nearest existing point within a pixel radius threshold.
  // Returns { line: 'left'|'right', index: 0|1 } or null.
  _findNearPoint(nx, ny, thresholdPx) {
    const canvas = this.annCanvas;
    if (!canvas) return null;
    const w = canvas.width, h = canvas.height;
    const thr2 = thresholdPx * thresholdPx;
    let best = null, bestD = Infinity;
    const check = (line, name) => {
      for (let i = 0; i < line.length; i++) {
        const dx = (line[i][0] - nx) * w;
        const dy = (line[i][1] - ny) * h;
        const d2 = dx * dx + dy * dy;
        if (d2 < thr2 && d2 < bestD) {
          bestD = d2;
          best = { line: name, index: i };
        }
      }
    };
    check(this.annLeftLine, 'left');
    check(this.annRightLine, 'right');
    return best;
  },

  setupAnnotationKeys() {
    document.addEventListener('keydown', (e) => {
      if (this.currentPage !== 'annotation') return;
      // Don't hijack keys when typing in form fields
      const tag = (e.target && e.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); this.prevFrame(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); this.nextFrame(); }
    });
  },

  setupAnnotationCanvas() {
    const canvas = document.getElementById('annotation-canvas');
    if (!canvas) return;
    this.annCanvas = canvas;
    this.annCtx = canvas.getContext('2d');

    // Drag state
    let dragging = null; // { line: 'left'|'right', index: 0|1 }

    const getXY = (e) => {
      const rect = canvas.getBoundingClientRect();
      let cx, cy;
      if (e.touches) { cx = e.touches[0].clientX; cy = e.touches[0].clientY; }
      else { cx = e.clientX; cy = e.clientY; }
      return [
        Math.max(0, Math.min(1, (cx - rect.left) / rect.width)),
        Math.max(0, Math.min(1, (cy - rect.top) / rect.height)),
      ];
    };

    const onDown = (e) => {
      e.preventDefault();
      if (!this.annImage) return;
      const [x, y] = getXY(e);

      // Check if near an existing point → start drag
      const hit = this._findNearPoint(x, y, 24);
      if (hit) {
        dragging = hit;
        return;
      }

      // Otherwise add new point
      if (this.annMode === 'line') {
        const total = this.annLeftLine.length + this.annRightLine.length;
        if (total >= 4) return;
        if (this.annLeftLine.length < 2) {
          this.annLeftLine.push([x, y]);
        } else {
          this.annRightLine.push([x, y]);
        }
      } else {
        // Curve mode: add to current side
        if (this.annCurveSide === 'left') {
          this.annLeftLine.push([x, y]);
        } else {
          this.annRightLine.push([x, y]);
        }
        this._updateCurveConfirmBtn();
      }
      this._updateStepIndicator();
      this.drawAnnotation();
    };

    const onMove = (e) => {
      if (!dragging) return;
      e.preventDefault();
      const [x, y] = getXY(e);
      const arr = dragging.line === 'left' ? this.annLeftLine : this.annRightLine;
      arr[dragging.index] = [x, y];
      this.drawAnnotation();
    };

    const onUp = (e) => {
      if (dragging) {
        dragging = null;
        this._updateStepIndicator();
      }
    };

    // Mouse events
    canvas.addEventListener('mousedown', onDown);
    canvas.addEventListener('mousemove', onMove);
    canvas.addEventListener('mouseup', onUp);

    // Touch events
    canvas.addEventListener('touchstart', onDown, { passive: false });
    canvas.addEventListener('touchmove', onMove, { passive: false });
    canvas.addEventListener('touchend', onUp);

    window.addEventListener('resize', () => {
      if (this.annImage) {
        this.resizeCanvas();
        this.drawAnnotation();
      }
    });
  },

  resizeCanvas() {
    if (!this.annCanvas || !this.annImage) return;
    const container = document.getElementById('annotation-area');
    const w = container.clientWidth;
    const aspect = this.annImage.naturalHeight / this.annImage.naturalWidth;
    const h = Math.round(w * aspect);
    this.annCanvas.width = w;
    this.annCanvas.height = h;
  },

  drawAnnotation() {
    const ctx = this.annCtx;
    const canvas = this.annCanvas;
    if (!ctx || !canvas || !this.annImage) return;

    const w = canvas.width;
    const h = canvas.height;
    ctx.drawImage(this.annImage, 0, 0, w, h);

    // If we have a loaded polygon that couldn't be decomposed into lines, draw it
    if (this.annLoadedPoly && this.annLeftLine.length === 0 && this.annRightLine.length === 0) {
      const lp = this.annLoadedPoly;
      ctx.fillStyle = 'rgba(233, 69, 96, 0.18)';
      ctx.strokeStyle = '#e94560';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(lp[0][0] * w, lp[0][1] * h);
      for (let i = 1; i < lp.length; i++) ctx.lineTo(lp[i][0] * w, lp[i][1] * h);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      return;
    }

    const leftLen = this.annLeftLine.length;
    const rightLen = this.annRightLine.length;
    if (leftLen === 0 && rightLen === 0) return;

    // Helper: draw extended line across image
    const drawExtLine = (p1, p2, color) => {
      const hits = this._lineImageIntersections(p1, p2);
      if (hits.length >= 2) {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(hits[0][0] * w, hits[0][1] * h);
        ctx.lineTo(hits[1][0] * w, hits[1][1] * h);
        ctx.stroke();
      }
    };

    // Helper: draw a polyline path (array of [x,y] normalized)
    const drawPolyline = (pts, color) => {
      if (pts.length < 2) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(pts[0][0] * w, pts[0][1] * h);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0] * w, pts[i][1] * h);
      ctx.stroke();
    };

    // Helper: draw a labeled point
    const drawPt = (pt, color, label) => {
      const px = pt[0] * w, py = pt[1] * h;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(px, py, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(label, px, py + 3);
    };

    if (this.annMode === 'curve') {
      // --- Curve mode drawing ---
      const poly = this._computePolygonFromCurves();
      if (poly) {
        ctx.fillStyle = 'rgba(233, 69, 96, 0.18)';
        ctx.beginPath();
        ctx.moveTo(poly[0][0] * w, poly[0][1] * h);
        for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i][0] * w, poly[i][1] * h);
        ctx.closePath();
        ctx.fill();

        // Midline (green): average of left and right at each y
        const leftExt = this._extendSplineToBounds(this.annLeftLine);
        const rightExt = this._extendSplineToBounds(this.annRightLine);
        const midPts = [];
        for (let i = 0; i <= 60; i++) {
          const y = i / 60;
          const lx = this._sampleSplineAtY(leftExt, y);
          const rx = this._sampleSplineAtY(rightExt, y);
          if (lx !== null && rx !== null) {
            midPts.push([(lx + rx) / 2, y]);
          }
        }
        drawPolyline(midPts, '#00ff00');
      }

      // Draw spline curves (extended to boundary)
      if (leftLen >= 2) drawPolyline(this._extendSplineToBounds(this.annLeftLine), '#00e5ff');
      else if (leftLen === 1) drawPt(this.annLeftLine[0], '#00e5ff', 'L1');

      if (rightLen >= 2) drawPolyline(this._extendSplineToBounds(this.annRightLine), '#ffa500');
      else if (rightLen === 1) drawPt(this.annRightLine[0], '#ffa500', 'R1');

      // Draw control points
      for (let i = 0; i < leftLen; i++) drawPt(this.annLeftLine[i], '#00e5ff', 'L' + (i + 1));
      for (let i = 0; i < rightLen; i++) drawPt(this.annRightLine[i], '#ffa500', 'R' + (i + 1));
    } else {
      // --- Line mode drawing (original) ---
      const poly = this._computePolygonFromLines();
      if (poly) {
        ctx.fillStyle = 'rgba(233, 69, 96, 0.18)';
        ctx.beginPath();
        ctx.moveTo(poly[0][0] * w, poly[0][1] * h);
        for (let i = 1; i < poly.length; i++) {
          ctx.lineTo(poly[i][0] * w, poly[i][1] * h);
        }
        ctx.closePath();
        ctx.fill();

        // Midline (green)
        const leftHits = this._lineImageIntersections(this.annLeftLine[0], this.annLeftLine[1]);
        const rightHits = this._lineImageIntersections(this.annRightLine[0], this.annRightLine[1]);
        if (leftHits.length >= 2 && rightHits.length >= 2) {
          const midP1 = [(leftHits[0][0] + rightHits[0][0]) / 2,
                          (leftHits[0][1] + rightHits[0][1]) / 2];
          const midP2 = [(leftHits[1][0] + rightHits[1][0]) / 2,
                          (leftHits[1][1] + rightHits[1][1]) / 2];
          drawExtLine(midP1, midP2, '#00ff00');
        }
      }

      // Draw left line (cyan)
      if (leftLen === 2) drawExtLine(this.annLeftLine[0], this.annLeftLine[1], '#00e5ff');

      // Draw right line (orange)
      if (rightLen === 2) drawExtLine(this.annRightLine[0], this.annRightLine[1], '#ffa500');

      // Draw clicked points
      for (let i = 0; i < leftLen; i++) drawPt(this.annLeftLine[i], '#00e5ff', 'L' + (i + 1));
      for (let i = 0; i < rightLen; i++) drawPt(this.annRightLine[i], '#ffa500', 'R' + (i + 1));
    }
  },

  async saveAnnotation() {
    if (!this.annSession || !this.annFrame) return;

    const poly = this.annMode === 'curve'
      ? this._computePolygonFromCurves()
      : this._computePolygonFromLines();
    if (!poly) {
      alert(this.annMode === 'curve'
        ? 'Place at least 2 points on each edge.'
        : 'Place 2 points for each edge line (4 points total).');
      return;
    }

    // For curve mode, keep enough points so saved masks match the on-screen shape.
    const savePoly = (this.annMode === 'curve' && poly.length > 120)
      ? this._downsamplePoly(poly, 120)
      : poly;

    try {
      const res = await fetch(
        `/api/sessions/${this.annSession}/frames/${this.annFrame}/annotation`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ points: savePoly }),
        }
      );
      if (res.ok) {
        const thumb = document.getElementById(`thumb-${this.annFrame}`);
        if (thumb && !thumb.classList.contains('annotated')) {
          thumb.classList.add('annotated');
          const dot = document.createElement('span');
          dot.className = 'badge-dot';
          thumb.appendChild(dot);
        }
      } else {
        const data = await res.json();
        alert(data.detail || 'Failed to save');
      }
    } catch (e) {
      alert('Connection error');
    }
  },

  // Downsample a polygon to approximately targetN points, evenly spaced by index.
  _downsamplePoly(poly, targetN) {
    if (poly.length <= targetN) return poly;
    const result = [];
    for (let i = 0; i < targetN; i++) {
      const idx = Math.round(i * (poly.length - 1) / (targetN - 1));
      result.push(poly[idx]);
    }
    return result;
  },

  undoLastPoint() {
    if (this.annMode === 'curve') {
      if (this.annCurveSide === 'right' && this.annRightLine.length > 0) {
        this.annRightLine.pop();
        // If right is empty, go back to left editing
        if (this.annRightLine.length === 0) {
          this.annCurveSide = 'left';
        }
      } else if (this.annLeftLine.length > 0) {
        this.annLeftLine.pop();
      }
      this._updateCurveConfirmBtn();
    } else {
      if (this.annRightLine.length > 0) {
        this.annRightLine.pop();
      } else if (this.annLeftLine.length > 0) {
        this.annLeftLine.pop();
      }
    }
    this._updateStepIndicator();
    this.drawAnnotation();
  },

  clearAnnotation() {
    this.annLeftLine = [];
    this.annRightLine = [];
    this.annPoints = [];
    this.annLoadedPoly = null;
    if (this.annMode === 'curve') {
      this.annCurveSide = 'left';
      this._updateCurveConfirmBtn();
    }
    this._updateStepIndicator();
    this.drawAnnotation();
  },

  async deleteAnnotation() {
    if (!this.annSession || !this.annFrame) return;
    try {
      await fetch(
        `/api/sessions/${this.annSession}/frames/${this.annFrame}/annotation`,
        { method: 'DELETE' }
      );
      this.annLeftLine = [];
      this.annRightLine = [];
      this.annPoints = [];
      this.annLoadedPoly = null;
      if (this.annMode === 'curve') {
        this.annCurveSide = 'left';
        this._updateCurveConfirmBtn();
      }
      this._updateStepIndicator();
      this.drawAnnotation();

      const thumb = document.getElementById(`thumb-${this.annFrame}`);
      if (thumb) {
        thumb.classList.remove('annotated');
        const dot = thumb.querySelector('.badge-dot');
        if (dot) dot.remove();
      }
    } catch (e) {
      alert('Failed to delete annotation');
    }
  },

  // ----------------------------------------------------------------
  // Test Image Detection
  // ----------------------------------------------------------------
  async startTestDetect() {
    if (!this.annSession || !this.annFrame) {
      alert('Select a frame first.');
      return;
    }
    try {
      const res = await fetch('/api/test-detect/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session: this.annSession,
          frame: this.annFrame,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to start test detect');
        return;
      }
      this.navigate('dashboard');
    } catch (e) {
      alert('Connection error');
    }
  },

  // ----------------------------------------------------------------
  // Training
  // ----------------------------------------------------------------
  // Cached session data for stats recalculation
  _trainSessions: [],

  async loadTrainingInfo() {
    // Fetch sessions once and share the result for both stats and session list
    this._trainSessions = [];
    try {
      const res = await fetch('/api/sessions');
      this._trainSessions = await res.json();
    } catch (e) { /* offline */ }

    this.renderTrainSessionList(this._trainSessions);
    this.recalcSelectedStats();

    // Fetch current training status
    try {
      const res = await fetch('/api/training/status');
      if (res.ok) {
        const data = await res.json();
        this.updateTraining(data);
      }
    } catch (e) { /* offline */ }
  },

  renderTrainSessionList(sessions) {
    const container = document.getElementById('train-session-list');
    if (!container) return;

    // Only show sessions that have annotated frames
    const annotated = (sessions || []).filter(s => s.annotated_count > 0);
    if (annotated.length === 0) {
      container.innerHTML = '<div class="empty-state">No annotated sessions</div>';
      return;
    }

    container.innerHTML = annotated.map(s => `
      <label class="train-session-row">
        <input type="checkbox" class="train-session-cb" value="${s.name}"
               onchange="App.recalcSelectedStats()">
        <span class="train-session-name">${s.name}</span>
        <span class="train-session-count">${s.annotated_count} annotated</span>
      </label>
    `).join('');
  },

  toggleAllTrainSessions(checked) {
    document.querySelectorAll('.train-session-cb').forEach(cb => {
      cb.checked = checked;
    });
    this.recalcSelectedStats();
  },

  getSelectedTrainSessions() {
    const cbs = document.querySelectorAll('.train-session-cb:checked');
    return Array.from(cbs).map(cb => cb.value);
  },

  recalcSelectedStats() {
    const selected = new Set(this.getSelectedTrainSessions());
    let totalFrames = 0;
    let totalAnnotated = 0;
    for (const s of (this._trainSessions || [])) {
      if (selected.has(s.name)) {
        totalFrames += s.frame_count || 0;
        totalAnnotated += s.annotated_count || 0;
      }
    }

    const el1 = document.getElementById('stat-total-frames');
    const el2 = document.getElementById('stat-total-annotated');
    if (el1) el1.textContent = totalFrames;
    if (el2) el2.textContent = totalAnnotated;
  },

  async updateDatasetStats() {
    try {
      const res = await fetch('/api/sessions');
      this._trainSessions = await res.json();
      this.recalcSelectedStats();
    } catch (e) { /* offline */ }
  },

  async startTraining() {
    const epochs = parseInt(document.getElementById('train-epochs').value) || 50;
    const batchSize = parseInt(document.getElementById('train-batch').value) || 4;
    const imgSize = parseInt(document.getElementById('train-imgsize').value) || 640;
    const lr0 = parseFloat(document.getElementById('train-lr0').value) || 0.001;
    const lrf = parseFloat(document.getElementById('train-lrf').value) || 0.1;
    const freeze = parseInt(document.getElementById('train-freeze').value) || 10;
    const flipud = parseFloat(document.getElementById('train-flipud').value) || 0.5;
    const amp = document.getElementById('train-amp').value === 'true';
    const sessions = this.getSelectedTrainSessions();

    if (sessions.length === 0) {
      alert('Select at least one session.');
      return;
    }

    try {
      const res = await fetch('/api/training/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          epochs: epochs,
          batch_size: batchSize,
          img_size: imgSize,
          sessions: sessions,
          lr0: lr0,
          lrf: lrf,
          freeze: freeze,
          flipud: flipud,
          amp: amp,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to start training');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  async stopTraining() {
    try {
      const res = await fetch('/api/training/stop', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to stop');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  // ----------------------------------------------------------------
  // Evaluation
  // ----------------------------------------------------------------
  _evalSessions: [],

  async loadEvaluationPage() {
    // Load models into eval dropdown
    await this.loadEvalModels();
    // Load sessions
    await this.loadEvalSessions();
    // Fetch current status
    try {
      const res = await fetch('/api/evaluation/status');
      if (res.ok) {
        const data = await res.json();
        this.updateEvaluation(data);
      }
    } catch (e) { /* offline */ }
    // Load history
    this.loadEvalHistory();
  },

  async loadEvalModels() {
    const select = document.getElementById('eval-model-select');
    if (!select) return;
    try {
      const res = await fetch('/api/models');
      if (!res.ok) return;
      const data = await res.json();
      const models = data.models || [];
      select.innerHTML = '<option value="">--</option>';
      for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m.path;
        opt.textContent = `${m.name} (${m.size_mb}MB)`;
        select.appendChild(opt);
      }
    } catch (e) { /* offline */ }
  },

  async loadEvalSessions() {
    this._evalSessions = [];
    try {
      const res = await fetch('/api/sessions');
      this._evalSessions = await res.json();
    } catch (e) { /* offline */ }
    this.renderEvalSessionList(this._evalSessions);
  },

  renderEvalSessionList(sessions) {
    const container = document.getElementById('eval-session-list');
    if (!container) return;
    const annotated = (sessions || []).filter(s => s.annotated_count > 0);
    if (annotated.length === 0) {
      container.innerHTML = '<div class="empty-state">No annotated sessions</div>';
      return;
    }
    container.innerHTML = annotated.map(s => `
      <label class="train-session-row">
        <input type="checkbox" class="eval-session-cb" value="${s.name}">
        <span class="train-session-name">${s.name}</span>
        <span class="train-session-count">${s.annotated_count} annotated</span>
      </label>
    `).join('');
  },

  toggleAllEvalSessions(checked) {
    document.querySelectorAll('.eval-session-cb').forEach(cb => {
      cb.checked = checked;
    });
  },

  getSelectedEvalSessions() {
    const cbs = document.querySelectorAll('.eval-session-cb:checked');
    return Array.from(cbs).map(cb => cb.value);
  },

  async startEvaluation() {
    const modelPath = document.getElementById('eval-model-select').value;
    if (!modelPath) {
      alert('Select a model.');
      return;
    }
    const sessions = this.getSelectedEvalSessions();
    if (sessions.length === 0) {
      alert('Select at least one session.');
      return;
    }

    try {
      const res = await fetch('/api/evaluation/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model_path: modelPath,
          sessions: sessions,
          conf: parseFloat(document.getElementById('eval-conf').value) || 0.25,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to start evaluation');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  async stopEvaluation() {
    try {
      const res = await fetch('/api/evaluation/stop', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to stop');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  updateEvaluation(data) {
    if (!data) return;
    const phase = document.getElementById('eval-phase');
    const progress = document.getElementById('eval-progress');
    const detail = document.getElementById('eval-detail');
    const btnStart = document.getElementById('btn-eval-start');
    const btnStop = document.getElementById('btn-eval-stop');

    if (data.running) {
      const pct = data.total_frames > 0
        ? Math.round((data.current_frame / data.total_frames) * 100) : 0;
      if (phase) phase.textContent = `Evaluating: ${data.phase || 'running'} (${data.model_name || ''})`;
      if (progress) progress.style.width = `${pct}%`;
      if (detail) detail.textContent =
        `Frame ${data.current_frame}/${data.total_frames}`;
      if (btnStart) btnStart.disabled = true;
      if (btnStop) btnStop.disabled = false;
    } else {
      if (data.phase === 'completed') {
        if (phase) phase.textContent = `Completed: ${data.model_name || ''}`;
        if (progress) progress.style.width = '100%';
        if (detail) detail.textContent = `Average IoU: ${Number(data.avg_iou || 0).toFixed(4)}`;
        // Reload history
        this.loadEvalHistory();
      } else {
        if (phase) phase.textContent = data.phase || 'Not running';
        if (progress) progress.style.width = '0%';
        if (detail) detail.textContent = '';
      }
      if (btnStart) btnStart.disabled = false;
      if (btnStop) btnStop.disabled = true;
    }
  },

  async loadEvalHistory() {
    const container = document.getElementById('eval-history-list');
    if (!container) return;

    // Collect evaluations from all sessions
    const sessions = this._evalSessions.length > 0
      ? this._evalSessions
      : await fetch('/api/sessions').then(r => r.json()).catch(() => []);

    const allEvals = [];
    for (const s of sessions) {
      try {
        const res = await fetch(`/api/sessions/${s.name}/evaluations`);
        if (res.ok) {
          const evals = await res.json();
          for (const ev of evals) {
            ev.session = s.name;
            allEvals.push(ev);
          }
        }
      } catch (e) { /* skip */ }
    }

    if (allEvals.length === 0) {
      container.innerHTML = '<p style="color:var(--text2)">No past evaluations</p>';
      return;
    }

    // Sort by timestamp descending
    allEvals.sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''));

    container.innerHTML = allEvals.map(ev => `
      <div class="eval-history-item"
           onclick="App.loadEvalResult('${ev.session}', '${ev.filename}')">
        <div>
          <strong>${ev.model_name}</strong>
          <br><span style="font-size:11px; color:var(--text2)">${ev.session} | ${ev.timestamp}</span>
        </div>
        <div style="text-align:right;">
          <span style="font-size:18px; font-weight:600; color:var(--accent);">${Number(ev.avg_iou).toFixed(3)}</span>
          <br><span style="font-size:11px; color:var(--text2)">${ev.total_frames} frames</span>
        </div>
      </div>
    `).join('');
  },

  async loadEvalResult(session, filename) {
    try {
      const res = await fetch(`/api/sessions/${session}/evaluations/${filename}`);
      if (!res.ok) return;
      const data = await res.json();
      this.displayEvalResults(data);
    } catch (e) {
      alert('Failed to load evaluation result');
    }
  },

  displayEvalResults(data) {
    const card = document.getElementById('eval-results-card');
    if (!card) return;
    card.style.display = 'block';

    const modelEl = document.getElementById('eval-results-model');
    const avgEl = document.getElementById('eval-avg-iou');
    const framesEl = document.getElementById('eval-total-frames');
    const tbody = document.getElementById('eval-results-tbody');

    if (modelEl) modelEl.textContent = data.model_name || '';
    if (avgEl) avgEl.textContent = Number(data.avg_iou || 0).toFixed(4);
    if (framesEl) framesEl.textContent = `${data.evaluated_frames || 0}/${data.total_frames || 0}`;

    if (tbody) {
      const rows = (data.per_frame || []).map(f => {
        const iouText = f.iou != null ? Number(f.iou).toFixed(4) : 'error';
        const iouClass = f.iou != null ? (f.iou >= 0.7 ? 'iou-good' : f.iou >= 0.4 ? 'iou-mid' : 'iou-low') : 'iou-error';
        return `<tr><td>${f.session || ''}</td><td>${f.frame || ''}</td><td class="${iouClass}">${iouText}</td></tr>`;
      });
      tbody.innerHTML = rows.join('');
    }

    // Scroll to results
    card.scrollIntoView({ behavior: 'smooth' });
  },

  // ----------------------------------------------------------------
  // Playback
  // ----------------------------------------------------------------
  _playbackPaused: false,
  _playbackSource: 'svo',

  async loadPlaybackPage() {
    await this.loadPlaybackSessions();
    await this.loadPlaybackVideos();
    try {
      const res = await fetch('/api/playback/status');
      if (res.ok) {
        this.updatePlayback(await res.json());
      }
    } catch (e) { /* offline */ }
  },

  setPlaybackSource(src) {
    this._playbackSource = src;
    const svoBtn = document.getElementById('playback-src-svo');
    const vidBtn = document.getElementById('playback-src-video');
    const svoRow = document.getElementById('playback-session-row');
    const vidRow = document.getElementById('playback-video-row');
    if (svoBtn) svoBtn.classList.toggle('active', src === 'svo');
    if (vidBtn) vidBtn.classList.toggle('active', src === 'video');
    if (svoRow) svoRow.style.display = src === 'svo' ? '' : 'none';
    if (vidRow) vidRow.style.display = src === 'video' ? '' : 'none';
  },

  async loadPlaybackVideos() {
    const select = document.getElementById('playback-video-select');
    if (!select) return;
    try {
      const res = await fetch('/api/videos');
      if (!res.ok) return;
      const videos = await res.json();
      select.innerHTML = '<option value="">--</option>';
      for (const v of videos) {
        const opt = document.createElement('option');
        opt.value = v.filename;
        const dims = v.width && v.height ? `${v.width}x${v.height}` : '?';
        const dur = v.duration_s ? `${v.duration_s}s` : '?';
        opt.textContent = `${v.filename} (${v.size_mb}MB, ${dims}, ${dur})`;
        select.appendChild(opt);
      }
    } catch (e) { /* offline */ }
  },

  async loadPlaybackSessions() {
    const select = document.getElementById('playback-session-select');
    if (!select) return;
    try {
      const res = await fetch('/api/sessions');
      if (!res.ok) return;
      const sessions = await res.json();
      const withSvo = sessions.filter(s => (s.svo2_size_mb || 0) > 0);
      select.innerHTML = '<option value="">--</option>';
      for (const s of withSvo) {
        const opt = document.createElement('option');
        opt.value = s.name;
        opt.textContent = `${s.name} (${s.svo2_size_mb}MB)`;
        select.appendChild(opt);
      }
    } catch (e) { /* offline */ }
  },

  async startPlayback() {
    let body;
    if (this._playbackSource === 'video') {
      const video = document.getElementById('playback-video-select').value;
      if (!video) {
        alert('Select a video file.');
        return;
      }
      body = { video };
    } else {
      const session = document.getElementById('playback-session-select').value;
      if (!session) {
        alert('Select a session.');
        return;
      }
      body = { session };
    }
    try {
      const res = await fetch('/api/playback/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to start playback');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  async stopPlayback() {
    try {
      const res = await fetch('/api/playback/stop', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        alert(data.detail || data.message || 'Failed to stop');
      }
      this.fetchStatus();
    } catch (e) {
      alert('Connection error');
    }
  },

  async togglePlaybackPause() {
    const next = !this._playbackPaused;
    try {
      const res = await fetch('/api/playback/pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused: next }),
      });
      if (!res.ok) {
        const data = await res.json();
        alert(data.detail || 'Failed to toggle pause');
        return;
      }
      this._playbackPaused = next;
    } catch (e) {
      alert('Connection error');
    }
  },

  updatePlayback(data) {
    if (!data) return;
    this._playbackPaused = !!data.paused;

    const phase = document.getElementById('playback-phase');
    const progress = document.getElementById('playback-progress');
    const detail = document.getElementById('playback-detail');
    const btnStart = document.getElementById('btn-playback-start');
    const btnPause = document.getElementById('btn-playback-pause');
    const btnStop = document.getElementById('btn-playback-stop');

    if (data.running) {
      const pct = data.total_frames > 0
        ? Math.round((data.current_frame / data.total_frames) * 100) : 0;
      const label = data.source_name || data.session_name || '';
      if (phase) {
        const state = data.paused ? 'paused' : (data.phase || 'playing');
        phase.textContent = `${state}: ${label}`;
      }
      if (progress) progress.style.width = `${pct}%`;
      if (detail) {
        detail.textContent =
          `Frame ${data.current_frame}/${data.total_frames}` +
          `   Skipped: ${data.skipped_frames || 0}`;
      }
      if (btnStart) btnStart.disabled = true;
      if (btnPause) {
        btnPause.disabled = false;
        btnPause.textContent = data.paused ? 'Resume' : 'Pause';
      }
      if (btnStop) btnStop.disabled = false;
    } else {
      if (phase) {
        const label = data.source_name || data.session_name || '';
        phase.textContent = data.phase === 'completed'
          ? `Completed: ${label}`
          : (data.phase || 'Not running');
      }
      if (progress) {
        progress.style.width = data.phase === 'completed' ? '100%' : '0%';
      }
      if (detail) {
        detail.textContent = data.skipped_frames
          ? `Skipped: ${data.skipped_frames}` : '';
      }
      if (btnStart) btnStart.disabled = false;
      if (btnPause) {
        btnPause.disabled = true;
        btnPause.textContent = 'Pause';
      }
      if (btnStop) btnStop.disabled = true;
    }
  },
};

// Register service worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// Start
document.addEventListener('DOMContentLoaded', () => App.init());
