const API = {
  async get(path) {
    const resp = await fetch(`/api${path}`);
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp.json();
  },

  async post(path) {
    const resp = await fetch(`/api${path}`, { method: 'POST' });
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp.json();
  },

  _qs(params) {
    const p = Object.entries(params).filter(([, v]) => v != null && v !== '');
    return p.length ? '?' + p.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&') : '';
  },

  listCatalogs() { return this.get('/catalog/list'); },

  /** Start a background scan. Returns immediately with status. */
  startScan() { return this.post('/catalog/scan-all'); },

  /** Poll the current scan progress. */
  scanStatus() { return this.get('/catalog/scan-status'); },

  /** Check whether a valid cache exists. */
  cacheStatus() { return this.get('/catalog/cache-status'); },

  /** Load scan results from cache (bulk metadata + cached groups). */
  loadFromCache() { return this.post('/catalog/cache-load'); },

  getSchemas(catalog) {
    return this.get('/catalog/schemas' + this._qs({ catalog }));
  },

  getTables(schema, catalog) {
    return this.get('/catalog/tables' + this._qs({ schema, catalog }));
  },

  getTable(catalog, schema, table) {
    return this.get(`/catalog/table/${catalog}/${schema}/${table}`);
  },

  detectDuplicates(threshold = 0.5) {
    return this.get('/duplicates/detect' + this._qs({ threshold }));
  },

  getGroups() { return this.get('/duplicates/groups'); },

  compareTables(cat1, s1, t1, cat2, s2, t2) {
    return this.get(`/compare/${cat1}/${s1}/${t1}/${cat2}/${s2}/${t2}`);
  },

  getSample(catalog, schema, table) {
    return this.get(`/compare/sample/${catalog}/${schema}/${table}`);
  },
};
