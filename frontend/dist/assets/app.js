/* ============================================================
   UC Data Quality Explorer — Single-Page Application
   ============================================================ */

let state = {
  scanned: false,
  scanning: false,
  scanResult: null,
  scanProgress: null,
  scanError: null,
  schemas: [],
  tables: [],
  groups: [],
  selectedTable: null,
  compareResult: null,
  threshold: 0.5,
  cacheInfo: null,   // { cached_at, age_days } when loaded from cache
};

// ===== Router =====
function getPage() {
  const hash = location.hash || '#/';
  if (hash.startsWith('#/catalog')) return 'catalog';
  if (hash.startsWith('#/duplicates')) return 'duplicates';
  if (hash.startsWith('#/compare')) return 'compare';
  return 'dashboard';
}

function navigate() {
  const page = getPage();
  document.querySelectorAll('.nav-link').forEach(l => {
    l.classList.toggle('active', l.dataset.page === page);
  });
  render(page);
}

window.addEventListener('hashchange', navigate);
window.addEventListener('load', async () => {
  navigate();
  await tryLoadFromCache();
});

async function tryLoadFromCache() {
  try {
    const status = await API.cacheStatus();
    if (!status.valid) {
      console.log('Cache not valid:', status.reason);
      return;
    }

    state.scanning = true;
    state.scanProgress = {
      state: 'running',
      message: 'Loading from cache\u2026',
      catalogs_done: 0, catalogs_total: 0,
      catalogs_scanned: [], errors: [],
    };
    renderDashboard();

    const cached = await API.loadFromCache();

    state.scanResult = cached.scan_result;
    state.cacheInfo = {
      cached_at: cached.cached_at,
      age_days: cached.cache_age_days,
    };

    showPostScanLoading('Loading schemas and tables\u2026');
    state.schemas = await API.getSchemas();
    state.tables = await API.getTables();
    state.groups = cached.groups || [];
    state.scanned = true;
  } catch (e) {
    console.warn('Cache load failed (will require manual scan):', e);
  } finally {
    state.scanning = false;
    state.scanProgress = null;
    renderDashboard();
  }
}

// ===== Render =====
const $ = id => document.getElementById(id);
const main = () => $('main-content');

function render(page) {
  switch (page) {
    case 'dashboard': renderDashboard(); break;
    case 'catalog': renderCatalog(); break;
    case 'duplicates': renderDuplicates(); break;
    case 'compare': renderCompare(); break;
  }
}

// ===== Utilities =====
function similarityColor(score) {
  if (score >= 0.8) return 'var(--red)';
  if (score >= 0.6) return 'var(--yellow)';
  return 'var(--green)';
}

function formatNumber(n) {
  if (n == null) return '\u2014';
  return n.toLocaleString();
}

function timeAgo(ts) {
  if (!ts) return '\u2014';
  const d = new Date(ts);
  const diff = Date.now() - d.getTime();
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  return `${Math.floor(diff / 86400000)}d ago`;
}

function loading(msg = 'Loading...') {
  return `<div class="loading"><div class="spinner"></div>${msg}</div>`;
}

function permBadges(permissions) {
  if (!permissions || !permissions.length) return '<span class="tag tag-yellow">No grants found</span>';
  return permissions.map(p => {
    const isWrite = p.privileges.some(pr =>
      pr === 'ALL_PRIVILEGES' || pr === 'MODIFY' || pr === 'CREATE'
    );
    const isRead = p.privileges.some(pr => pr === 'SELECT');
    let tags = '';
    if (isWrite) tags += `<span class="tag tag-blue">WRITE</span> `;
    else if (isRead) tags += `<span class="tag tag-green">READ</span> `;
    else tags += p.privileges.map(pr => `<span class="tag tag-accent">${pr}</span>`).join(' ');
    return `<div class="perm-row">
      <span class="perm-principal">${p.principal}</span>
      <span class="perm-badges">${tags}</span>
    </div>`;
  }).join('');
}

