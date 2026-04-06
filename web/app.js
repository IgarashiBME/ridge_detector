// Ridge Detector v2 - PWA Application
'use strict';

const App = {
  ws: null,
  wsConnected: false,
  currentMode: 'IDLE',
  currentPage: 'dashboard',
  // Annotation state
  annSession: null,
  annFrame: null,
  annPoints: [],       // legacy (kept for computed polygon output)
  annLeftLine: [],     // 2 points defining left edge line
  annRightLine: [],    // 2 points defining right edge line
  annImage: null,
  annCanvas: null,
  annCtx: null,

  // ----------------------------------------------------------------
  // Init
  // ----------------------------------------------------------------
  init() {
    this.setupRouter();
    this.connectWebSocket();
    this.setupAnnotationCanvas();

    // Initial route
    this.handleRoute();
    this.loadModels();

    // Periodic status fetch as fallback
    setInterval(() => this.fetchStatus(), 3000);
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
    };
    const navEl = document.querySelector(navMap[name]);
    if (navEl) navEl.classList.add('active');
  },

  navigate(page) {
    if (page === 'sessions') window.location.hash = '#/sessions';
    else if (page === 'dashboard') window.location.hash = '#/';
    else if (page === 'training') window.location.hash = '#/training';
    else if (page === 'evaluation') window.location.hash = '#/evaluation';
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
          channels: ['status', 'detection', 'training', 'evaluation', 'log', 'frame']
        }));
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
    if (btnRec) btnRec.disabled = !isIdle;
    if (btnDet) btnDet.disabled = !isIdle;
    if (btnStop) btnStop.disabled = isIdle;

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
  },

  updateDetection(data) {
    if (!data) return;
    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    setVal('stat-a', data.a != null ? Number(data.a).toFixed(4) : '--');
    setVal('stat-b', data.b != null ? Number(data.b).toFixed(1) : '--');
    setVal('stat-fps', data.fps != null ? Number(data.fps).toFixed(1) : '--');
    setVal('stat-serial', data.serial_count != null
      ? `${data.serial_status || ''} ${data.serial_count}` : '--');
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
    if (this.currentPage !== 'dashboard') return;
    const img = document.getElementById('live-preview');
    if (img && base64Data) {
      img.src = `data:image/jpeg;base64,${base64Data}`;
    }
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
    this.annPoints = [];

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

    // Highlight selection
    document.querySelectorAll('.frame-thumb').forEach(el => el.classList.remove('selected'));
    const thumb = document.getElementById(`thumb-${filename}`);
    if (thumb) thumb.classList.add('selected');

    document.getElementById('ann-frame-name').textContent = filename;
    document.getElementById('ann-editor').style.display = 'block';

    // Load image into canvas
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      this.annImage = img;
      this.resizeCanvas();
      this.drawAnnotation();
    };
    img.src = `/api/sessions/${this.annSession}/frames/${filename}`;

    // Load existing annotation — reconstruct left/right lines from saved polygon
    try {
      const res = await fetch(
        `/api/sessions/${this.annSession}/frames/${filename}/annotation`
      );
      const data = await res.json();
      if (data.exists && data.points && data.points.length === 4) {
        // Saved order: leftTop, rightTop, rightBottom, leftBottom
        const p = data.points;
        this.annLeftLine = [p[0], p[3]];   // left top, left bottom
        this.annRightLine = [p[1], p[2]];  // right top, right bottom
      }
    } catch (e) { /* no annotation */ }
    this._updateStepIndicator();
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

  // Compute 4-point polygon from left and right edge lines.
  // Returns array of 4 normalized [x,y] points ordered as a proper quadrilateral, or null.
  _computePolygonFromLines() {
    if (this.annLeftLine.length < 2 || this.annRightLine.length < 2) return null;

    const leftHits = this._lineImageIntersections(this.annLeftLine[0], this.annLeftLine[1]);
    const rightHits = this._lineImageIntersections(this.annRightLine[0], this.annRightLine[1]);

    if (leftHits.length < 2 || rightHits.length < 2) return null;

    // Order: leftTop, rightTop, rightBottom, leftBottom (clockwise)
    const pts = [leftHits[0], rightHits[0], rightHits[1], leftHits[1]];
    return pts;
  },

  // Get annotation step label
  _annStepLabel() {
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

  setupAnnotationCanvas() {
    const canvas = document.getElementById('annotation-canvas');
    if (!canvas) return;
    this.annCanvas = canvas;
    this.annCtx = canvas.getContext('2d');

    const handler = (e) => {
      e.preventDefault();
      if (!this.annImage) return;
      const total = this.annLeftLine.length + this.annRightLine.length;
      if (total >= 4) return;

      const rect = canvas.getBoundingClientRect();
      let clientX, clientY;
      if (e.touches) {
        clientX = e.touches[0].clientX;
        clientY = e.touches[0].clientY;
      } else {
        clientX = e.clientX;
        clientY = e.clientY;
      }

      const x = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const y = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));

      if (this.annLeftLine.length < 2) {
        this.annLeftLine.push([x, y]);
      } else {
        this.annRightLine.push([x, y]);
      }

      this._updateStepIndicator();
      this.drawAnnotation();
    };

    canvas.addEventListener('click', handler);
    canvas.addEventListener('touchstart', handler, { passive: false });

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

    // Compute polygon and fill if both lines are complete
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
      const midP1 = [(this.annLeftLine[0][0] + this.annRightLine[0][0]) / 2,
                      (this.annLeftLine[0][1] + this.annRightLine[0][1]) / 2];
      const midP2 = [(this.annLeftLine[1][0] + this.annRightLine[1][0]) / 2,
                      (this.annLeftLine[1][1] + this.annRightLine[1][1]) / 2];
      drawExtLine(midP1, midP2, '#00ff00');
    }

    // Draw left line (cyan)
    if (leftLen === 2) drawExtLine(this.annLeftLine[0], this.annLeftLine[1], '#00e5ff');

    // Draw right line (orange)
    if (rightLen === 2) drawExtLine(this.annRightLine[0], this.annRightLine[1], '#ffa500');

    // Draw clicked points
    for (let i = 0; i < leftLen; i++) drawPt(this.annLeftLine[i], '#00e5ff', 'L' + (i + 1));
    for (let i = 0; i < rightLen; i++) drawPt(this.annRightLine[i], '#ffa500', 'R' + (i + 1));
  },

  async saveAnnotation() {
    if (!this.annSession || !this.annFrame) return;

    const poly = this._computePolygonFromLines();
    if (!poly) {
      alert('Place 2 points for each edge line (4 points total).');
      return;
    }

    try {
      const res = await fetch(
        `/api/sessions/${this.annSession}/frames/${this.annFrame}/annotation`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ points: poly }),
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

  undoLastPoint() {
    if (this.annRightLine.length > 0) {
      this.annRightLine.pop();
    } else if (this.annLeftLine.length > 0) {
      this.annLeftLine.pop();
    }
    this._updateStepIndicator();
    this.drawAnnotation();
  },

  clearAnnotation() {
    this.annLeftLine = [];
    this.annRightLine = [];
    this.annPoints = [];
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
        <input type="checkbox" class="train-session-cb" value="${s.name}" checked
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
        <input type="checkbox" class="eval-session-cb" value="${s.name}" checked>
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
};

// Register service worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// Start
document.addEventListener('DOMContentLoaded', () => App.init());
