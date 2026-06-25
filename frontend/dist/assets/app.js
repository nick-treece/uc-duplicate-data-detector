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
  filters: {
    hideGovernanceViews: true,
    hidePipelineStages: true,
    hideSharedSource: false,
    catalogPrefix: '',
    catalogPrefixMode: 'any',
    minGroupSize: 2,
    crossCatalogOnly: false,
    showDismissed: false,
    searchQuery: '',
    minScore: 0,
    maxScore: 100,
    schemaPrefix: '',
    schemaPrefixMode: 'any',
    tableTypes: [],
    ownerFilter: '',
  },
  sortBy: 'score',
  compactView: false,
  groupsPageSize: 50,
  groupsShown: 50,
  cacheLoading: true,   // true during initial cache check on startup
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
    state.cacheLoading = true;
    renderDashboard();

    const status = await API.cacheStatus();
    if (!status.valid) {
      console.log('Cache not valid:', status.reason);
      return;
    }

    state.scanProgress = { message: 'Cache found \u2014 loading scan results\u2026' };
    renderDashboard();

    const cached = await API.loadFromCache();

    state.scanResult = cached.scan_result;
    state.cacheInfo = {
      cached_at: cached.cached_at,
      age_days: cached.cache_age_days,
    };

    state.scanProgress = { message: 'Loading schemas\u2026' };
    renderDashboard();
    state.schemas = await API.getSchemas();

    state.scanProgress = { message: 'Loading tables\u2026' };
    renderDashboard();
    state.tables = await API.getTables();

    state.scanProgress = { message: 'Loading duplicate groups\u2026' };
    renderDashboard();
    state.groups = cached.groups || [];
    state.scanned = true;
  } catch (e) {
    console.warn('Cache load failed (will require manual scan):', e);
  } finally {
    state.scanning = false;
    state.scanProgress = null;
    state.cacheLoading = false;
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

// ===== Dismissed Groups (localStorage) =====
function groupKey(g) {
  return g.tables.slice().sort().join(',');
}

function getDismissedKeys() {
  try { return new Set(JSON.parse(localStorage.getItem('uc-dismissed-groups') || '[]')); }
  catch { return new Set(); }
}

function saveDismissedKeys(keys) {
  localStorage.setItem('uc-dismissed-groups', JSON.stringify([...keys]));
}

function dismissGroup(g) {
  const keys = getDismissedKeys();
  keys.add(groupKey(g));
  saveDismissedKeys(keys);
}

function undismissGroup(g) {
  const keys = getDismissedKeys();
  keys.delete(groupKey(g));
  saveDismissedKeys(keys);
}

// ===== Group Filtering =====
function applyGroupFilters(groups) {
  console.log('[FILTER] input groups:', groups.length,
    '| threshold:', state.threshold,
    '| filters:', JSON.stringify(state.filters));

  if (groups.length && state.filters.catalogPrefix) {
    const prefix = state.filters.catalogPrefix.toLowerCase();
    const sample = groups[0];
    console.log('[FILTER] sample group tables:', sample.tables,
      '| first catalog:', sample.tables[0]?.split('.')[0],
      '| prefix test:', sample.tables[0]?.split('.')[0]?.toLowerCase().startsWith(prefix));
  }

  const result = groups.filter(g => {
    const tags = g.tags || [];

    if (state.filters.hideGovernanceViews && tags.includes('governance_view'))
      return false;

    if (state.filters.hidePipelineStages && tags.includes('pipeline_stage'))
      return false;

    if (state.filters.hideSharedSource && tags.includes('shared_source'))
      return false;

    const dismissed = getDismissedKeys();
    if (!state.filters.showDismissed && dismissed.has(groupKey(g)))
      return false;

    if (state.filters.minGroupSize > 2 && g.tables.length < state.filters.minGroupSize)
      return false;

    if (state.filters.crossCatalogOnly) {
      const catalogs = new Set(g.tables.map(t => t.split('.')[0]));
      if (catalogs.size < 2) return false;
    }

    if (state.filters.searchQuery) {
      const q = state.filters.searchQuery.toLowerCase();
      const matchesLabel = g.label.toLowerCase().includes(q);
      const matchesTable = g.tables.some(t => t.toLowerCase().includes(q));
      if (!matchesLabel && !matchesTable) return false;
    }

    const maxScore = g.pairs.length ? Math.max(...g.pairs.map(p => p.composite_score)) * 100 : 0;
    if (maxScore < state.filters.minScore || maxScore > state.filters.maxScore)
      return false;

    if (state.filters.schemaPrefix) {
      const prefix = state.filters.schemaPrefix.toLowerCase();
      const method = state.filters.schemaPrefixMode === 'all' ? 'every' : 'some';
      const hasMatch = g.tables[method](t => t.split('.')[1]?.toLowerCase().startsWith(prefix));
      if (!hasMatch) return false;
    }

    if (state.filters.tableTypes.length && g.table_types?.length) {
      const hasMatch = g.table_types.some(t => state.filters.tableTypes.includes(t));
      if (!hasMatch) return false;
    }

    if (state.filters.ownerFilter && g.owners?.length) {
      if (!g.owners.includes(state.filters.ownerFilter)) return false;
    }

    if (state.filters.catalogPrefix) {
      const prefix = state.filters.catalogPrefix.toLowerCase();
      const method = state.filters.catalogPrefixMode === 'all' ? 'every' : 'some';
      const hasMatch = g.tables[method](t => t.split('.')[0].toLowerCase().startsWith(prefix));
      if (!hasMatch) return false;
    }

    return true;
  });

  console.log('[FILTER] result:', result.length);
  return result;
}

function sortGroups(groups) {
  const sorted = [...groups];
  if (state.sortBy === 'size')     sorted.sort((a, b) => b.tables.length - a.tables.length);
  if (state.sortBy === 'catalogs') sorted.sort((a, b) => new Set(b.tables.map(t => t.split('.')[0])).size - new Set(a.tables.map(t => t.split('.')[0])).size);
  if (state.sortBy === 'score')    sorted.sort((a, b) => Math.max(...b.pairs.map(p => p.composite_score)) - Math.max(...a.pairs.map(p => p.composite_score)));
  return sorted;
}

function filteredGroupsInfo() {
  const all = state.groups;
  const filtered = sortGroups(applyGroupFilters(all));
  const hidden = all.length - filtered.length;
  return { filtered, total: all.length, hidden };
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
      <button class="btn btn-primary" id="scan-btn" ${state.scanning || state.cacheLoading ? 'disabled' : ''}>
        ${state.scanning ? '<div class="spinner" style="width:14px;height:14px;margin-right:6px"></div> Scanning\u2026' : state.cacheLoading ? '<div class="spinner" style="width:14px;height:14px;margin-right:6px"></div> Checking cache\u2026' : 'Scan All Catalogs'}
      </button>
    </div>
    ${state.cacheLoading ? `<div class="card" style="margin-bottom:16px;padding:16px;display:flex;align-items:center;gap:10px;border-left:3px solid var(--accent)">
      <div class="spinner" style="width:16px;height:16px;flex-shrink:0"></div>
      <div>
        <div style="font-size:13px;font-weight:600;color:var(--text)">Loading previous results\u2026</div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:2px">${state.scanProgress?.message || 'Checking for cached scan data'}</div>
      </div>
    </div>` : ''}
    <div id="scan-progress"></div>
    ${state.scanError ? `<div style="background:var(--red-bg, #2d1b1b);border:1px solid var(--red, #e74c3c);border-radius:6px;padding:16px;margin-bottom:16px">
      <div style="font-weight:700;font-size:14px;color:var(--red, #e74c3c);margin-bottom:8px">Scan failed</div>
      <pre style="font-size:12px;color:var(--text-muted);white-space:pre-wrap;word-break:break-word;margin:0">${state.scanError}</pre>
    </div>` : ''}
    ${sr ? renderScanSummary(sr) : (state.cacheLoading ? '' : '<div class="stat-card"><div class="stat-label">Status</div><div class="stat-value" style="font-size:16px;color:var(--text-muted)">Click \u201cScan All Catalogs\u201d to begin</div></div>')}
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
      <div class="stat-card"><div class="stat-label">Duplicate Groups</div><div class="stat-value accent">${filteredGroupsInfo().filtered.length}${filteredGroupsInfo().hidden ? ` <span style="font-size:12px;font-weight:400;color:var(--text-muted)">(${filteredGroupsInfo().hidden} hidden)</span>` : ''}</div></div>
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
  const { filtered, hidden } = filteredGroupsInfo();
  if (!filtered.length) {
    el.innerHTML = '<div class="card"><div class="empty-state"><h3>No duplicates detected</h3><p>All tables appear unique across all catalogs' + (hidden ? ` (${hidden} groups hidden by filters).` : '.') + '</p></div></div>';
    return;
  }
  el.innerHTML = `
    <div class="section-title" style="margin-top:24px">Top Duplicate Groups</div>
    ${filtered.slice(0, 5).map(g => renderDupGroupCard(g)).join('')}
    ${filtered.length > 5 ? `<p style="color:var(--text-muted);font-size:13px">+ ${filtered.length - 5} more groups. <a href="#/duplicates" style="color:var(--accent)">View all</a></p>` : ''}
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

function renderFilterSummary() {
  const f = state.filters;
  const defaults = {
    hideGovernanceViews: true, hidePipelineStages: true, hideSharedSource: false,
    catalogPrefix: '', minGroupSize: 2, crossCatalogOnly: false,
    showDismissed: false, searchQuery: '',
  };

  const chips = [];
  if (!f.hideGovernanceViews)  chips.push({ label: 'Showing governance views', key: 'hideGovernanceViews', value: true });
  if (!f.hidePipelineStages)   chips.push({ label: 'Showing pipeline stages',  key: 'hidePipelineStages',  value: true });
  if (f.hideSharedSource)      chips.push({ label: 'Hiding shared-source',      key: 'hideSharedSource',    value: false });
  if (f.crossCatalogOnly)      chips.push({ label: 'Cross-catalog only',        key: 'crossCatalogOnly',    value: false });
  if (f.showDismissed)         chips.push({ label: 'Showing dismissed',         key: 'showDismissed',       value: false });
  if (f.minGroupSize > 2)      chips.push({ label: `Min size: ${f.minGroupSize}`, key: 'minGroupSize',      value: 2 });
  if (f.searchQuery)           chips.push({ label: `Search: "${f.searchQuery}"`,  key: 'searchQuery',       value: '' });
  if (f.catalogPrefix)         chips.push({ label: `Prefix: ${f.catalogPrefix}`,  key: 'catalogPrefix',     value: '' });
  if (f.minScore > 0)              chips.push({ label: `Min score: ${f.minScore}%`,     key: 'minScore',     value: 0 });
  if (f.maxScore < 100)            chips.push({ label: `Max score: ${f.maxScore}%`,     key: 'maxScore',     value: 100 });
  if (f.schemaPrefix)              chips.push({ label: `Schema: ${f.schemaPrefix}`,      key: 'schemaPrefix', value: '' });
  if (f.tableTypes.length)         chips.push({ label: `Types: ${f.tableTypes.join(', ')}`, key: '_tableTypes', value: '' });
  if (f.ownerFilter)               chips.push({ label: `Owner: ${f.ownerFilter}`,        key: 'ownerFilter',  value: '' });
  if (state.sortBy !== 'score') chips.push({ label: `Sort: ${state.sortBy}`, key: '_sortBy', value: 'score' });

  if (!chips.length) return '';

  const chipHtml = chips.map(c => `
    <span class="tag tag-accent" style="cursor:pointer;font-size:11px" data-filter-key="${c.key}" data-filter-value="${c.value}" title="Click to clear">
      ${c.label} &times;
    </span>`).join('');

  return `
    <div id="filter-summary" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:12px;padding:8px 12px;background:var(--bg-card);border-radius:6px;border:1px solid var(--border)">
      <span style="font-size:11px;color:var(--text-muted);margin-right:4px">Active filters:</span>
      ${chipHtml}
      <button class="btn btn-outline btn-sm" id="clear-filters-btn" style="font-size:11px;padding:2px 8px;margin-left:4px">Clear all</button>
    </div>`;
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

  const { filtered, total, hidden } = filteredGroupsInfo();

  main().innerHTML = `
    <h2 class="page-title">Duplicate Detection</h2>
    <p class="page-desc">Tables across <strong>${(state.scanResult?.catalogs_scanned || []).length}</strong> catalog(s) grouped by similarity. The gold badge marks the recommended standard dataset.</p>
    <div class="threshold-control">
      <label>Similarity Threshold</label>
      <input type="range" id="threshold-slider" min="0.1" max="1.0" step="0.05" value="${state.threshold}" />
      <span class="threshold-value" id="threshold-val">${(state.threshold * 100).toFixed(0)}%</span>
      <button class="btn btn-outline btn-sm" id="redetect-btn">Re-detect</button>
    </div>
    <div class="card" style="margin-bottom:16px;padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <span style="font-weight:600;font-size:13px">Filters</span>
        <button class="btn btn-outline btn-sm" id="compact-toggle" title="${state.compactView ? 'Switch to card view' : 'Switch to compact view'}">
          ${state.compactView ? '&#9646;&#9646; Card view' : '&#8803; Compact view'}
        </button>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:16px;align-items:center">
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
          <input type="checkbox" id="filter-gov" ${state.filters.hideGovernanceViews ? 'checked' : ''} />
          Hide governance views
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
          <input type="checkbox" id="filter-shared-source" ${state.filters.hideSharedSource ? 'checked' : ''} />
          Hide shared-source groups
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
          <input type="checkbox" id="filter-pipeline" ${state.filters.hidePipelineStages ? 'checked' : ''} />
          Hide pipeline stages
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
          <input type="checkbox" id="filter-cross-catalog" ${state.filters.crossCatalogOnly ? 'checked' : ''} />
          Cross-catalog only
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
          <input type="checkbox" id="filter-show-dismissed" ${state.filters.showDismissed ? 'checked' : ''} />
          Show dismissed
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          Min group size
          <input type="number" id="filter-min-size" min="2" max="20" value="${state.filters.minGroupSize}" style="font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);width:60px" />
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          <input type="text" id="filter-search" value="${state.filters.searchQuery}" placeholder="Search table or group name…" style="font-size:13px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);width:220px" />
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          Sort by
          <select id="sort-by" style="font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text)">
            <option value="score"    ${state.sortBy === 'score'    ? 'selected' : ''}>Max similarity</option>
            <option value="size"     ${state.sortBy === 'size'     ? 'selected' : ''}>Group size</option>
            <option value="catalogs" ${state.sortBy === 'catalogs' ? 'selected' : ''}>Catalog count</option>
          </select>
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          Score
          <input type="number" id="filter-min-score" min="0" max="100" value="${state.filters.minScore}" style="font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);width:55px" />
          –
          <input type="number" id="filter-max-score" min="0" max="100" value="${state.filters.maxScore}" style="font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);width:55px" />
          %
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          Schema prefix
          <select id="filter-schema-mode" style="font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text)">
            <option value="any" ${state.filters.schemaPrefixMode === 'any' ? 'selected' : ''}>Any</option>
            <option value="all" ${state.filters.schemaPrefixMode === 'all' ? 'selected' : ''}>All</option>
          </select>
          <input type="text" id="filter-schema-prefix" value="${state.filters.schemaPrefix}" placeholder="e.g. analytics" style="font-size:13px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);width:140px" />
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          Table type
          ${['TABLE','VIEW','EXTERNAL'].map(t =>
            `<label style="display:flex;align-items:center;gap:3px;font-size:13px;cursor:pointer;font-weight:normal">
              <input type="checkbox" class="filter-type-cb" value="${t}" ${state.filters.tableTypes.includes(t) ? 'checked' : ''} />
              ${t[0] + t.slice(1).toLowerCase()}
            </label>`
          ).join('')}
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          Owner
          <select id="filter-owner" style="font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);max-width:200px">
            <option value="">Any</option>
            ${[...new Set(state.groups.flatMap(g => g.owners || []))].sort().map(o =>
              `<option value="${o}" ${state.filters.ownerFilter === o ? 'selected' : ''}>${o}</option>`
            ).join('')}
          </select>
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
          Catalog prefix
          <select id="filter-prefix-mode" style="font-size:13px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text)">
            <option value="any" ${state.filters.catalogPrefixMode === 'any' ? 'selected' : ''}>Any</option>
            <option value="all" ${state.filters.catalogPrefixMode === 'all' ? 'selected' : ''}>All</option>
          </select>
          catalog begins with
          <input type="text" id="filter-prefix" value="${state.filters.catalogPrefix}" placeholder="e.g. catalog_40_copper" style="font-size:13px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);width:180px" />
        </label>
      </div>
      ${hidden ? `<div style="margin-top:8px;font-size:12px;color:var(--text-muted)">Showing ${filtered.length} of ${total} groups (${hidden} filtered)</div>` : ''}
    </div>
    ${renderFilterSummary()}
    <div id="dup-groups">${filtered.length ? (state.compactView ? renderCompactView(filtered) : filtered.slice(0, state.groupsShown).map(g => renderDupGroupCard(g)).join('') + (filtered.length > state.groupsShown ? `<div style="text-align:center;padding:16px"><button class="btn btn-outline" id="show-more-btn">Show more (${state.groupsShown} of ${filtered.length})</button></div>` : '')) : '<div class="empty-state"><h3>No duplicates found</h3><p>Try adjusting the threshold or filters.</p></div>'}</div>
  `;

  // ── Filter event handlers ──
  function onFilterChange() {
    state.filters.hideGovernanceViews = $('filter-gov').checked;
    state.filters.hidePipelineStages = $('filter-pipeline').checked;
    state.filters.hideSharedSource = $('filter-shared-source').checked;
    state.filters.catalogPrefix = $('filter-prefix').value.trim();
    state.filters.catalogPrefixMode = $('filter-prefix-mode').value;
    state.filters.crossCatalogOnly = $('filter-cross-catalog').checked;
    state.filters.showDismissed = $('filter-show-dismissed').checked;
    state.filters.minGroupSize = parseInt($('filter-min-size').value) || 2;
    state.filters.searchQuery  = $('filter-search').value.trim();
    state.filters.minScore     = parseInt($('filter-min-score').value) || 0;
    state.filters.maxScore     = parseInt($('filter-max-score').value) || 100;
    state.filters.schemaPrefix = $('filter-schema-prefix').value.trim();
    state.filters.schemaPrefixMode = $('filter-schema-mode').value;
    state.filters.tableTypes   = [...document.querySelectorAll('.filter-type-cb:checked')].map(cb => cb.value);
    state.filters.ownerFilter  = $('filter-owner').value;
    state.sortBy = $('sort-by').value;
    state.groupsShown = state.groupsPageSize;  // reset pagination on filter change
    renderDuplicates();
  }

  $('filter-gov').onchange = onFilterChange;
  $('filter-pipeline').onchange = onFilterChange;
  $('filter-shared-source').onchange = onFilterChange;
  $('filter-prefix-mode').onchange = onFilterChange;
  $('filter-cross-catalog').onchange = onFilterChange;
  $('filter-show-dismissed').onchange = onFilterChange;

  // Filter summary chip clicks and clear-all
  document.getElementById('filter-summary')?.addEventListener('click', e => {
    const chip = e.target.closest('[data-filter-key]');
    if (chip) {
      const key = chip.dataset.filterKey;
      const val = chip.dataset.filterValue;
      if (key === '_sortBy')    { state.sortBy = val; }
      else if (key === '_tableTypes') { state.filters.tableTypes = []; }
      else {
        const parsed = val === 'true' ? true : val === 'false' ? false : (isNaN(val) ? val : Number(val));
        state.filters[key] = parsed;
      }
      state.groupsShown = state.groupsPageSize;
      renderDuplicates();
    }
  });

  const clearBtn = $('clear-filters-btn');
  if (clearBtn) clearBtn.onclick = () => {
    state.filters.hideGovernanceViews = true;
    state.filters.hidePipelineStages  = true;
    state.filters.hideSharedSource    = false;
    state.filters.catalogPrefix       = '';
    state.filters.minGroupSize        = 2;
    state.filters.crossCatalogOnly    = false;
    state.filters.showDismissed       = false;
    state.filters.searchQuery         = '';
    state.filters.minScore            = 0;
    state.filters.maxScore            = 100;
    state.filters.schemaPrefix        = '';
    state.filters.schemaPrefixMode    = 'any';
    state.filters.tableTypes          = [];
    state.filters.ownerFilter         = '';
    state.sortBy = 'score';
    state.groupsShown = state.groupsPageSize;
    renderDuplicates();
  };

  $('compact-toggle').onclick = () => {
    state.compactView = !state.compactView;
    renderDuplicates();
  };

  $('filter-schema-mode').onchange = onFilterChange;
  $('filter-owner').onchange = onFilterChange;
  document.querySelectorAll('.filter-type-cb').forEach(cb => cb.onchange = onFilterChange);

  let _scoreTimer = null;
  ['filter-min-score', 'filter-max-score'].forEach(id => {
    $(id).oninput = () => { clearTimeout(_scoreTimer); _scoreTimer = setTimeout(onFilterChange, 400); };
  });

  let _schemaPrefixTimer = null;
  $('filter-schema-prefix').oninput = () => {
    clearTimeout(_schemaPrefixTimer);
    _schemaPrefixTimer = setTimeout(onFilterChange, 400);
  };

  let _searchTimer = null;
  $('filter-search').oninput = () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(onFilterChange, 400);
  };

  $('sort-by').onchange = onFilterChange;

  let _minSizeTimer = null;
  $('filter-min-size').oninput = () => {
    clearTimeout(_minSizeTimer);
    _minSizeTimer = setTimeout(onFilterChange, 400);
  };

  // "Show more" button — appends next page without full re-render
  const showMoreBtn = $('show-more-btn');
  if (showMoreBtn) {
    showMoreBtn.onclick = () => {
      state.groupsShown += state.groupsPageSize;
      renderDuplicates();
    };
  }

  let _prefixTimer = null;
  $('filter-prefix').oninput = () => {
    clearTimeout(_prefixTimer);
    _prefixTimer = setTimeout(onFilterChange, 400);  // debounce
  };

  $('threshold-slider').oninput = (e) => {
    state.threshold = parseFloat(e.target.value);
    $('threshold-val').textContent = (state.threshold * 100).toFixed(0) + '%';
  };

  $('redetect-btn').onclick = async () => {
    $('redetect-btn').disabled = true;
    $('dup-groups').innerHTML = loading('Detecting duplicates\u2026');

    try {
      await API.detectDuplicates(state.threshold);

      // Poll until detection completes
      while (true) {
        await new Promise(r => setTimeout(r, 2000));
        const status = await API.detectStatus();

        if (status.state === 'completed') {
          state.groups = await API.getGroups();
          break;
        }
        if (status.state === 'failed') {
          $('dup-groups').innerHTML = `<div class="empty-state"><h3>Detection failed</h3><p>${status.message || status.error}</p></div>`;
          $('redetect-btn').disabled = false;
          return;
        }
        // Still running — keep polling
        $('dup-groups').innerHTML = loading(status.message || 'Detecting duplicates\u2026');
      }
    } catch (e) {
      $('dup-groups').innerHTML = `<div class="empty-state"><h3>Detection failed</h3><p>${e.message}</p></div>`;
      $('redetect-btn').disabled = false;
      return;
    }

    $('redetect-btn').disabled = false;
    renderDuplicates();
  };
}

function renderCompactView(groups) {
  const rows = groups.map(g => {
    const maxScore = g.pairs.length ? Math.max(...g.pairs.map(p => p.composite_score)) : 0;
    const catalogs = [...new Set(g.tables.map(t => t.split('.')[0]))];
    const tags = (g.tags || []).map(t => `<span class="tag tag-accent" style="font-size:10px">${t}</span>`).join(' ');
    const goldShort = g.gold_standard ? g.gold_standard.split('.').pop() : '\u2014';
    const firstPair = g.pairs[0];
    const compareBtn = firstPair
      ? `<button class="btn btn-outline btn-sm compare-btn" data-a="${firstPair.table_a}" data-b="${firstPair.table_b}">Compare</button>`
      : '';
    return `<tr>
      <td style="font-weight:600;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${g.label}">${g.label}</td>
      <td style="text-align:center">${g.tables.length}</td>
      <td><span class="similarity-score" style="color:${similarityColor(maxScore)}">${(maxScore * 100).toFixed(0)}%</span></td>
      <td style="text-align:center">${catalogs.length}</td>
      <td style="font-size:11px;color:var(--text-muted);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${g.gold_standard || ''}">${goldShort}</td>
      <td>${tags}</td>
      <td style="white-space:nowrap">${compareBtn}</td>
    </tr>`;
  }).join('');

  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Group</th>
          <th>Tables</th>
          <th>Score</th>
          <th>Catalogs</th>
          <th>Gold standard</th>
          <th>Tags</th>
          <th></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderDupGroupCard(g) {
  const maxScore = g.pairs.length ? Math.max(...g.pairs.map(p => p.composite_score)) : 0;
  const catalogSet = new Set(g.tables.map(t => t.split('.')[0]));
  const crossCatalog = catalogSet.size > 1;

  return `
    <div class="dup-group">
      <div class="dup-group-header">
        <span class="dup-group-title">${g.label} \u2014 ${g.tables.length} tables${crossCatalog ? ` across ${catalogSet.size} catalogs` : ''}</span>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="similarity-score" style="color:${similarityColor(maxScore)}">${(maxScore * 100).toFixed(0)}% max similarity</span>
          <button class="btn btn-outline btn-sm dismiss-btn" data-key="${groupKey(g)}" style="font-size:11px;padding:2px 8px;opacity:0.6" title="Dismiss this group">Dismiss</button>
        </div>
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
      <details style="margin-top:12px">
        <summary style="cursor:pointer;font-size:12px;color:var(--text-muted);user-select:none;padding:4px 0">
          Show ${g.pairs.length} pair${g.pairs.length !== 1 ? 's' : ''}
        </summary>
        <table class="data-table" style="margin-top:8px">
          <thead><tr><th>Table A</th><th>Table B</th><th>Columns</th><th>Types</th><th>Name</th><th>Lineage</th><th>Score</th><th></th></tr></thead>
          <tbody>
            ${g.pairs.slice(0, 6).map(p => {
              return `<tr>
                <td style="font-weight:500" title="${p.table_a}">${p.table_a}</td>
                <td style="font-weight:500" title="${p.table_b}">${p.table_b}</td>
                <td>${(p.column_similarity * 100).toFixed(0)}%</td>
                <td>${(p.type_similarity * 100).toFixed(0)}%</td>
                <td>${(p.name_similarity * 100).toFixed(0)}%</td>
                <td>${(p.lineage_similarity * 100).toFixed(0)}%</td>
                <td><span class="similarity-score" style="color:${similarityColor(p.composite_score)}">${(p.composite_score * 100).toFixed(0)}%</span></td>
                <td><button class="btn btn-outline btn-sm compare-btn" data-a="${p.table_a}" data-b="${p.table_b}">Compare</button></td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </details>
    </div>
  `;
}

document.addEventListener('click', (e) => {
  const dismissBtn = e.target.closest('.dismiss-btn');
  if (dismissBtn) {
    const key = dismissBtn.dataset.key;
    const keys = getDismissedKeys();
    keys.add(key);
    saveDismissedKeys(keys);
    renderDuplicates();
    return;
  }

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
  _lineageGraphLoaded = false;
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
    <p class="page-desc">Side-by-side schema diff and permissions comparison.</p>
    <div id="compare-form"></div>
    <div id="compare-result"></div>
  `;

  refreshPickers();

  if (c1 && s1 && t1 && c2 && s2 && t2) {
    doCompare();
  }
}

async function doCompare() {
  _lineageGraphLoaded = false;
  const graphContainer = document.getElementById('lineage-graph-container');
  if (graphContainer) graphContainer.style.display = 'none';

  const { cat: c1, sch: s1, tbl: t1 } = _pickA;
  const { cat: c2, sch: s2, tbl: t2 } = _pickB;
  if (!c1 || !s1 || !t1 || !c2 || !s2 || !t2) { alert('Select two tables'); return; }

  const el = $('compare-result');
  el.innerHTML = loading('Comparing tables\u2026');

  try {
    const result = await API.compareTables(c1, s1, t1, c2, s2, t2);
    state.compareResult = result;
    renderCompareResult(result);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><h3>Comparison failed</h3><p>${e.message}</p></div>`;
  }
}

function renderCompareResult(r) {
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

    ${(r.access_tree_a && r.access_tree_a.length) || (r.access_tree_b && r.access_tree_b.length) ? `
    <div class="section">
      <div class="section-title">Access Tree</div>

      ${r.shared_access && r.shared_access.shared_groups.length ? `
        <div style="margin-bottom:12px;padding:10px 14px;background:var(--accent-soft);border-left:3px solid var(--accent);border-radius:4px;font-size:13px">
          <strong>${r.shared_access.shared_groups.length} shared group${r.shared_access.shared_groups.length > 1 ? 's' : ''}:</strong>
          ${r.shared_access.shared_groups.map(g => `<span class="tag tag-accent" style="margin-left:4px">${g}</span>`).join('')}
          ${r.shared_access.shared_user_count ? ` <span style="color:var(--text-muted);margin-left:8px">(${r.shared_access.shared_user_count} shared users)</span>` : ''}
        </div>
      ` : ''}

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div>
          <div style="font-weight:600;margin-bottom:8px;font-size:13px">${r.table_a.full_name}</div>
          ${(r.access_tree_a || []).map(p => {
            const isShared = r.shared_access && r.shared_access.shared_groups.includes(p.principal);
            return p.type === 'group' ? `
            <div class="access-group" style="margin-bottom:6px">
              <div class="access-group-header" onclick="this.parentElement.classList.toggle('expanded')" style="cursor:pointer;display:flex;align-items:center;gap:6px;padding:6px 8px;background:${isShared ? 'var(--accent-soft)' : 'var(--bg-card)'};border-radius:6px;font-size:13px;${isShared ? 'border-left:3px solid var(--accent)' : ''}">
                <span class="access-chevron" style="transition:transform 0.2s;display:inline-block">&#9654;</span>
                <span style="font-weight:600">${p.principal}</span>
                ${isShared ? '<span class="tag tag-accent" style="font-size:10px">SHARED</span>' : ''}
                <span class="tag tag-accent" style="font-size:11px;margin-left:auto">${p.privileges.join(', ')}</span>
                <span style="color:var(--text-muted);font-size:11px">${p.members.length}</span>
              </div>
              <div class="access-group-members" style="display:none;padding:4px 0 4px 24px">
                ${p.members.map(m => `
                  <div style="font-size:12px;padding:3px 0;display:flex;gap:8px;align-items:center">
                    <span style="color:var(--text-muted)">&#8226;</span>
                    <span>${m.name}</span>
                    <span style="color:var(--text-muted);font-size:11px">${m.email}</span>
                  </div>
                `).join('')}
              </div>
            </div>
          ` : `
            <div style="margin-bottom:6px;padding:6px 8px;background:var(--bg-card);border-radius:6px;font-size:13px;display:flex;align-items:center;gap:6px">
              <span style="color:var(--text-muted)">&#9679;</span>
              <span>${p.principal}</span>
              <span class="tag" style="font-size:11px;margin-left:auto">${p.privileges.join(', ')}</span>
            </div>
          `}).join('')}
        </div>
        <div>
          <div style="font-weight:600;margin-bottom:8px;font-size:13px">${r.table_b.full_name}</div>
          ${(r.access_tree_b || []).map(p => {
            const isShared = r.shared_access && r.shared_access.shared_groups.includes(p.principal);
            return p.type === 'group' ? `
            <div class="access-group" style="margin-bottom:6px">
              <div class="access-group-header" onclick="this.parentElement.classList.toggle('expanded')" style="cursor:pointer;display:flex;align-items:center;gap:6px;padding:6px 8px;background:${isShared ? 'var(--accent-soft)' : 'var(--bg-card)'};border-radius:6px;font-size:13px;${isShared ? 'border-left:3px solid var(--accent)' : ''}">
                <span class="access-chevron" style="transition:transform 0.2s;display:inline-block">&#9654;</span>
                <span style="font-weight:600">${p.principal}</span>
                ${isShared ? '<span class="tag tag-accent" style="font-size:10px">SHARED</span>' : ''}
                <span class="tag tag-accent" style="font-size:11px;margin-left:auto">${p.privileges.join(', ')}</span>
                <span style="color:var(--text-muted);font-size:11px">${p.members.length}</span>
              </div>
              <div class="access-group-members" style="display:none;padding:4px 0 4px 24px">
                ${p.members.map(m => `
                  <div style="font-size:12px;padding:3px 0;display:flex;gap:8px;align-items:center">
                    <span style="color:var(--text-muted)">&#8226;</span>
                    <span>${m.name}</span>
                    <span style="color:var(--text-muted);font-size:11px">${m.email}</span>
                  </div>
                `).join('')}
              </div>
            </div>
          ` : `
            <div style="margin-bottom:6px;padding:6px 8px;background:var(--bg-card);border-radius:6px;font-size:13px;display:flex;align-items:center;gap:6px">
              <span style="color:var(--text-muted)">&#9679;</span>
              <span>${p.principal}</span>
              <span class="tag" style="font-size:11px;margin-left:auto">${p.privileges.join(', ')}</span>
            </div>
          `}).join('')}
        </div>
      </div>
    </div>
    ` : ''}

${r.lineage && r.lineage.has_lineage ? `
    <div class="section">
      <div class="section-title">Lineage Comparison</div>

      <!-- Consumer counts -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
        <div style="background:var(--bg-card);padding:12px;border-radius:8px;text-align:center">
          <div style="font-size:12px;color:var(--text-muted)">Consumers of A</div>
          <div style="font-size:20px;font-weight:600">${r.lineage.consumer_counts.a}</div>
        </div>
        <div style="background:var(--bg-card);padding:12px;border-radius:8px;text-align:center">
          <div style="font-size:12px;color:var(--text-muted)">Consumers of B</div>
          <div style="font-size:20px;font-weight:600">${r.lineage.consumer_counts.b}</div>
        </div>
      </div>

      <!-- Direct flow banner -->
      ${r.lineage.direct_flow ? `
        <div style="margin-bottom:16px;padding:10px 14px;background:var(--accent-soft);border-left:3px solid var(--accent);border-radius:4px">
          <strong>Direct data flow:</strong> ${r.lineage.direct_flow.source} \u2192 ${r.lineage.direct_flow.target}
          ${r.lineage.direct_flow.entity_types.length ? `<br><span style="color:var(--text-muted);font-size:13px">Via: ${r.lineage.direct_flow.entity_types.join(", ")}</span>` : ""}
        </div>
      ` : ""}

      <!-- Shared upstream -->
      ${r.lineage.shared_upstream.length ? `
        <div style="margin-bottom:16px">
          <strong>Shared upstream sources (${r.lineage.shared_upstream.length}):</strong>
          <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">
            ${r.lineage.shared_upstream.map(t => `<span class="tag tag-accent">${t}</span>`).join("")}
          </div>
        </div>
      ` : ""}

      <!-- Side-by-side upstream/downstream -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
        <div>
          <div style="font-weight:600;margin-bottom:8px;font-size:13px">Upstream of A (${r.lineage.upstream_a.length})</div>
          ${r.lineage.upstream_a.length ? `
            <div style="max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:3px">
              ${r.lineage.upstream_a.map(t => `<span class="tag" style="font-size:11px;display:block;word-break:break-all;${r.lineage.shared_upstream.includes(t) ? 'background:var(--accent-soft) !important;border-color:var(--accent)' : ''}">${t}</span>`).join("")}
            </div>
          ` : `<span style="color:var(--text-muted);font-size:13px">None found</span>`}
        </div>
        <div>
          <div style="font-weight:600;margin-bottom:8px;font-size:13px">Upstream of B (${r.lineage.upstream_b.length})</div>
          ${r.lineage.upstream_b.length ? `
            <div style="max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:3px">
              ${r.lineage.upstream_b.map(t => `<span class="tag" style="font-size:11px;display:block;word-break:break-all;${r.lineage.shared_upstream.includes(t) ? 'background:var(--accent-soft) !important;border-color:var(--accent)' : ''}">${t}</span>`).join("")}
            </div>
          ` : `<span style="color:var(--text-muted);font-size:13px">None found</span>`}
        </div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
        <div>
          <div style="font-weight:600;margin-bottom:8px;font-size:13px">Downstream of A (${r.lineage.downstream_a.length})</div>
          ${r.lineage.downstream_a.length ? `
            <div style="max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:3px">
              ${r.lineage.downstream_a.map(t => `<span class="tag" style="font-size:11px;display:block;word-break:break-all;${r.lineage.shared_downstream && r.lineage.shared_downstream.includes(t) ? 'background:var(--accent-soft) !important;border-color:var(--accent)' : ''}">${t}</span>`).join("")}
            </div>
          ` : `<span style="color:var(--text-muted);font-size:13px">None found</span>`}
        </div>
        <div>
          <div style="font-weight:600;margin-bottom:8px;font-size:13px">Downstream of B (${r.lineage.downstream_b.length})</div>
          ${r.lineage.downstream_b.length ? `
            <div style="max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:3px">
              ${r.lineage.downstream_b.map(t => `<span class="tag" style="font-size:11px;display:block;word-break:break-all;${r.lineage.shared_downstream && r.lineage.shared_downstream.includes(t) ? 'background:var(--accent-soft) !important;border-color:var(--accent)' : ''}">${t}</span>`).join("")}
            </div>
          ` : `<span style="color:var(--text-muted);font-size:13px">None found</span>`}
        </div>
      </div>


      <!-- Shared ancestors (deep/transitive) -->
      ${r.lineage.shared_ancestors && r.lineage.shared_ancestors.length ? `
        <div style="margin-bottom:16px">
          <strong>Shared ancestors (${r.lineage.shared_ancestors.length} common sources across pipeline):</strong>
          <div style="margin-top:6px;max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:4px">
            ${r.lineage.shared_ancestors.map(a => `
              <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--accent-soft);border-left:3px solid var(--accent);border-radius:4px;font-size:12px">
                <span style="flex:1;word-break:break-all;font-weight:500">${a.name}</span>
                <span class="tag tag-accent" style="flex-shrink:0">depth A: ${a.depth_a}</span>
                <span class="tag tag-accent" style="flex-shrink:0">depth B: ${a.depth_b}</span>
              </div>
            `).join("")}
          </div>
        </div>
      ` : ""}


      <!-- Lineage Graph (Enhancement 3) -->
      <div style="margin-top:16px">
        <div style="cursor:pointer;user-select:none;font-weight:600;font-size:13px;padding:8px 0" onclick="toggleLineageGraph(this)">
          &#9654; Lineage Graph <span style="font-size:12px;color:var(--text-muted)">(click to expand)</span>
        </div>
        <div id="lineage-graph-container" style="display:none">
          <div id="lineage-graph-loading" style="text-align:center;padding:20px;color:var(--text-muted)">Loading graph...</div>
          <div id="lineage-graph-legend" style="display:none;padding:8px 12px;margin-bottom:8px;font-size:11px;color:var(--text-muted);border:1px solid var(--border);border-radius:6px;background:var(--bg-secondary)">
            <span style="margin-right:16px"><span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:#1e40af;border:1.5px solid #3b82f6;vertical-align:middle;margin-right:4px"></span> Compared tables</span>
            <span style="margin-right:16px"><span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:#7c3aed;border:1.5px solid #a78bfa;vertical-align:middle;margin-right:4px"></span> Shared ancestor</span>
            <span style="margin-right:16px"><span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:#1e293b;border:1.5px solid #475569;vertical-align:middle;margin-right:4px"></span> Intermediate</span>
            <span style="margin-left:12px;border-left:1px solid var(--border);padding-left:12px">Tier dots:</span>
            <span style="margin-left:8px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#f59e0b;vertical-align:middle;margin-right:3px"></span>gold</span>
            <span style="margin-left:8px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#94a3b8;vertical-align:middle;margin-right:3px"></span>silver</span>
            <span style="margin-left:8px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#d97706;vertical-align:middle;margin-right:3px"></span>bronze</span>
            <span style="margin-left:8px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#b45309;vertical-align:middle;margin-right:3px"></span>copper</span>
          </div>
          <div id="lineage-graph-svg" style="width:100%;height:500px;overflow:hidden;border:1px solid var(--border);border-radius:8px;background:var(--bg-secondary)"></div>
        </div>
      </div>

      <!-- Column-level lineage -->
      ${r.lineage.column_mappings.length ? `
        <div>
          <strong>Column-level lineage (${r.lineage.column_mappings.length} mappings):</strong>
          <table class="data-table" style="margin-top:8px;font-size:12px">
            <thead><tr>
              <th>Source Table</th><th>Source Column</th>
              <th>Target Table</th><th>Target Column</th>
            </tr></thead>
            <tbody>
              ${r.lineage.column_mappings.map(m => `
                <tr>
                  <td style="font-size:11px;color:var(--text-muted)">${m.source_table}</td>
                  <td style="font-weight:600">${m.source_col}</td>
                  <td style="font-size:11px;color:var(--text-muted)">${m.target_table}</td>
                  <td style="font-weight:600">${m.target_col}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      ` : ""}
    </div>
  ` : ""}

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


// ── Enhancement 3: Lineage Graph (D3 + dagre) ────────────────────────────

let _lineageGraphLoaded = false;

function toggleLineageGraph(el) {
  const container = document.getElementById('lineage-graph-container');
  if (!container) return;

  const isHidden = container.style.display === 'none';
  container.style.display = isHidden ? 'block' : 'none';
  // Update the arrow character in the toggle text
  el.innerHTML = el.innerHTML.replace(isHidden ? '\u25b6' : '\u25bc', isHidden ? '\u25bc' : '\u25b6');

  if (isHidden && !_lineageGraphLoaded) {
    _lineageGraphLoaded = true;
    loadLineageGraph();
  }
}

async function loadLineageGraph() {
  const cat1 = _pickA.cat, s1 = _pickA.sch, t1 = _pickA.tbl;
  const cat2 = _pickB.cat, s2 = _pickB.sch, t2 = _pickB.tbl;

  const loading = document.getElementById('lineage-graph-loading');
  const svgContainer = document.getElementById('lineage-graph-svg');

  if (!cat1 || !s1 || !t1 || !cat2 || !s2 || !t2) {
    if (loading) loading.innerHTML = '<span style="color:var(--red)">Cannot load graph: no tables selected</span>';
    return;
  }

  // Check CDN dependencies loaded
  if (typeof d3 === 'undefined' || typeof dagre === 'undefined') {
    if (loading) loading.innerHTML = '<span style="color:var(--red)">Graph libraries (D3/dagre) failed to load. Check browser console.</span>';
    return;
  }

  try {
    const resp = await fetch(`/api/compare/lineage-graph/${cat1}/${s1}/${t1}/${cat2}/${s2}/${t2}`);
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || errData.error || `HTTP ${resp.status}`);
    }
    const data = await resp.json();

    if (loading) loading.style.display = 'none';

    if (!data.nodes || data.nodes.length === 0) {
      svgContainer.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)">No connecting lineage path found between these tables.</div>';
      return;
    }

    renderDagreGraph(svgContainer, data);
    const legendEl = document.getElementById('lineage-graph-legend');
    if (legendEl) legendEl.style.display = 'block';
  } catch (err) {
    if (loading) loading.style.display = 'none';
    svgContainer.innerHTML = `<div style="text-align:center;padding:40px;color:var(--red)">Failed to load graph: ${err.message}</div>`;
  }
}

function renderDagreGraph(container, data) {
  const width = container.clientWidth || 900;
  const height = 500;

  // Tier colours
  const tierColors = {
    gold: '#f59e0b',
    silver: '#94a3b8',
    bronze: '#d97706',
    copper: '#b45309',
    unknown: '#6366f1'
  };

  // Create dagre graph
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 40, ranksep: 80, marginx: 30, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));

  // Add nodes
  data.nodes.forEach(n => {
    const labelLines = n.label.split('.');
    const displayLabel = labelLines.length > 1 ? labelLines[labelLines.length - 1] : n.label;
    g.setNode(n.id, { label: displayLabel, width: Math.min(displayLabel.length * 7 + 20, 200), height: 36 });
  });

  // Add edges
  data.edges.forEach(e => {
    g.setEdge(e.source, e.target);
  });

  // Compute layout
  dagre.layout(g);

  // Get graph dimensions
  const graphWidth = g.graph().width || width;
  const graphHeight = g.graph().height || height;

  // Create SVG
  container.innerHTML = '';
  const svg = d3.select(container).append('svg')
    .attr('width', '100%')
    .attr('height', height)
    .attr('viewBox', `0 0 ${graphWidth + 60} ${graphHeight + 40}`)
    .attr('preserveAspectRatio', 'xMidYMid meet');

  const root = svg.append('g').attr('transform', 'translate(30, 20)');

  // Zoom/pan
  const zoom = d3.zoom()
    .scaleExtent([0.3, 8])
    .on('zoom', (event) => root.attr('transform', event.transform));
  svg.call(zoom);

  // Draw edges (arrows)
  root.append('defs').append('marker')
    .attr('id', 'arrowhead')
    .attr('viewBox', '0 0 10 10')
    .attr('refX', 8).attr('refY', 5)
    .attr('markerWidth', 6).attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path').attr('d', 'M0,0 L10,5 L0,10 Z')
    .attr('fill', '#64748b');

  g.edges().forEach(e => {
    const edge = g.edge(e);
    const line = d3.line().x(p => p.x).y(p => p.y).curve(d3.curveBasis);
    root.append('path')
      .attr('d', line(edge.points))
      .attr('fill', 'none')
      .attr('stroke', '#64748b')
      .attr('stroke-width', 1.5)
      .attr('marker-end', 'url(#arrowhead)');
  });

  // Node lookup for data
  const nodeMap = {};
  data.nodes.forEach(n => { nodeMap[n.id] = n; });

  // Draw nodes
  g.nodes().forEach(nodeId => {
    const node = g.node(nodeId);
    const nodeData = nodeMap[nodeId];
    if (!node || !nodeData) return;

    const group = root.append('g')
      .attr('transform', `translate(${node.x - node.width/2}, ${node.y - node.height/2})`)
      .style('cursor', 'pointer');

    // Background rect
    const fillColor = nodeData.is_target ? '#1e40af' : nodeData.is_shared_ancestor ? '#7c3aed' : '#1e293b';
    const strokeColor = nodeData.is_target ? '#3b82f6' : nodeData.is_shared_ancestor ? '#a78bfa' : tierColors[nodeData.tier] || '#475569';

    group.append('rect')
      .attr('width', node.width)
      .attr('height', node.height)
      .attr('rx', 6)
      .attr('fill', fillColor)
      .attr('stroke', strokeColor)
      .attr('stroke-width', nodeData.is_target ? 2.5 : 1.5);

    // Tier indicator dot
    group.append('circle')
      .attr('cx', 10).attr('cy', node.height / 2)
      .attr('r', 4)
      .attr('fill', tierColors[nodeData.tier] || '#475569');

    // Label text
    const displayLabel = node.label.length > 25 ? node.label.substring(0, 23) + '...' : node.label;
    group.append('text')
      .attr('x', 20).attr('y', node.height / 2 + 4)
      .attr('fill', '#e2e8f0')
      .attr('font-size', '11px')
      .attr('font-family', 'monospace')
      .text(displayLabel);

    // Tooltip on hover
    group.append('title')
      .text(`${nodeData.id}\nTier: ${nodeData.tier}\nConsumers: ${nodeData.consumers}${nodeData.depth_from_a != null ? '\nDepth from A: ' + nodeData.depth_from_a : ''}${nodeData.depth_from_b != null ? '\nDepth from B: ' + nodeData.depth_from_b : ''}`);
  });

}