// ===== Dashboard =====
async function renderDashboard() {
  const sr = state.scanResult;
  main().innerHTML = `
    <h2 class="page-title">Dashboard</h2>
    <p class="page-desc">Scan all accessible Unity Catalog metadata, detect duplicate datasets, and identify gold-standard tables.</p>
    ${state.cacheInfo ? `<div class="card" style="margin-bottom:16px;padding:12px;display:flex;align-items:center;gap:8px;border-left:3px solid var(--green)">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
      <span style="font-size:13px;color:var(--text-muted)">Loaded from cache \u2014 <strong>${Math.floor(state.cacheInfo.age_days)}d ago</strong>.
      Click \u201cScan All Catalogs\u201d to force a fresh scan.</span>
    </div>` : ''}
    <div style="margin-bottom:20px">
      <button class="btn btn-primary" id="scan-btn" ${state.scanning ? 'disabled' : ''}>
        ${state.scanning ? '<div class="spinner" style="width:14px;height:14px;margin-right:6px"></div> Scanning\u2026' : 'Scan All Catalogs'}
      </button>
    </div>
    <div id="scan-progress"></div>
    ${state.scanError ? `<div style="background:var(--red-bg, #2d1b1b);border:1px solid var(--red, #e74c3c);border-radius:6px;padding:16px;margin-bottom:16px">
      <div style="font-weight:700;font-size:14px;color:var(--red, #e74c3c);margin-bottom:8px">Scan failed</div>
      <pre style="font-size:12px;color:var(--text-muted);white-space:pre-wrap;word-break:break-word;margin:0">${state.scanError}</pre>
    </div>` : ''}
    ${sr ? renderScanSummary(sr) : '<div class="stat-card"><div class="stat-label">Status</div><div class="stat-value" style="font-size:16px;color:var(--text-muted)">Click \u201cScan All Catalogs\u201d to begin</div></div>'}
    <div id="top-duplicates"></div>
  `;

  $('scan-btn').onclick = doScan;
  if (state.groups.length) renderTopDuplicates();
}

function renderScanSummary(sr) {
  const t = sr.total;
  const cats = sr.catalogs_scanned || [];
  const perCat = sr.per_catalog || {};
  const errors = sr.errors || [];

  let errorsHtml = '';
  if (errors.length) {
    errorsHtml = `
      <div style="background:var(--red-bg, #2d1b1b);border:1px solid var(--red, #e74c3c);border-radius:6px;padding:12px;margin-bottom:16px">
        <div style="font-weight:600;font-size:13px;color:var(--red, #e74c3c);margin-bottom:6px">${errors.length} warning(s) during scan</div>
        ${errors.slice(0, 10).map(e => `<div style="font-size:12px;color:var(--text-muted);margin-bottom:2px">\u2022 ${e}</div>`).join('')}
        ${errors.length > 10 ? `<div style="font-size:12px;color:var(--text-muted)">+ ${errors.length - 10} more</div>` : ''}
      </div>
    `;
  }

  return `
    ${errorsHtml}
    <div class="stats-grid" id="stats-grid">
      <div class="stat-card"><div class="stat-label">Catalogs Scanned</div><div class="stat-value">${t.catalog_count}</div></div>
      <div class="stat-card"><div class="stat-label">Schemas</div><div class="stat-value">${t.schema_count}</div></div>
      <div class="stat-card"><div class="stat-label">Tables</div><div class="stat-value">${t.table_count}</div></div>
      <div class="stat-card"><div class="stat-label">Columns</div><div class="stat-value">${t.column_count}</div></div>
      <div class="stat-card"><div class="stat-label">Duplicate Groups</div><div class="stat-value accent">${state.groups.length}</div></div>
    </div>
    <div class="section" style="margin-top:20px">
      <div class="section-title">Catalogs</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        ${cats.map(c => {
          const info = perCat[c] || {};
          const err = info.error;
          return `<div class="catalog-chip ${err ? 'catalog-chip-error' : ''}">
            <strong>${c}</strong>
            <span class="catalog-chip-detail">${err ? 'error' : `${info.schema_count || 0} schemas \u00B7 ${info.table_count || 0} tables`}</span>
          </div>`;
        }).join('')}
      </div>
    </div>
  `;
}

// ===== Scan helpers =====
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function renderProgressPanel(status) {
  const done = status.catalogs_done || 0;
  const total = status.catalogs_total || 0;
  const message = status.message || 'Scanning\u2026';
  const scanned = status.catalogs_scanned || [];
  const errors = status.errors || [];
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  let html = `
    <div class="card" style="margin-bottom:16px;padding:16px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <div class="spinner" style="width:14px;height:14px"></div>
        <span style="font-weight:600;font-size:14px">${message}</span>
      </div>
  `;

  if (total > 0) {
    html += `
      <div style="background:var(--bg);border-radius:4px;height:8px;overflow:hidden;margin-bottom:8px">
        <div style="background:var(--accent);height:100%;width:${pct}%;transition:width 0.3s ease"></div>
      </div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">${done} of ${total} catalogs (${pct}%)</div>
    `;
  }

  if (scanned.length) {
    html += `
      <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">
        ${scanned.map(c => `<span class="tag tag-green" style="font-size:11px">\u2713 ${c}</span>`).join('')}
      </div>
    `;
  }

  if (errors.length) {
    html += `
      <div style="background:var(--red-bg, #2d1b1b);border:1px solid var(--red, #e74c3c);border-radius:4px;padding:8px;margin-top:8px">
        <div style="font-weight:600;font-size:12px;color:var(--red, #e74c3c);margin-bottom:4px">${errors.length} warning(s)</div>
        ${errors.slice(0, 5).map(e => `<div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">\u2022 ${e}</div>`).join('')}
        ${errors.length > 5 ? `<div style="font-size:11px;color:var(--text-muted)">+ ${errors.length - 5} more</div>` : ''}
      </div>
    `;
  }

  html += '</div>';
  return html;
}

function updateScanProgress(status) {
  const el = document.getElementById('scan-progress');
  if (!el) return;
  el.innerHTML = renderProgressPanel(status);
}

function showPostScanLoading(message) {
  const el = document.getElementById('scan-progress');
  if (!el) return;
  el.innerHTML = `
    <div class="card" style="margin-bottom:16px;padding:16px">
      <div style="display:flex;align-items:center;gap:8px">
        <div class="spinner" style="width:14px;height:14px"></div>
        <span style="font-weight:600;font-size:14px">${message}</span>
      </div>
    </div>
  `;
}

async function pollScanUntilDone() {
  let consecutiveErrors = 0;

  while (true) {
    await sleep(2000);

    try {
      const status = await API.scanStatus();
      consecutiveErrors = 0;  // reset on success
      state.scanProgress = status;
      updateScanProgress(status);

      if (status.state === 'completed') return status.result;
      if (status.state === 'failed') {
        const errors = status.errors || [];
        const errorDetail = errors.length ? `\n\nDetails:\n${errors.join('\n')}` : '';
        throw new Error((status.error || 'Scan failed') + errorDetail);
      }
    } catch (e) {
      if (e.message && (e.message.includes('Scan failed') || e.message.includes('API error: 5'))) {
        throw e;  // real server-side failure
      }
      consecutiveErrors++;
      console.warn(`Poll attempt failed (${consecutiveErrors}/10): ${e.message}`);
      if (consecutiveErrors >= 10) {
        throw new Error('Lost connection to scan \u2014 server may still be processing');
      }
      // transient network/timeout error \u2014 keep polling
    }
  }
}

async function doScan() {
  state.scanning = true;
  state.scanProgress = null;
  state.scanError = null;
  renderDashboard();

  try {
    // Start the background scan (returns immediately)
    await API.startScan();

    // Poll until the scan completes or fails
    const scanResult = await pollScanUntilDone();
    state.scanResult = scanResult;

    // Fetch supplementary data now that the scan is done
    showPostScanLoading('Loading schemas and tables\u2026');
    state.schemas = await API.getSchemas();
    state.tables = await API.getTables();

    showPostScanLoading('Loading duplicate groups\u2026');
    state.groups = await API.getGroups();
    state.scanned = true;
    state.cacheInfo = null;  // fresh scan, not from cache
  } catch (e) {
    console.error('Scan failed:', e);
    state.scanError = e.message;
  }

  state.scanning = false;
  state.scanProgress = null;
  renderDashboard();
}

function renderTopDuplicates() {
  const el = $('top-duplicates');
  if (!state.groups.length) {
    el.innerHTML = '<div class="card"><div class="empty-state"><h3>No duplicates detected</h3><p>All tables appear unique across all catalogs.</p></div></div>';
    return;
  }
  el.innerHTML = `
    <div class="section-title" style="margin-top:24px">Top Duplicate Groups</div>
    ${state.groups.slice(0, 5).map(g => renderDupGroupCard(g)).join('')}
    ${state.groups.length > 5 ? `<p style="color:var(--text-muted);font-size:13px">+ ${state.groups.length - 5} more groups. <a href="#/duplicates" style="color:var(--accent)">View all</a></p>` : ''}
  `;
}

// ===== Catalog Explorer =====
async function renderCatalog() {
  if (!state.scanned) {
    main().innerHTML = `
      <h2 class="page-title">Catalog Explorer</h2>
      <p class="page-desc">Browse schemas and tables across all catalogs.</p>
      <div class="empty-state"><h3>No data scanned yet</h3><p>Go to the Dashboard and click \u201cScan All Catalogs\u201d first.</p></div>
    `;
    return;
  }

  const catalogs = (state.scanResult?.catalogs_scanned || []);

  main().innerHTML = `
    <h2 class="page-title">Catalog Explorer</h2>
    <p class="page-desc">Browsing <strong>${catalogs.length}</strong> catalog${catalogs.length !== 1 ? 's' : ''}. Click a table to see its metadata and permissions.</p>
    <div class="tree-container">
      <div class="tree-panel" id="tree-panel">${renderTree(catalogs)}</div>
      <div class="detail-panel" id="detail-panel">
        <div class="empty-state"><h3>Select a table</h3><p>Click on a table in the tree to view details.</p></div>
      </div>
    </div>
  `;

  $('tree-panel').onclick = async (e) => {
    const tableEl = e.target.closest('.tree-table');
    if (tableEl) {
      const { catalog, schema, table } = tableEl.dataset;
      document.querySelectorAll('.tree-table').forEach(t => t.classList.remove('active'));
      tableEl.classList.add('active');
      $('detail-panel').innerHTML = loading('Loading table details\u2026');
      try {
        const info = await API.getTable(catalog, schema, table);
        state.selectedTable = info;
        renderTableDetail(info);
      } catch (e) {
        $('detail-panel').innerHTML = `<div class="empty-state"><h3>Error</h3><p>${e.message}</p></div>`;
      }
    }
    const toggle = e.target.closest('.tree-toggle');
    if (toggle) {
      const children = toggle.nextElementSibling;
      if (children) children.style.display = children.style.display === 'none' ? 'block' : 'none';
    }
  };
}

function renderTree(catalogs) {
  return catalogs.map(catName => {
    const catSchemas = state.schemas.filter(s => s.catalog === catName);
    return `
      <div class="tree-catalog">
        <div class="tree-toggle tree-catalog-name">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
          ${catName}
          <span class="count">${catSchemas.length}</span>
        </div>
        <div class="tree-catalog-children">
          ${catSchemas.map(s => {
            const tables = state.tables.filter(t => t.catalog === catName && t.schema === s.name);
            return `
              <div class="tree-schema">
                <div class="tree-toggle tree-schema-name">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>
                  ${s.name}
                  <span class="count">${s.table_count}</span>
                </div>
                <div class="tree-tables">
                  ${tables.map(t => `<div class="tree-table" data-catalog="${catName}" data-schema="${t.schema}" data-table="${t.name}">${t.name}</div>`).join('')}
                </div>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }).join('');
}

function renderTableDetail(info) {
  const dp = $('detail-panel');
  dp.innerHTML = `
    <h3 style="font-size:18px;font-weight:700;margin-bottom:4px">${info.name}</h3>
    <p style="font-size:12px;color:var(--text-muted);margin-bottom:16px">${info.full_name}</p>

    ${info.comment ? `<div class="card" style="margin-bottom:16px;background:var(--bg)"><p style="font-size:13px;color:var(--text-muted)">${info.comment}</p></div>` : ''}

    <div class="stats-grid" style="grid-template-columns:repeat(2,1fr);margin-bottom:20px">
      <div class="stat-card"><div class="stat-label">Columns</div><div class="stat-value" style="font-size:20px">${info.columns.length}</div></div>
      <div class="stat-card"><div class="stat-label">Owner</div><div class="stat-value" style="font-size:14px">${info.owner || '\u2014'}</div></div>
    </div>

    <div class="section">
      <div class="section-title">Access Permissions</div>
      <div class="perm-list">${permBadges(info.permissions)}</div>
    </div>

    <div class="section">
      <div class="section-title">Columns</div>
      <table class="data-table">
        <thead><tr><th>#</th><th>Name</th><th>Type</th><th>Nullable</th></tr></thead>
        <tbody>
          ${info.columns.map((c, i) => `
            <tr>
              <td style="color:var(--text-dim)">${i + 1}</td>
              <td style="font-weight:600">${c.name}</td>
              <td><span class="tag tag-accent">${c.type_name}</span></td>
              <td>${c.nullable ? '\u2713' : '\u2717'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ===== Duplicates =====
async function renderDuplicates() {
  if (!state.scanned) {
    main().innerHTML = `
      <h2 class="page-title">Duplicate Detection</h2>
      <p class="page-desc">Find duplicate and similar datasets across all catalogs.</p>
      <div class="empty-state"><h3>No data scanned yet</h3><p>Go to the Dashboard and click \u201cScan All Catalogs\u201d first.</p></div>
    `;
    return;
  }

  main().innerHTML = `
    <h2 class="page-title">Duplicate Detection</h2>
    <p class="page-desc">Tables across <strong>${(state.scanResult?.catalogs_scanned || []).length}</strong> catalog(s) grouped by similarity. The gold badge marks the recommended standard dataset.</p>
    <div class="threshold-control">
      <label>Similarity Threshold</label>
      <input type="range" id="threshold-slider" min="0.1" max="1.0" step="0.05" value="${state.threshold}" />
      <span class="threshold-value" id="threshold-val">${(state.threshold * 100).toFixed(0)}%</span>
      <button class="btn btn-outline btn-sm" id="redetect-btn">Re-detect</button>
    </div>
    <div id="dup-groups">${state.groups.length ? state.groups.map(g => renderDupGroupCard(g)).join('') : '<div class="empty-state"><h3>No duplicates found</h3><p>Try lowering the threshold.</p></div>'}</div>
  `;

  $('threshold-slider').oninput = (e) => {
    state.threshold = parseFloat(e.target.value);
    $('threshold-val').textContent = (state.threshold * 100).toFixed(0) + '%';
  };

  $('redetect-btn').onclick = async () => {
    $('dup-groups').innerHTML = loading('Detecting duplicates\u2026');
    state.groups = await API.detectDuplicates(state.threshold);
    $('dup-groups').innerHTML = state.groups.length
      ? state.groups.map(g => renderDupGroupCard(g)).join('')
      : '<div class="empty-state"><h3>No duplicates found</h3><p>Try lowering the threshold.</p></div>';
  };
}

function renderDupGroupCard(g) {
  const maxScore = g.pairs.length ? Math.max(...g.pairs.map(p => p.composite_score)) : 0;
  const catalogSet = new Set(g.tables.map(t => t.split('.')[0]));
  const crossCatalog = catalogSet.size > 1;

  return `
    <div class="dup-group">
      <div class="dup-group-header">
        <span class="dup-group-title">${g.label} \u2014 ${g.tables.length} tables${crossCatalog ? ` across ${catalogSet.size} catalogs` : ''}</span>
        <span class="similarity-score" style="color:${similarityColor(maxScore)}">${(maxScore * 100).toFixed(0)}% max similarity</span>
      </div>
      <div class="similarity-bar"><div class="similarity-bar-fill" style="width:${maxScore * 100}%;background:${similarityColor(maxScore)}"></div></div>
      <div class="dup-tables-list" style="margin-top:10px">
        ${g.tables.map(t => {
          const isGold = t === g.gold_standard;
          const score = g.gold_scores[t];
          return `<span class="dup-table-tag ${isGold ? 'gold' : ''}" title="Gold score: ${score ?? '\u2014'}">${isGold ? '\u2605 ' : ''}${t}</span>`;
        }).join('')}
      </div>
      ${g.gold_standard ? `<div style="margin-top:8px"><span class="gold-badge">\u2605 Gold Standard: ${g.gold_standard}</span></div>` : ''}
      <div style="margin-top:12px">
        <table class="data-table">
          <thead><tr><th>Table A</th><th>Table B</th><th>Columns</th><th>Types</th><th>Name</th><th>Score</th><th></th></tr></thead>
          <tbody>
            ${g.pairs.slice(0, 6).map(p => {
              return `<tr>
                <td style="font-weight:500">${p.table_a}</td>
                <td style="font-weight:500">${p.table_b}</td>
                <td>${(p.column_similarity * 100).toFixed(0)}%</td>
                <td>${(p.type_similarity * 100).toFixed(0)}%</td>
                <td>${(p.name_similarity * 100).toFixed(0)}%</td>
                <td><span class="similarity-score" style="color:${similarityColor(p.composite_score)}">${(p.composite_score * 100).toFixed(0)}%</span></td>
                <td><button class="btn btn-outline btn-sm compare-btn" data-a="${p.table_a}" data-b="${p.table_b}">Compare</button></td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.compare-btn');
  if (btn) {
    const [c1, s1, t1] = btn.dataset.a.split('.');
    const [c2, s2, t2] = btn.dataset.b.split('.');
    location.hash = `#/compare?c1=${c1}&s1=${s1}&t1=${t1}&c2=${c2}&s2=${s2}&t2=${t2}`;
  }
});

// ===== Compare — helpers for cascading selectors =====
function catalogsForPicker() {
  return [...new Set(state.tables.map(t => t.catalog))].sort();
}

function schemasForCatalog(cat) {
  return [...new Set(state.tables.filter(t => t.catalog === cat).map(t => t.schema))].sort();
}

function tablesForSchema(cat, sch) {
  return state.tables.filter(t => t.catalog === cat && t.schema === sch).sort((a, b) => a.name.localeCompare(b.name));
}

function renderPickerOptions(items, selected, labelFn = x => x) {
  return items.map(i => {
    const val = typeof i === 'string' ? i : i.name;
    const label = typeof i === 'string' ? i : labelFn(i);
    return `<option value="${val}" ${val === selected ? 'selected' : ''}>${label}</option>`;
  }).join('');
}

function buildPickerHtml(side, cat, sch, tbl) {
  const catalogs = catalogsForPicker();
  const schemas = cat ? schemasForCatalog(cat) : [];
  const tables = (cat && sch) ? tablesForSchema(cat, sch) : [];

  return `
    <div class="picker-side">
      <span class="picker-label">Table ${side.toUpperCase()}</span>
      <div class="picker-row">
        <div class="picker-field">
          <span class="field-label">Catalog</span>
          <select id="cat-${side}">
            <option value="">Select catalog\u2026</option>
            ${renderPickerOptions(catalogs, cat)}
          </select>
        </div>
        <div class="picker-field">
          <span class="field-label">Schema</span>
          <select id="sch-${side}" ${!cat ? 'disabled' : ''}>
            <option value="">Select schema\u2026</option>
            ${renderPickerOptions(schemas, sch)}
          </select>
        </div>
        <div class="picker-field">
          <span class="field-label">Table</span>
          <select id="tbl-${side}" ${!sch ? 'disabled' : ''}>
            <option value="">Select table\u2026</option>
            ${renderPickerOptions(tables, tbl, t => t.name)}
          </select>
        </div>
      </div>
      ${(cat && sch && tbl) ? `<div class="picker-selected">${cat}.${sch}.${tbl}</div>` : ''}
    </div>
  `;
}

let _pickA = { cat: null, sch: null, tbl: null };
let _pickB = { cat: null, sch: null, tbl: null };

function bindPicker(side, pick) {
  const catSel = $(`cat-${side}`);
  const schSel = $(`sch-${side}`);
  const tblSel = $(`tbl-${side}`);

  catSel.onchange = () => {
    pick.cat = catSel.value || null;
    pick.sch = null;
    pick.tbl = null;
    refreshPickers();
  };
  schSel.onchange = () => {
    pick.sch = schSel.value || null;
    pick.tbl = null;
    refreshPickers();
  };
  tblSel.onchange = () => {
    pick.tbl = tblSel.value || null;
    refreshPickers();
  };
}

function refreshPickers() {
  const form = $('compare-form');
  if (!form) return;
  form.innerHTML = `
    <div class="picker-grid">
      ${buildPickerHtml('a', _pickA.cat, _pickA.sch, _pickA.tbl)}
      ${buildPickerHtml('b', _pickB.cat, _pickB.sch, _pickB.tbl)}
    </div>
    <button class="btn btn-primary" id="compare-go" ${!(_pickA.tbl && _pickB.tbl) ? 'disabled' : ''}>Compare</button>
  `;
  bindPicker('a', _pickA);
  bindPicker('b', _pickB);
  $('compare-go').onclick = doCompare;
}

// ===== Compare =====
async function renderCompare() {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const c1 = params.get('c1'), s1 = params.get('s1'), t1 = params.get('t1');
  const c2 = params.get('c2'), s2 = params.get('s2'), t2 = params.get('t2');

  if (!state.scanned) {
    main().innerHTML = `
      <h2 class="page-title">Compare Tables</h2>
      <p class="page-desc">Side-by-side comparison of two tables.</p>
      <div class="empty-state"><h3>No data scanned yet</h3><p>Go to the Dashboard and click \u201cScan All Catalogs\u201d first.</p></div>
    `;
    return;
  }

  _pickA = { cat: c1 || null, sch: s1 || null, tbl: t1 || null };
  _pickB = { cat: c2 || null, sch: s2 || null, tbl: t2 || null };

  main().innerHTML = `
    <h2 class="page-title">Compare Tables</h2>
    <p class="page-desc">Side-by-side schema diff, permissions, and sample data comparison.</p>
    <div id="compare-form"></div>
    <div id="compare-result"></div>
  `;

  refreshPickers();

  if (c1 && s1 && t1 && c2 && s2 && t2) {
    doCompare();
  }
}

async function doCompare() {
  const { cat: c1, sch: s1, tbl: t1 } = _pickA;
  const { cat: c2, sch: s2, tbl: t2 } = _pickB;
  if (!c1 || !s1 || !t1 || !c2 || !s2 || !t2) { alert('Select two tables'); return; }

  const el = $('compare-result');
  el.innerHTML = loading('Comparing tables\u2026');

  try {
    const [result, sampleA, sampleB] = await Promise.all([
      API.compareTables(c1, s1, t1, c2, s2, t2),
      API.getSample(c1, s1, t1).catch(() => null),
      API.getSample(c2, s2, t2).catch(() => null),
    ]);
    state.compareResult = result;
    renderCompareResult(result, sampleA, sampleB);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><h3>Comparison failed</h3><p>${e.message}</p></div>`;
  }
}

function renderCompareResult(r, sampleA, sampleB) {
  const el = $('compare-result');
  el.innerHTML = `
    <div class="compare-grid" style="margin-bottom:20px">
      <div class="card">
        <h4 style="font-weight:700;margin-bottom:8px">${r.table_a.full_name}</h4>
        <div style="font-size:13px;color:var(--text-muted)">
          <div>Columns: <strong>${r.table_a.column_count}</strong></div>
          <div>Owner: ${r.table_a.owner || '\u2014'}</div>
          ${r.table_a.comment ? `<div style="margin-top:6px;font-style:italic">${r.table_a.comment}</div>` : ''}
        </div>
      </div>
      <div class="card">
        <h4 style="font-weight:700;margin-bottom:8px">${r.table_b.full_name}</h4>
        <div style="font-size:13px;color:var(--text-muted)">
          <div>Columns: <strong>${r.table_b.column_count}</strong></div>
          <div>Owner: ${r.table_b.owner || '\u2014'}</div>
          ${r.table_b.comment ? `<div style="margin-top:6px;font-style:italic">${r.table_b.comment}</div>` : ''}
        </div>
      </div>
    </div>

    <div class="stats-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:20px">
      <div class="stat-card"><div class="stat-label">Shared Columns</div><div class="stat-value" style="font-size:24px;color:var(--green)">${r.shared_columns}</div></div>
      <div class="stat-card"><div class="stat-label">Only in A</div><div class="stat-value" style="font-size:24px;color:var(--red)">${r.only_a_columns}</div></div>
      <div class="stat-card"><div class="stat-label">Only in B</div><div class="stat-value" style="font-size:24px;color:var(--blue)">${r.only_b_columns}</div></div>
    </div>

    ${r.permissions_diff && r.permissions_diff.length ? `
    <div class="section">
      <div class="section-title">Access Permissions Comparison</div>
      <table class="data-table">
        <thead><tr><th>Principal</th><th>${r.table_a.full_name}</th><th>${r.table_b.full_name}</th><th>Match</th></tr></thead>
        <tbody>
          ${r.permissions_diff.map(p => `
            <tr>
              <td style="font-weight:600">${p.principal}</td>
              <td>${(p.privileges_a || []).map(pr => `<span class="tag ${pr === 'SELECT' ? 'tag-green' : 'tag-blue'}">${pr}</span> `).join('') || '\u2014'}</td>
              <td>${(p.privileges_b || []).map(pr => `<span class="tag ${pr === 'SELECT' ? 'tag-green' : 'tag-blue'}">${pr}</span> `).join('') || '\u2014'}</td>
              <td>${p.match ? '<span class="tag tag-green">Match</span>' : '<span class="tag tag-yellow">Differs</span>'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
    ` : ''}

    <div class="section">
      <div class="section-title">Column Schema Diff</div>
      <table class="data-table">
        <thead><tr><th>Column</th><th>Status</th><th>Type (A)</th><th>Type (B)</th></tr></thead>
        <tbody>
          ${r.column_diff.map(c => `
            <tr class="diff-row-${c.status}">
              <td style="font-weight:600">${c.column}</td>
              <td>
                ${c.status === 'shared' ? (c.type_match ? '<span class="tag tag-green">Shared</span>' : '<span class="tag tag-yellow">Type Mismatch</span>') : ''}
                ${c.status === 'only_a' ? `<span class="tag tag-red">Only in A</span>` : ''}
                ${c.status === 'only_b' ? `<span class="tag tag-blue">Only in B</span>` : ''}
              </td>
              <td>${c.type_a || '\u2014'}</td>
              <td>${c.type_b || '\u2014'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>

    <div class="section">
      <div class="section-title">Sample Data</div>
      <div class="compare-grid">
        <div>
          <h4 style="font-size:13px;font-weight:600;margin-bottom:8px">${r.table_a.full_name}</h4>
          ${sampleA ? renderSampleTable(sampleA) : '<p style="color:var(--text-muted);font-size:13px">Could not load sample data</p>'}
        </div>
        <div>
          <h4 style="font-size:13px;font-weight:600;margin-bottom:8px">${r.table_b.full_name}</h4>
          ${sampleB ? renderSampleTable(sampleB) : '<p style="color:var(--text-muted);font-size:13px">Could not load sample data</p>'}
        </div>
      </div>
    </div>
  `;
}

function renderSampleTable(data) {
  if (!data || !data.columns) return '<p style="color:var(--text-muted)">No data</p>';
  return `
    <div class="sample-container">
      <table class="data-table">
        <thead><tr>${data.columns.map(c => `<th>${c.name}</th>`).join('')}</tr></thead>
        <tbody>
          ${(data.rows || []).slice(0, 8).map(row =>
            `<tr>${row.map(v => `<td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${v ?? '<null>'}</td>`).join('')}</tr>`
          ).join('')}
        </tbody>
      </table>
    </div>
  `;
}
