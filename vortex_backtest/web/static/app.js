(function () {
  'use strict';
  // 数据来源默认"自动"：优先真实 API，拿不到(无后端/报错)则回退 mock，便于壳独立运行。
  // 强制时把 LIVE 设为 true(只用真实)或 'mock'(只用假数据)。
  var LIVE = false;

  var app = document.getElementById('app');
  var toolbar = document.getElementById('toolbar');
  var crumbs = document.getElementById('crumbs');
  var srLive = document.getElementById('sr');
  var root = document.documentElement;
  var state = { benchmark: '000300.SH', page: {}, reasonFilter: '', jobId: null, lbMetric: 'total_return', lbScope: 'best' };
  var PAGE = 25;
  var ACCOUNT = 'demo';  // 看板账户上下文(种子账户;后续可加账户选择器)
  var charts = [];

  function applyTheme(mode) {
    mode = mode || localStorage.getItem('vbt-theme') || 'system';
    var dark = mode === 'dark' || (mode === 'system' && matchMedia('(prefers-color-scheme:dark)').matches);
    root.setAttribute('data-theme', dark ? 'dark' : 'light');
    localStorage.setItem('vbt-theme', mode);
  }
  applyTheme();

  function pct(x) { return x == null ? '—' : (x >= 0 ? '+' : '') + (x * 100).toFixed(2) + '%'; }
  function money(x) { return x == null ? '—' : '¥' + Math.round(x).toLocaleString(); }
  function fix(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 2 : d); }
  function cls(x) { return x == null ? '' : (x >= 0 ? 'profit' : 'loss'); }
  function arrow(x) { return x == null ? '' : (x >= 0 ? '▲ ' : '▼ '); }
  function r2(x) { return Math.round(x * 100) / 100; }
  function relTime(at) {
    if (!at) return '';
    var d = Date.now() - Date.parse(at);
    if (isNaN(d)) return String(at).slice(0, 16).replace('T', ' ');
    var m = Math.floor(d / 60000);
    if (m < 1) return '刚刚';
    if (m < 60) return m + ' 分钟前';
    var h = Math.floor(m / 60);
    if (h < 24) return h + ' 小时前';
    return Math.floor(h / 24) + ' 天前';
  }
  function slice(arr, tab) { var p = state.page[tab] || 0; return (arr || []).slice(p * PAGE, p * PAGE + PAGE); }
  function pager(tab, total) {
    var pages = Math.max(1, Math.ceil(total / PAGE)), p = Math.min(state.page[tab] || 0, pages - 1);
    if (total <= PAGE) return '';
    var from = total ? p * PAGE + 1 : 0, to = Math.min(total, (p + 1) * PAGE);
    return '<div class="pager" style="display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:10px;font-size:13px">' +
      '<button class="pgbtn" data-tab="' + tab + '" data-d="-1"' + (p <= 0 ? ' disabled' : '') + '>‹ 上一页</button>' +
      '<span class="muted">' + from + '–' + to + ' / ' + total + '</span>' +
      '<button class="pgbtn" data-tab="' + tab + '" data-d="1"' + (p >= pages - 1 ? ' disabled' : '') + '>下一页 ›</button></div>';
  }
  function esc(s) { return String(s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); }
  function cssVar(n) { return getComputedStyle(root).getPropertyValue(n).trim(); }

  function genSeries(n, drift, vol, seed) {
    var v = 1, out = [1], s = seed;
    for (var i = 1; i < n; i++) { s = (s * 9301 + 49297) % 233280; out.push(Math.round(v * (1 + drift + vol * (s / 233280 - 0.5)) * 1e4) / 1e4); v = out[i]; }
    return out;
  }
  function genDates(n) { var o = []; for (var i = 0; i < n; i++) { var d = new Date(2026, 0, 5 + i); o.push(d.toISOString().slice(0, 10)); } return o; }
  function drawdown(eq) { var pk = -1e9, o = []; eq.forEach(function (v) { pk = Math.max(pk, v); o.push(v / pk - 1); }); return o; }
  var N = 60, DATES = genDates(N), EQ = genSeries(N, 0.0016, 0.012, 7), BM = genSeries(N, 0.0009, 0.011, 13), DD = drawdown(EQ);
  var MOCK = {
    benchmarks: { available: true, default: '000300.SH', items: [
      { symbol: '000300.SH', name: '沪深300' }, { symbol: '000905.SH', name: '中证500' },
      { symbol: '000016.SH', name: '上证50' }, { symbol: '399006.SZ', name: '创业板指' }] },
    jobs: [
      { job_id: '57a2e1', account_id: 'demo', status: 'completed', start_date: '2026-01-05', end_date: '2026-03-31', frequency: '1min', strategy_ids: ['main-replay'], summary: { total_return: EQ[N - 1] - 1, max_drawdown: Math.min.apply(null, DD), trades: 14, rejections: 11 } },
      { job_id: '3bd7aa', account_id: 'demo', status: 'running', start_date: '2026-01-05', end_date: '2026-03-31', frequency: '1min', strategy_ids: ['intraday-x'], progress: { trading_day: '2026-02-10', pct: 0.62 } },
      { job_id: 'dc0290', account_id: 'star', status: 'failed', start_date: '2026-01-05', end_date: '2026-01-10', frequency: '1min', strategy_ids: ['star-replay'], summary: { error: 'minute_data_missing' } }],
    equity: { dates: DATES, equity: EQ, drawdown: DD, baseline: 1, rebase: true, benchmark: { symbol: '000300.SH', available: true, values: BM } },
    metrics: { sample_days: N, low_confidence: false,
      absolute: { cumulative_return: EQ[N - 1] - 1, annual_return: 0.221, annual_volatility: 0.183, max_drawdown: Math.min.apply(null, DD) },
      risk_adjusted: { sharpe: 1.24, sortino: 1.68, calmar: 3.56, var_95: -0.021, omega: 1.4 },
      benchmark_relative: { excess_return: 0.051, annual_excess: 0.06, alpha: 0.062, beta: 0.86, information_ratio: 0.91, tracking_error: 0.07, up_capture: 1.05, down_capture: 0.82 },
      benchmark: { symbol: '000300.SH', available: true } },
    rejsum: { counts: { t_plus_1_not_sellable: 5, limit_up_buy_blocked: 3, insufficient_cash: 2, invalid_lot_size: 1 }, total: 11 },
    summary: {
      trades: [
        { trade_date: '2026-01-06', symbol: '600000.SH', side_name: 'BUY', quantity: 1000, price: 9.32, amount: 9320, commission: 5, stamp_tax: 0, cash_after: 90675 },
        { trade_date: '2026-01-06', symbol: '000001.SZ', side_name: 'BUY', quantity: 1000, price: 10.99, amount: 10990, commission: 5, stamp_tax: 0, cash_after: 79680 },
        { trade_date: '2026-02-10', symbol: '000001.SZ', side_name: 'SELL', quantity: 500, price: 11.20, amount: 5600, commission: 5, stamp_tax: 6, cash_after: 85269 }
      ],
      rejections: [
        { trade_date: '2026-01-06', symbol: '000001.SZ', side_name: 'SELL', quantity: 500, reason: 't_plus_1_not_sellable' },
        { trade_date: '2026-01-20', symbol: '600000.SH', side_name: 'BUY', quantity: 1000, reason: 'limit_up_buy_blocked' },
        { trade_date: '2026-02-03', symbol: '000001.SZ', side_name: 'BUY', quantity: 2000, reason: 'insufficient_cash' }
      ],
      positions: [
        { symbol: '600000.SH', quantity: 1000, available_quantity: 1000, cost_basis: 9.33, last_price: 9.61, market_value: 9610, unrealized_pnl: 280, unrealized_pnl_ratio: 0.03 },
        { symbol: '000001.SZ', quantity: 500, available_quantity: 500, cost_basis: 10.99, last_price: 11.35, market_value: 5675, unrealized_pnl: 180, unrealized_pnl_ratio: 0.0328 }
      ],
      strategies: [
        { strategy_id: 'main-replay', total_return: 0.084, max_drawdown: -0.062, total_value: 94965, trades: [1, 2], rejections: [1],
          daily: [{ trade_date: '2026-01-05', total_value: 100000 }, { trade_date: '2026-01-12', total_value: 101200 }, { trade_date: '2026-01-19', total_value: 99800 }, { trade_date: '2026-01-26', total_value: 103400 }, { trade_date: '2026-02-02', total_value: 105100 }, { trade_date: '2026-02-09', total_value: 104200 }, { trade_date: '2026-02-16', total_value: 106800 }, { trade_date: '2026-02-23', total_value: 108400 }] },
        { strategy_id: 'star-replay', total_return: 0.041, max_drawdown: -0.038, total_value: 10100, trades: [], rejections: [],
          daily: [{ trade_date: '2026-01-05', total_value: 10000 }, { trade_date: '2026-01-12', total_value: 10150 }, { trade_date: '2026-01-19', total_value: 9950 }, { trade_date: '2026-01-26', total_value: 10250 }, { trade_date: '2026-02-02', total_value: 10180 }, { trade_date: '2026-02-09', total_value: 10120 }, { trade_date: '2026-02-16', total_value: 10300 }, { trade_date: '2026-02-23', total_value: 10410 }] }
      ]
    }
  };

  function j(url) { return fetch(url).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }); }
  function jp(url) { return fetch(url, { method: 'POST' }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }); }
  function jh(url) {  // GET + 读 X-Total-Count → {items,total}（服务端分页）
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      var total = parseInt(r.headers.get('X-Total-Count') || '', 10);
      return r.json().then(function (b) { return { items: b, total: isNaN(total) ? b.length : total }; });
    });
  }
  // 数据来源：'live' 强制真实 API；'mock' 强制假数据；null=自动(优先真实，失败回退 mock，
  // 便于壳脱离后端独立运行)。LIVE 仅作向后兼容别名。
  var FORCE = LIVE === true ? 'live' : (LIVE === 'mock' ? 'mock' : null);
  function pick(path, mockVal) {
    if (FORCE === 'mock') return Promise.resolve(mockVal);
    return j(path).catch(function (e) { if (FORCE === 'live') throw e; return mockVal; });
  }
  var api = {
    benchmarks: function () { return pick('/benchmarks', MOCK.benchmarks); },
    list: function (st) { return pick('/backtests' + (st ? '?status=' + st : ''), MOCK.jobs); },
    equity: function (id, bm) { return pick('/backtests/' + id + '/equity?rebase=1' + (bm ? '&benchmark=' + bm : ''), MOCK.equity); },
    metrics: function (id, bm) { return pick('/backtests/' + id + '/metrics' + (bm ? '?benchmark=' + bm : ''), MOCK.metrics); },
    rejsum: function (id) { return pick('/backtests/' + id + '/rejections/summary', MOCK.rejsum); },
    summary: function (id) { return pick('/backtests/' + id + '/summary', MOCK.summary); },
    cancel: function (id) { return FORCE === 'mock' ? Promise.resolve(null) : jp('/backtests/' + id + '/cancel'); }
  };
  api.tradesPage = function (id, q) {
    if (FORCE === 'mock') { var all = MOCK.summary.trades; return Promise.resolve({ items: all.slice(q.offset, q.offset + q.limit), total: all.length }); }
    return jh('/backtests/' + id + '/trades?limit=' + q.limit + '&offset=' + q.offset);
  };
  api.rejectionsPage = function (id, q) {
    if (FORCE === 'mock') {
      var all = q.reason ? MOCK.summary.rejections.filter(function (r) { return r.reason === q.reason; }) : MOCK.summary.rejections;
      return Promise.resolve({ items: all.slice(q.offset, q.offset + q.limit), total: all.length });
    }
    return jh('/backtests/' + id + '/rejections?limit=' + q.limit + '&offset=' + q.offset + (q.reason ? '&reason=' + encodeURIComponent(q.reason) : ''));
  };
  function jput(url, body) {
    return fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  // ---- 策略中心 mock(壳脱机时用) ----
  MOCK.strategies = [
    { strategy_id: 'momentum-v3', n_runs: 14, running: true, last_run_at: '2026-06-06T09:00:00', board: 'main', favorite: true, pinned: true, tags: ['核心'], symbols: ['600000.SH', '000001.SZ'],
      latest: { job_id: 'j14', status: 'running', total_return: 0.032, max_drawdown: -0.018, sharpe: 1.7, created_at: '2026-06-06T09:00:00', progress: { pct: 0.62, trading_day: '2026-02-10' } },
      best: { job_id: 'j9', status: 'completed', total_return: 0.246, sharpe: 1.92, calmar: 3.9, max_drawdown: -0.061, created_at: '2026-05-20T00:00:00' } },
    { strategy_id: 'mean-rev-a', n_runs: 9, running: false, last_run_at: '2026-06-06T07:00:00', board: '混合', favorite: false, pinned: false, tags: [], symbols: ['000001.SZ'],
      latest: { job_id: 'j9b', status: 'completed', total_return: 0.124, max_drawdown: -0.04, sharpe: 1.5, created_at: '2026-06-06T07:00:00' },
      best: { job_id: 'j7b', status: 'completed', total_return: 0.181, sharpe: 1.64, calmar: 3.1, max_drawdown: -0.058, created_at: '2026-05-28T00:00:00' } },
    { strategy_id: 'star-growth', n_runs: 7, running: true, last_run_at: '2026-06-05T13:00:00', board: 'star', favorite: false, pinned: false, tags: ['科创'], symbols: ['688169.SH'],
      latest: { job_id: 'j7s', status: 'running', total_return: 0.009, max_drawdown: -0.012, sharpe: 1.0, created_at: '2026-06-05T13:00:00', progress: { pct: 0.28, trading_day: '2026-01-19' } },
      best: { job_id: 'j5s', status: 'completed', total_return: 0.153, sharpe: 1.38, calmar: 2.4, max_drawdown: -0.064, created_at: '2026-05-22T00:00:00' } },
    { strategy_id: 'vol-target-b', n_runs: 5, running: false, last_run_at: '2026-06-03T00:00:00', board: 'main', favorite: false, pinned: false, tags: [], symbols: ['600000.SH'],
      latest: { job_id: 'j5v', status: 'completed', total_return: -0.021, max_drawdown: -0.052, sharpe: 0.71, created_at: '2026-06-03T00:00:00' },
      best: { job_id: 'j2v', status: 'completed', total_return: 0.064, sharpe: 1.1, calmar: 1.3, max_drawdown: -0.049, created_at: '2026-05-30T00:00:00' } }
  ];
  function _scopeVal(s, scope, metric) { var src = (scope === 'latest' ? s.latest : s.best) || {}; return src[metric]; }
  function mockLeaderboard(metric, scope) {
    return MOCK.strategies.map(function (s) {
      var src = (scope === 'latest' ? s.latest : s.best) || {};
      return { strategy_id: s.strategy_id, metric: metric, scope: scope, value: _scopeVal(s, scope, metric), board: s.board, n_runs: s.n_runs, favorite: s.favorite, symbols: s.symbols,
        metrics: { total_return: src.total_return, annual_return: src.annual_return || src.total_return, sharpe: src.sharpe, sortino: src.sortino, calmar: src.calmar, max_drawdown: src.max_drawdown } };
    }).filter(function (r) { return r.value != null; }).sort(function (a, b) { return b.value - a.value; });
  }
  function mockStrategyDetail(id) {
    var s = MOCK.strategies.filter(function (x) { return x.strategy_id === id; })[0] || MOCK.strategies[0];
    var runs = [s.latest, s.best, { job_id: 'jx', status: 'completed', total_return: 0.052, max_drawdown: -0.03, sharpe: 1.1, created_at: '2026-05-10T00:00:00', start_date: '2026-01-05', end_date: '2026-03-31' }];
    return Object.assign({}, s, { runs: runs, equity: MOCK.equity, positions: MOCK.summary.positions, trades: MOCK.summary.trades });
  }
  function mockCompare(ids) {
    var list = String(ids).split(',').map(function (x) { return x.trim(); }).filter(Boolean);
    return { strategies: list.map(function (sid, i) {
      var s = MOCK.strategies.filter(function (x) { return x.strategy_id === sid; })[0] || MOCK.strategies[i % MOCK.strategies.length];
      return { strategy_id: sid, latest: s.latest, best: s.best, equity: { dates: DATES, equity: genSeries(N, 0.001 + i * 0.0006, 0.012, 7 + i * 5), rebase: true } };
    }), benchmark: state.benchmark };
  }
  api.strategies = function (metric) { return pick('/strategies?account_id=' + ACCOUNT + '&best_metric=' + (metric || 'total_return'), MOCK.strategies); };
  api.strategy = function (id, bm) { return pick('/strategies/' + encodeURIComponent(id) + '?account_id=' + ACCOUNT + (bm ? '&benchmark=' + bm : ''), mockStrategyDetail(id)); };
  api.leaderboard = function (metric, scope) { return pick('/leaderboard?account_id=' + ACCOUNT + '&metric=' + metric + '&scope=' + scope, mockLeaderboard(metric, scope)); };
  api.compareStrategies = function (ids, bm) { return pick('/strategies/compare?account_id=' + ACCOUNT + '&ids=' + encodeURIComponent(ids) + (bm ? '&benchmark=' + bm : ''), mockCompare(ids)); };
  api.setMeta = function (id, patch) { return FORCE === 'mock' ? Promise.resolve(patch) : jput('/strategies/' + encodeURIComponent(id) + '/meta?account_id=' + ACCOUNT, patch); };

  function badge(st, prog) {
    var p = (st === 'running' && prog) ? ' ' + Math.round(prog.pct * 100) + '%' : '';
    return '<span class="badge b-' + st + '">' + st + p + '</span>';
  }
  function setCrumbs(items) {
    crumbs.innerHTML = items.map(function (it, i) {
      var sep = i ? '<span>/</span>' : '';
      return sep + (it.h ? '<a href="' + it.h + '">' + esc(it.t) + '</a>' : '<span>' + esc(it.t) + '</span>');
    }).join(' ');
  }
  function destroyCharts() { charts.forEach(function (c) { try { c.destroy(); } catch (e) {} }); charts = []; }

  function renderToolbar() {
    api.benchmarks().then(function (b) {
      var opts = '<option value="">对标：关</option>' + b.items.map(function (it) {
        return '<option value="' + it.symbol + '"' + (it.symbol === state.benchmark ? ' selected' : '') + '>对标 ' + it.name + '</option>';
      }).join('');
      toolbar.innerHTML = '<select id="bm" aria-label="基准对标">' + opts + '</select>' +
        '<select id="theme" aria-label="主题"><option value="system">主题:系统</option><option value="light">浅色</option><option value="dark">深色</option></select>' +
        '<button id="refresh" aria-label="刷新">↻</button>';
      document.getElementById('theme').value = localStorage.getItem('vbt-theme') || 'system';
      document.getElementById('bm').addEventListener('change', function (e) { state.benchmark = e.target.value; route(); });
      document.getElementById('theme').addEventListener('change', function (e) { applyTheme(e.target.value); route(); });
      document.getElementById('refresh').addEventListener('click', route);
    });
  }

  function renderList() {
    destroyCharts();
    setNav('runs');
    api.list().then(function (jobs) {
      var rows = jobs.map(function (jb) {
        var s = jb.summary || {};
        var ret = jb.status === 'completed' ? '<span class="' + cls(s.total_return) + '">' + arrow(s.total_return) + pct(s.total_return) + '</span>' : '';
        var dd = jb.status === 'completed' ? pct(s.max_drawdown) : '';
        var last = jb.status === 'failed' ? '<span class="loss">' + esc(s.error || 'failed') + '</span>' : (s.trades != null ? s.trades + ' 笔' : '');
        var names = jb.strategy_ids || [];
        var nameHtml = names.length ? names.map(esc).join('<span class="muted">, </span>') : '<span class="muted">—</span>';
        return '<tr class="row" data-id="' + jb.job_id + '"><td>' + nameHtml + '<div class="note mono" style="font-size:11px;opacity:.55">' + jb.job_id + '</div></td><td>' + jb.account_id +
          '</td><td class="mono">' + jb.start_date + '→' + jb.end_date + '</td><td>' + badge(jb.status, jb.progress) +
          ((jb.status === 'queued' || jb.status === 'running') ? ' <button class="cancelbtn" data-id="' + jb.job_id + '" style="font-size:11px;padding:2px 6px">取消</button>' : '') +
          '</td><td class="num">' + ret + '</td><td class="num">' + dd + '</td><td>' + last + '</td></tr>';
      }).join('');
      app.innerHTML = '<div class="card"><table><thead><tr><th>策略</th><th>账户</th><th>区间</th><th>状态</th>' +
        '<th class="num">收益</th><th class="num">回撤</th><th>成交/原因</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
      Array.prototype.forEach.call(app.querySelectorAll('tr.row'), function (tr) {
        tr.tabIndex = 0;
        function go() { location.hash = '#/job/' + tr.dataset.id + '/overview'; }
        tr.addEventListener('click', go);
        tr.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
      });
      Array.prototype.forEach.call(app.querySelectorAll('.cancelbtn'), function (b) {
        b.addEventListener('click', function (e) {
          e.stopPropagation();
          if (!confirm('取消该作业？')) return;
          api.cancel(b.dataset.id).then(renderList).catch(function () { alert('取消失败（仅排队中可取消）'); });
        });
      });
    });
  }

  function chip(label, val, dim) { return '<div class="chip' + (dim ? ' dim' : '') + '"><div class="l">' + label + '</div><div class="v">' + val + '</div></div>'; }

  function kpiBlock(m) {
    var a = m.absolute || {}, r = m.risk_adjusted || {};
    return '<div class="card kpis">' +
      '<div class="kpi"><div class="l">累计收益</div><div class="v ' + cls(a.cumulative_return) + '">' + arrow(a.cumulative_return) + pct(a.cumulative_return) + '</div></div>' +
      '<div class="kpi"><div class="l">年化收益</div><div class="v ' + cls(a.annual_return) + '">' + pct(a.annual_return) + '</div></div>' +
      '<div class="kpi"><div class="l">最大回撤</div><div class="v loss">' + pct(a.max_drawdown) + '</div></div>' +
      '<div class="kpi"><div class="l">年化波动</div><div class="v">' + pct(a.annual_volatility) + '</div></div>' +
      '<div class="kpi"><div class="l">Sharpe</div><div class="v">' + fix(r.sharpe) + '</div></div>' +
      '<div class="kpi"><div class="l">样本</div><div class="v">' + (m.sample_days || '—') + 'd</div></div></div>';
  }

  function metricsBlock(m) {
    var dim = !!m.low_confidence, r = m.risk_adjusted || {}, br = m.benchmark_relative || {};
    var risk = chip('Sharpe', fix(r.sharpe), dim) + chip('Sortino', fix(r.sortino), dim) + chip('Calmar', fix(r.calmar), dim) + chip('VaR95%', pct(r.var_95), dim);
    var rel = m.benchmark_relative ? (chip('超额', pct(br.excess_return), dim) + chip('Alpha', pct(br.alpha), dim) + chip('Beta', fix(br.beta), dim) + chip('信息比率', fix(br.information_ratio), dim))
      : '<div class="chip dim"><div class="l">基准相对</div><div class="v">未对标</div></div>';
    var note = dim ? '<p class="note">样本不足（&lt;60 交易日），风险/基准指标仅供参考。</p>' : '';
    return '<div class="card"><div class="metricgrid">' +
      '<div><p class="grp-title">绝对</p><div class="kpis" style="grid-template-columns:1fr 1fr">' +
        chip('年化', pct((m.absolute || {}).annual_return)) + chip('波动', pct((m.absolute || {}).annual_volatility)) +
        chip('累计', pct((m.absolute || {}).cumulative_return)) + chip('回撤', pct((m.absolute || {}).max_drawdown)) + '</div></div>' +
      '<div><p class="grp-title">风险调整</p><div class="kpis" style="grid-template-columns:1fr 1fr">' + risk + '</div></div>' +
      '<div><p class="grp-title">基准相对 · ' + (state.benchmark || '关') + '</p><div class="kpis" style="grid-template-columns:1fr 1fr">' + rel + '</div></div>' +
      '</div>' + note + '</div>';
  }

  function chartCard() {
    return '<div class="card"><div style="font-weight:500;margin-bottom:8px">净值 · 起点 1.0</div>' +
      '<div style="height:260px"><canvas id="eq"></canvas></div>' +
      '<div style="height:90px;margin-top:6px"><canvas id="dd"></canvas></div>' +
      '<p class="note">滚轮缩放 · 拖拽平移 · 悬浮看十字线；策略=实线，基准=虚线。</p></div>';
  }

  function rejCard(rj) {
    var max = Math.max.apply(null, Object.keys(rj.counts).map(function (k) { return rj.counts[k]; }).concat([1]));
    var bars = Object.keys(rj.counts).map(function (k) {
      var w = Math.round(rj.counts[k] / max * 100);
      return '<div class="bar-row"><div class="top"><span>' + esc(k) + '</span><span class="mono">' + rj.counts[k] + '</span></div><div class="bar"><span style="width:' + w + '%"></span></div></div>';
    }).join('') || '<p class="note">0 拒单 ✓ 全部通过</p>';
    return '<div class="cols2"><div class="card"><div style="font-weight:500;margin-bottom:10px">拒单原因分布</div>' + bars + '</div>' +
      '<div class="card"><div style="font-weight:500;margin-bottom:10px">说明</div><p class="note">拒单≠作业失败：撮合规则透明化在此呈现。点上方曲线数据点可联动当日成交/持仓（接 /trades、/positions）。</p></div></div>';
  }

  function fallbackSvg(canvasId, series, note) {
    var cv = document.getElementById(canvasId);
    if (!cv || !cv.parentNode) return;
    var H = cv.parentNode.clientHeight || 240, W = 700, pad = 8;
    var all = [];
    series.forEach(function (s) { s.data.forEach(function (v) { if (v != null && isFinite(v)) all.push(v); }); });
    if (!all.length) { cv.parentNode.innerHTML = '<div class="muted" style="padding:20px">无数据</div>'; return; }
    var mn = Math.min.apply(null, all), mx = Math.max.apply(null, all);
    if (mn === mx) { mn -= 1; mx += 1; }
    function xx(i, n) { return pad + i * (W - 2 * pad) / Math.max(1, n - 1); }
    function yy(v) { return pad + (mx - v) / (mx - mn) * (H - 2 * pad); }
    var paths = series.map(function (s) {
      var d = s.data.map(function (v, i) { return (i ? 'L' : 'M') + xx(i, s.data.length).toFixed(1) + ' ' + yy(v).toFixed(1); }).join(' ');
      return '<path d="' + d + '" fill="none" stroke="' + s.color + '" stroke-width="' + (s.dash ? 1.5 : 2) + '"' + (s.dash ? ' stroke-dasharray="5 4"' : '') + '/>';
    }).join('');
    cv.parentNode.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="' + H + '" preserveAspectRatio="none" role="img">' + paths + '</svg>' +
      '<p class="note">' + (note || '静态预览 · Chart.js 未从 CDN 加载（离线/代理环境）；交互图需联网或本地化 Chart.js') + '</p>';
  }

  function drawEquity(eq) {
    if (!window.Chart) {
      var fs = [{ data: eq.equity, color: cssVar('--accent') }];
      if (eq.benchmark && eq.benchmark.available && eq.benchmark.values) fs.push({ data: eq.benchmark.values, color: cssVar('--muted'), dash: true });
      fallbackSvg('eq', fs);
      fallbackSvg('dd', [{ data: eq.drawdown.map(function (v) { return v * 100; }), color: cssVar('--loss') }], '回撤 % · 静态预览');
      return;
    }
    try { Chart.register(window.ChartZoom || window['chartjs-plugin-zoom']); } catch (e) {}
    var ds = [{ label: '策略', data: eq.equity, borderColor: cssVar('--accent'), borderWidth: 2, pointRadius: 0, tension: 0.15 }];
    if (eq.benchmark && eq.benchmark.available && eq.benchmark.values) {
      ds.push({ label: eq.benchmark.symbol, data: eq.benchmark.values, borderColor: cssVar('--muted'), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, tension: 0.15 });
    }
    var eqc = new Chart(document.getElementById('eq'), {
      type: 'line', data: { labels: eq.dates, datasets: ds },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { color: cssVar('--text'), boxWidth: 14 } },
          zoom: { zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' }, pan: { enabled: true, mode: 'x' } }
        },
        scales: { x: { ticks: { color: cssVar('--muted'), maxTicksLimit: 8 }, grid: { display: false } }, y: { ticks: { color: cssVar('--muted') }, grid: { color: cssVar('--border') } } }
      }
    });
    charts.push(eqc);
    var ddc = new Chart(document.getElementById('dd'), {
      type: 'line', data: { labels: eq.dates, datasets: [{ label: '回撤', data: eq.drawdown.map(function (v) { return v * 100; }), borderColor: cssVar('--loss'), backgroundColor: 'rgba(185,28,28,.15)', fill: true, borderWidth: 1, pointRadius: 0 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { ticks: { color: cssVar('--muted'), callback: function (v) { return v + '%'; } }, grid: { color: cssVar('--border') } } } }
    });
    charts.push(ddc);
  }

  function tabsHtml(id, tab) {
    return '<div class="tabs" role="tablist">' + ['overview', 'trades', 'rejections', 'positions', 'compare'].map(function (t) {
      return '<a class="tab ' + (t === tab ? 'on' : '') + '" role="tab" href="#/job/' + id + '/' + t + '">' + t + '</a>';
    }).join('') + '</div>';
  }

  function tradesTable(items) {
    var rows = (items || []).map(function (t) {
      return '<tr><td class="mono">' + t.trade_date + '</td><td class="mono">' + esc(t.symbol) +
        '</td><td class="' + (t.side_name === 'SELL' ? 'loss' : 'profit') + '">' + (t.side_name === 'SELL' ? '卖' : '买') +
        '</td><td class="num">' + t.quantity + '</td><td class="num">' + fix(t.price) + '</td><td class="num">' + money(t.amount) +
        '</td><td class="num">' + fix(t.commission) + '</td><td class="num">' + money(t.cash_after) + '</td></tr>';
    }).join('') || '<tr><td colspan="8" class="muted">无成交</td></tr>';
    return '<table><thead><tr><th>日期</th><th>标的</th><th>方向</th><th class="num">数量</th>' +
      '<th class="num">价格</th><th class="num">金额</th><th class="num">佣金</th><th class="num">现金余额</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function rejectionsControls(counts, selected) {
    var total = Object.keys(counts).reduce(function (a, k) { return a + counts[k]; }, 0);
    var opts = '<option value="">全部原因 (' + total + ')</option>' + Object.keys(counts).map(function (k) {
      return '<option value="' + esc(k) + '"' + (k === selected ? ' selected' : '') + '>' + esc(k) + ' (' + counts[k] + ')</option>';
    }).join('');
    return '<div style="margin-bottom:10px"><select id="reasonf" aria-label="按原因筛选">' + opts + '</select></div>';
  }

  function rejectionsTable(items) {
    var rows = (items || []).map(function (r) {
      return '<tr><td class="mono">' + r.trade_date + '</td><td class="mono">' + esc(r.symbol) +
        '</td><td>' + (r.side_name === 'SELL' ? '卖' : '买') + '</td><td class="num">' + r.quantity + '</td><td class="warn">' + esc(r.reason) + '</td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted">0 拒单 ✓ 全部通过</td></tr>';
    return '<table><thead><tr><th>日期</th><th>标的</th><th>方向</th><th class="num">数量</th><th>原因</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function wireTabControls(id) {
    var sel = document.getElementById('reasonf');
    if (sel) sel.addEventListener('change', function () { state.reasonFilter = sel.value; state.page.rejections = 0; renderOverview(id, 'rejections'); });
    Array.prototype.forEach.call(document.querySelectorAll('.pgbtn'), function (b) {
      b.addEventListener('click', function () {
        var tab = b.getAttribute('data-tab'), d = parseInt(b.getAttribute('data-d'), 10);
        state.page[tab] = Math.max(0, (state.page[tab] || 0) + d);
        renderOverview(id, tab);
      });
    });
  }

  function positionsTable(positions) {
    var rows = (positions || []).map(function (p) {
      return '<tr><td class="mono">' + esc(p.symbol) + '</td><td class="num">' + p.quantity + '</td><td class="num">' + p.available_quantity +
        '</td><td class="num">' + fix(p.cost_basis) + '</td><td class="num">' + fix(p.last_price) + '</td><td class="num">' + money(p.market_value) +
        '</td><td class="num ' + cls(p.unrealized_pnl) + '">' + money(p.unrealized_pnl) + '</td><td class="num ' + cls(p.unrealized_pnl_ratio) + '">' + pct(p.unrealized_pnl_ratio) + '</td></tr>';
    }).join('') || '<tr><td colspan="8" class="muted">无持仓</td></tr>';
    return '<table><thead><tr><th>标的</th><th class="num">数量</th><th class="num">可卖</th><th class="num">成本</th>' +
      '<th class="num">现价</th><th class="num">市值</th><th class="num">浮盈</th><th class="num">浮盈率</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function positionsView(s) { return '<div class="card">' + positionsTable(s.positions) + '</div>'; }

  function compareView(s) {
    var st = s.strategies || [];
    if (st.length <= 1) return '<div class="card muted">单策略回测，无对比。多策略时这里并列各子账户的收益/回撤/成交/拒单并叠加净值。</div>';
    var rows = st.map(function (x) {
      return '<tr><td class="mono">' + esc(x.strategy_id) + '</td><td class="num ' + cls(x.total_return) + '">' + pct(x.total_return) +
        '</td><td class="num loss">' + pct(x.max_drawdown) + '</td><td class="num">' + money(x.total_value) +
        '</td><td class="num">' + ((x.trades || []).length) + '</td><td class="num">' + ((x.rejections || []).length) + '</td></tr>';
    }).join('');
    var hasDaily = st.some(function (x) { return (x.daily || []).length; });
    var chart = hasDaily ? '<div class="card"><div style="font-weight:500;margin-bottom:8px">各策略净值（起点 1.0）</div><div style="height:260px"><canvas id="cmp"></canvas></div></div>' : '';
    return '<div class="card"><table><thead><tr><th>策略</th><th class="num">收益</th><th class="num">最大回撤</th>' +
      '<th class="num">期末权益</th><th class="num">成交</th><th class="num">拒单</th></tr></thead><tbody>' + rows + '</tbody></table></div>' + chart;
  }

  function drawCompare(s) {
    if (!document.getElementById('cmp')) return;
    var st = (s.strategies || []).filter(function (x) { return (x.daily || []).length; });
    if (!st.length) return;
    var palette = [cssVar('--accent'), cssVar('--warn'), cssVar('--profit'), cssVar('--loss'), cssVar('--muted')];
    if (!window.Chart) {
      var fs = st.map(function (x, i) {
        var base = x.daily[0].total_value || 1;
        return { data: x.daily.map(function (d) { return d.total_value / base; }), color: palette[i % palette.length] };
      });
      fallbackSvg('cmp', fs);
      return;
    }
    var labels = (st[0].daily || []).map(function (d) { return d.trade_date; });
    var ds = st.map(function (x, i) {
      var base = x.daily[0].total_value || 1;
      return { label: x.strategy_id, data: x.daily.map(function (d) { return Math.round(d.total_value / base * 1e4) / 1e4; }), borderColor: palette[i % palette.length], borderWidth: 2, pointRadius: 0, tension: 0.15 };
    });
    charts.push(new Chart(document.getElementById('cmp'), {
      type: 'line', data: { labels: labels, datasets: ds },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: cssVar('--text'), boxWidth: 14 } } }, scales: { x: { ticks: { color: cssVar('--muted'), maxTicksLimit: 8 }, grid: { display: false } }, y: { ticks: { color: cssVar('--muted') }, grid: { color: cssVar('--border') } } } }
    }));
  }

  function renderTradesTab(id) {  // 服务端分页：每页向后端取 limit/offset
    var p = state.page.trades || 0;
    api.tradesPage(id, { limit: PAGE, offset: p * PAGE }).then(function (res) {
      app.innerHTML = tabsHtml(id, 'trades') + '<div class="card">' + tradesTable(res.items) + pager('trades', res.total) + '</div>';
      wireTabControls(id);
    });
  }

  function renderRejectionsTab(id) {
    var p = state.page.rejections || 0, rf = state.reasonFilter || '';
    Promise.all([api.rejsum(id), api.rejectionsPage(id, { limit: PAGE, offset: p * PAGE, reason: rf })]).then(function (r) {
      var counts = (r[0] && r[0].counts) || {}, page = r[1];
      app.innerHTML = tabsHtml(id, 'rejections') + '<div class="card">' + rejectionsControls(counts, rf) + rejectionsTable(page.items) + pager('rejections', page.total) + '</div>';
      wireTabControls(id);
    });
  }

  function renderOverview(id, tab) {
    destroyCharts();
    state.jobId = id;
    setNav('runs');
    var bm = state.benchmark || '';
    if (tab === 'overview') {
      Promise.all([api.equity(id, bm), api.metrics(id, bm), api.rejsum(id)]).then(function (res) {
        var eq = res[0], m = res[1], rj = res[2];
        app.innerHTML = tabsHtml(id, tab) + kpiBlock(m) + metricsBlock(m) + chartCard() + rejCard(rj);
        drawEquity(eq);
        if (srLive) srLive.textContent = '回测 ' + id + ' 概览已加载，累计收益 ' + pct((m.absolute || {}).cumulative_return) + '。';
      });
      return;
    }
    if (tab === 'trades') { renderTradesTab(id); return; }
    if (tab === 'rejections') { renderRejectionsTab(id); return; }
    api.summary(id).then(function (s) {
      var body = tab === 'positions' ? positionsView(s) : compareView(s);
      app.innerHTML = tabsHtml(id, tab) + body;
      if (tab === 'compare') drawCompare(s);
    });
  }

  var cmpSel = {};
  var METRIC_LABEL = { total_return: '收益', annual_return: '年化', sharpe: 'Sharpe', sortino: 'Sortino', calmar: 'Calmar', annual_volatility: '波动', max_drawdown: '回撤' };
  function isPctMetric(m) { return ['total_return', 'annual_return', 'annual_volatility', 'max_drawdown'].indexOf(m) >= 0; }
  function fmtMetric(m, v) { return v == null ? '—' : (isPctMetric(m) ? pct(v) : fix(v)); }
  function statusBadgeCls(st) { return st === 'completed' ? 'b-completed' : st === 'running' ? 'b-running' : st === 'failed' ? 'b-failed' : 'b-queued'; }
  function setNav(active) {
    var items = [['home', '#/', '首页'], ['leaderboard', '#/leaderboard', '排行榜'], ['runs', '#/runs', '全部回测']];
    crumbs.innerHTML = '<span style="font-weight:500">vortex_backtest</span>' + items.map(function (it) {
      return '<span style="color:var(--muted)">·</span><a href="' + it[1] + '"' + (it[0] === active ? ' class="on"' : '') + '>' + it[2] + '</a>';
    }).join(' ');
  }

  function renderHome() {
    destroyCharts(); setNav('home');
    Promise.all([api.strategies('total_return'), api.leaderboard(state.lbMetric, state.lbScope)]).then(function (res) {
      var strs = res[0] || [], lb = res[1] || [];
      var running = strs.filter(function (s) { return s.running; });
      var now = Date.now();
      var recent7 = strs.filter(function (s) { return s.last_run_at && (now - Date.parse(s.last_run_at)) < 7 * 864e5; }).length;
      var best = strs.map(function (s) { return (s.best && s.best.total_return != null) ? s.best.total_return : null; }).filter(function (v) { return v != null; });
      var bestRet = best.length ? Math.max.apply(null, best) : null;
      var kpis = '<div class="kpis" style="margin-bottom:12px">' +
        '<div class="kpi"><div class="l">我的策略</div><div class="v mono">' + strs.length + '</div></div>' +
        '<div class="kpi"><div class="l">运行中</div><div class="v mono" style="color:var(--running)">' + running.length + '</div></div>' +
        '<div class="kpi"><div class="l">近 7 天活跃</div><div class="v mono">' + recent7 + '</div></div>' +
        '<div class="kpi"><div class="l">历史最优收益</div><div class="v mono profit">' + (bestRet == null ? '—' : pct(bestRet)) + '</div></div></div>';
      app.innerHTML = kpis + leaderboardCard(lb) + myStrategiesCard(strs) + '<div class="cols2">' + runningCard(running) + activityCard(strs) + '</div>';
      wireHome();
      if (srLive) srLive.textContent = '策略首页:' + strs.length + ' 个策略,' + running.length + ' 个运行中。';
    });
  }

  function lbControls() {
    var mopts = Object.keys(METRIC_LABEL).map(function (m) { return '<option value="' + m + '"' + (m === state.lbMetric ? ' selected' : '') + '>' + METRIC_LABEL[m] + '</option>'; }).join('');
    var sopts = [['best', '最优'], ['latest', '最新']].map(function (s) { return '<option value="' + s[0] + '"' + (s[0] === state.lbScope ? ' selected' : '') + '>' + s[1] + '</option>'; }).join('');
    return '<span style="display:flex;gap:6px;align-items:center"><span class="note">排序依据</span><select id="lbmetric" aria-label="排序依据">' + mopts + '</select><select id="lbscope" aria-label="范围">' + sopts + '</select></span>';
  }
  function lbHead() {
    function th(m) { return '<th class="num"' + (m === state.lbMetric ? ' style="color:var(--text)"' : '') + '>' + METRIC_LABEL[m] + '</th>'; }
    return '<thead><tr><th style="width:30px">#</th><th>策略</th><th>标的</th>' + th('total_return') + th('annual_return') + th('sharpe') + th('calmar') + th('max_drawdown') + '<th class="num">回测次数</th></tr></thead>';
  }
  function lbRows(lb, limit) {
    return (limit ? lb.slice(0, limit) : lb).map(function (r, i) {
      var m = r.metrics || {};
      return '<tr class="row lbrow2" data-id="' + esc(r.strategy_id) + '" style="cursor:pointer"><td><span class="rk' + (i === 0 ? ' rk1' : '') + '">' + (i + 1) + '</span></td>' +
        '<td class="mono">' + esc(r.strategy_id) + (r.favorite ? ' <span style="color:var(--warn)">★</span>' : '') + '</td>' +
        '<td class="muted" style="font-size:12px">' + (esc((r.symbols || []).join(' ')) || '—') + '</td>' +
        '<td class="num ' + cls(m.total_return) + '">' + pct(m.total_return) + '</td>' +
        '<td class="num ' + cls(m.annual_return) + '">' + pct(m.annual_return) + '</td>' +
        '<td class="num">' + fix(m.sharpe) + '</td><td class="num">' + fix(m.calmar) + '</td>' +
        '<td class="num loss">' + pct(m.max_drawdown) + '</td><td class="num">' + r.n_runs + '</td></tr>';
    }).join('') || '<tr><td colspan="9" class="muted">暂无可排名策略</td></tr>';
  }
  function leaderboardCard(lb) {
    return '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><span style="font-weight:500">排行榜</span>' + lbControls() + '</div>' +
      '<table>' + lbHead() + '<tbody>' + lbRows(lb, 8) + '</tbody></table></div>';
  }

  function runningCard(running) {
    if (!running.length) return '<div class="card"><div style="font-weight:500;margin-bottom:10px">运行中</div><p class="note">当前无运行中的策略</p></div>';
    var rows = running.map(function (s) {
      var L = s.latest || {}, pg = L.progress || {}, pc = Math.round((pg.pct || 0) * 100);
      return '<div style="margin-bottom:12px"><div style="display:flex;justify-content:space-between"><a class="mono" href="#/strategy/' + encodeURIComponent(s.strategy_id) + '">' + esc(s.strategy_id) + '</a><span class="mono ' + cls(L.total_return) + '">' + pct(L.total_return) + '</span></div>' +
        '<div style="height:6px;border-radius:4px;background:var(--surface);margin-top:4px"><div style="height:6px;width:' + pc + '%;border-radius:4px;background:var(--running)"></div></div>' +
        '<div class="note" style="margin-top:2px">' + (pg.trading_day ? '当前交易日 ' + pg.trading_day + ' · ' : '') + pc + '%</div></div>';
    }).join('');
    return '<div class="card"><div style="font-weight:500;margin-bottom:10px">运行中</div>' + rows + '</div>';
  }

  function myStrategiesCard(strs) {
    var rows = strs.map(function (s) {
      var L = s.latest || {}, B = s.best || {};
      var status = s.running ? '运行中' : (L.status || '—');
      var checked = cmpSel[s.strategy_id] ? ' checked' : '';
      var star = '<button class="star" data-id="' + esc(s.strategy_id) + '" aria-label="收藏" style="border:none;background:none;cursor:pointer;font-size:14px;color:' + (s.favorite ? 'var(--warn)' : 'var(--muted)') + '">★</button>';
      var pin = s.pinned ? ' <span class="tag">置顶</span>' : '';
      var tags = (s.tags || []).map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('');
      return '<tr class="row strow" data-id="' + esc(s.strategy_id) + '" style="cursor:pointer">' +
        '<td><input type="checkbox" class="cmpck" data-id="' + esc(s.strategy_id) + '"' + checked + ' aria-label="选入对比"></td>' +
        '<td>' + star + ' <span class="mono">' + esc(s.strategy_id) + '</span>' + pin + tags + '</td>' +
        '<td><span class="badge ' + statusBadgeCls(status === '运行中' ? 'running' : status) + '">' + status + '</span></td>' +
        '<td class="num ' + cls(L.total_return) + '">' + pct(L.total_return) + '</td>' +
        '<td class="num profit">' + pct(B.total_return) + '</td>' +
        '<td class="num">' + fix(B.sharpe) + '</td><td class="num">' + s.n_runs + '</td></tr>';
    }).join('') || '<tr><td colspan="7" class="muted">还没有策略 — 用 API 跑一次回测它就会出现</td></tr>';
    return '<div class="card"><div style="font-weight:500;margin-bottom:8px">我的策略</div>' +
      '<table><thead><tr><th style="width:28px"></th><th>策略</th><th>状态</th><th class="num">最新收益</th><th class="num">最优收益</th><th class="num">最优Sharpe</th><th class="num">回测次数</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div style="margin-top:10px;display:flex;align-items:center;gap:10px"><button id="cmpbtn">对比选中 →</button><span id="cmpsel" class="note"></span></div></div>';
  }

  function activityCard(strs) {
    var acts = strs.map(function (s) { return { id: s.strategy_id, at: s.last_run_at, L: s.latest || {} }; })
      .filter(function (a) { return a.at; }).sort(function (a, b) { return String(b.at).localeCompare(String(a.at)); }).slice(0, 7);
    var rows = acts.map(function (a) {
      var L = a.L, st = L.status, done = st === 'completed';
      var dot = st === 'running' ? 'var(--running)' : (st === 'failed' ? 'var(--loss)' : (done && L.total_return < 0 ? 'var(--loss)' : 'var(--profit)'));
      var verb = st === 'running' ? '运行中' : (st === 'failed' ? '失败' : '完成');
      var ret = (done && L.total_return != null) ? '<span class="mono ' + cls(L.total_return) + '">' + pct(L.total_return) + '</span>' : '';
      return '<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-top:.5px solid var(--border)">' +
        '<span style="width:8px;height:8px;border-radius:50%;background:' + dot + ';flex:none"></span>' +
        '<a class="mono" href="#/strategy/' + encodeURIComponent(a.id) + '" style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(a.id) + '</a>' +
        '<span class="tag">' + verb + '</span><span style="flex:1"></span>' + ret +
        '<span class="note" style="white-space:nowrap">' + relTime(a.at) + '</span></div>';
    }).join('') || '<p class="note">暂无活动</p>';
    return '<div class="card"><div style="font-weight:500;margin-bottom:4px">近期活动</div>' + rows + '</div>';
  }

  function wireHome() {
    Array.prototype.forEach.call(app.querySelectorAll('.strow'), function (tr) {
      tr.addEventListener('click', function (e) {
        if (e.target.closest('.star') || e.target.closest('.cmpck')) return;
        location.hash = '#/strategy/' + encodeURIComponent(tr.dataset.id);
      });
    });
    Array.prototype.forEach.call(app.querySelectorAll('.lbrow2'), function (el) {
      el.addEventListener('click', function () { location.hash = '#/strategy/' + encodeURIComponent(el.dataset.id); });
    });
    Array.prototype.forEach.call(app.querySelectorAll('.star'), function (b) {
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        api.setMeta(b.dataset.id, { favorite: true }).then(renderHome).catch(function () {});
      });
    });
    var ck = app.querySelectorAll('.cmpck');
    function refreshSel() {
      var ids = Object.keys(cmpSel).filter(function (k) { return cmpSel[k]; });
      var el = document.getElementById('cmpsel'); if (el) el.textContent = ids.length ? ('已选 ' + ids.length + ': ' + ids.join(', ')) : '勾选 ≥2 个策略对比';
    }
    Array.prototype.forEach.call(ck, function (c) { c.addEventListener('change', function () { cmpSel[c.dataset.id] = c.checked; refreshSel(); }); });
    refreshSel();
    var cmpbtn = document.getElementById('cmpbtn');
    if (cmpbtn) cmpbtn.addEventListener('click', function () {
      var ids = Object.keys(cmpSel).filter(function (k) { return cmpSel[k]; });
      if (ids.length < 2) { alert('请勾选至少 2 个策略'); return; }
      location.hash = '#/compare?ids=' + encodeURIComponent(ids.join(','));
    });
    var lm = document.getElementById('lbmetric'), ls = document.getElementById('lbscope');
    if (lm) lm.addEventListener('change', function () { state.lbMetric = lm.value; renderHome(); });
    if (ls) ls.addEventListener('change', function () { state.lbScope = ls.value; renderHome(); });
  }

  function renderStrategyDetail(id) {
    destroyCharts(); setNav('');
    api.strategy(id, state.benchmark).then(function (s) {
      var L = s.latest || {}, B = s.best || {};
      var header = '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap;margin-bottom:12px">' +
        '<div><a href="#/" class="note">← 我的策略</a><div style="font-size:18px;font-weight:500" class="mono">' + esc(id) + '</div>' +
        '<div class="note">' + (s.board || '') + ' · ' + (s.n_runs || 0) + ' 次回测 · ' + (s.symbols || []).join(', ') + '</div></div>' +
        '<div style="display:flex;gap:8px"><button id="favbtn">' + (s.favorite ? '★ 已收藏' : '☆ 收藏') + '</button><button id="pinbtn">' + (s.pinned ? '取消置顶' : '置顶') + '</button></div></div>';
      var cards = '<div class="kpis" style="margin-bottom:12px">' +
        '<div class="kpi"><div class="l">最新收益</div><div class="v mono ' + cls(L.total_return) + '">' + pct(L.total_return) + '</div></div>' +
        '<div class="kpi"><div class="l">最优收益</div><div class="v mono profit">' + pct(B.total_return) + '</div></div>' +
        '<div class="kpi"><div class="l">最优Sharpe</div><div class="v mono">' + fix(B.sharpe) + '</div></div>' +
        '<div class="kpi"><div class="l">最优Calmar</div><div class="v mono">' + fix(B.calmar) + '</div></div>' +
        '<div class="kpi"><div class="l">最新回撤</div><div class="v mono loss">' + pct(L.max_drawdown) + '</div></div></div>';
      var chart = s.equity ? '<div class="card"><div style="font-weight:500;margin-bottom:8px">最新一次净值 · 起点 1.0' + (state.benchmark ? ' · 对标 ' + state.benchmark : '') + '</div><div style="height:260px"><canvas id="eq"></canvas></div><div style="height:90px;margin-top:6px"><canvas id="dd"></canvas></div></div>' : '';
      var runs = (s.runs || []).map(function (r) {
        return '<tr class="row runrow" data-job="' + esc(r.job_id || '') + '" style="cursor:pointer"><td class="mono">' + String(r.created_at || '').slice(0, 10) + '</td>' +
          '<td class="mono">' + (r.start_date || '') + '→' + (r.end_date || '') + '</td><td><span class="badge ' + statusBadgeCls(r.status) + '">' + r.status + '</span></td>' +
          '<td class="num ' + cls(r.total_return) + '">' + pct(r.total_return) + '</td><td class="num loss">' + pct(r.max_drawdown) + '</td><td class="num">' + fix(r.sharpe) + '</td></tr>';
      }).join('') || '<tr><td colspan="6" class="muted">无回测</td></tr>';
      var runsCard = '<div class="card"><div style="font-weight:500;margin-bottom:8px">历次回测</div><table><thead><tr><th>日期</th><th>区间</th><th>状态</th><th class="num">收益</th><th class="num">回撤</th><th class="num">Sharpe</th></tr></thead><tbody>' + runs + '</tbody></table><div class="note" style="margin-top:8px">点任一回测 → 进入该次的成交/拒单/持仓明细</div></div>';
      var posCard = '<div class="card"><div style="font-weight:500;margin-bottom:8px">当前持仓 <span class="note">· 最新一次</span></div>' + positionsTable(s.positions) + '</div>';
      var tradesCard = '<div class="card"><div style="font-weight:500;margin-bottom:8px">成交记录 <span class="note">· 最新一次,近 ' + ((s.trades || []).length) + ' 笔</span></div>' + tradesTable(s.trades) +
        (s.latest_job_id ? '<div class="note" style="margin-top:8px"><a href="#/job/' + esc(s.latest_job_id) + '/trades">查看全部成交 →</a></div>' : '') + '</div>';
      app.innerHTML = header + cards + chart + runsCard + posCard + tradesCard;
      if (s.equity) drawEquity(s.equity);
      Array.prototype.forEach.call(app.querySelectorAll('.runrow'), function (tr) {
        if (!tr.dataset.job) return;
        tr.addEventListener('click', function () { location.hash = '#/job/' + tr.dataset.job + '/overview'; });
      });
      var fav = document.getElementById('favbtn'), pin = document.getElementById('pinbtn');
      if (fav) fav.addEventListener('click', function () { api.setMeta(id, { favorite: !s.favorite }).then(function () { renderStrategyDetail(id); }).catch(function () {}); });
      if (pin) pin.addEventListener('click', function () { api.setMeta(id, { pinned: !s.pinned }).then(function () { renderStrategyDetail(id); }).catch(function () {}); });
    });
  }

  function renderLeaderboard() {
    destroyCharts(); setNav('leaderboard');
    api.leaderboard(state.lbMetric, state.lbScope).then(function (lb) {
      app.innerHTML = '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><span style="font-weight:500">排行榜 · 全部策略</span>' + lbControls() + '</div>' +
        '<table>' + lbHead() + '<tbody>' + lbRows(lb) + '</tbody></table>' +
        '<div class="note" style="margin-top:8px">「排序依据」决定排名,所有指标同时展示;点策略进入详情。</div></div>';
      Array.prototype.forEach.call(app.querySelectorAll('.lbrow2'), function (tr) { tr.addEventListener('click', function () { location.hash = '#/strategy/' + encodeURIComponent(tr.dataset.id); }); });
      var lm = document.getElementById('lbmetric'), ls = document.getElementById('lbscope');
      if (lm) lm.addEventListener('change', function () { state.lbMetric = lm.value; renderLeaderboard(); });
      if (ls) ls.addEventListener('change', function () { state.lbScope = ls.value; renderLeaderboard(); });
    });
  }

  function renderCompareStrategies(ids) {
    destroyCharts(); setNav('');
    api.compareStrategies(ids, state.benchmark).then(function (d) {
      var ss = d.strategies || [];
      var head = '<div style="margin-bottom:12px"><a href="#/" class="note">← 我的策略</a><div style="font-size:18px;font-weight:500">策略对比</div></div>';
      if (ss.length < 1) { app.innerHTML = head + '<div class="card muted">没有可对比的策略</div>'; return; }
      var rowsM = [['total_return', '最优收益'], ['max_drawdown', '最优回撤'], ['sharpe', '最优Sharpe'], ['calmar', '最优Calmar']];
      var thead = '<tr><th>指标</th>' + ss.map(function (s) { return '<th class="num mono">' + esc(s.strategy_id) + '</th>'; }).join('') + '</tr>';
      var body = rowsM.map(function (mm) {
        return '<tr><td>' + mm[1] + '</td>' + ss.map(function (s) { var v = (s.best || {})[mm[0]]; return '<td class="num ' + (isPctMetric(mm[0]) ? cls(v) : '') + '">' + (isPctMetric(mm[0]) ? pct(v) : fix(v)) + '</td>'; }).join('') + '</tr>';
      }).join('');
      var table = '<div class="card"><div style="font-weight:500;margin-bottom:8px">指标对比</div><table><thead>' + thead + '</thead><tbody>' + body + '</tbody></table></div>';
      var chart = '<div class="card"><div style="font-weight:500;margin-bottom:8px">净值对比 · 起点 1.0</div><div style="height:280px"><canvas id="cmp"></canvas></div></div>';
      app.innerHTML = head + table + chart;
      drawCompareSeries(ss);
    });
  }

  function drawCompareSeries(entries) {
    var withEq = entries.filter(function (e) { return e.equity && (e.equity.equity || []).length; });
    if (!withEq.length || !document.getElementById('cmp')) return;
    var palette = [cssVar('--accent'), cssVar('--warn'), cssVar('--profit'), cssVar('--loss'), cssVar('--muted')];
    if (!window.Chart) {
      fallbackSvg('cmp', withEq.map(function (e, i) { return { data: e.equity.equity, color: palette[i % palette.length] }; }));
      return;
    }
    var labels = withEq[0].equity.dates;
    var ds = withEq.map(function (e, i) { return { label: e.strategy_id, data: e.equity.equity, borderColor: palette[i % palette.length], borderWidth: 2, pointRadius: 0, tension: 0.15 }; });
    charts.push(new Chart(document.getElementById('cmp'), {
      type: 'line', data: { labels: labels, datasets: ds },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: cssVar('--text'), boxWidth: 14 } } }, scales: { x: { ticks: { color: cssVar('--muted'), maxTicksLimit: 8 }, grid: { display: false } }, y: { ticks: { color: cssVar('--muted') }, grid: { color: cssVar('--border') } } } }
    }));
  }

  function parseQuery(q) { var o = {}; (q || '').split('&').forEach(function (kv) { var i = kv.indexOf('='); if (i > 0) o[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1)); }); return o; }

  function route() {
    var raw = location.hash.replace(/^#\/?/, '');
    var qi = raw.indexOf('?'); var path = qi >= 0 ? raw.slice(0, qi) : raw; var query = qi >= 0 ? raw.slice(qi + 1) : '';
    var p = path.split('/');
    if (p[0] === 'job' && p[1]) return renderOverview(p[1], p[2] || 'overview');
    if (p[0] === 'strategy' && p[1]) return renderStrategyDetail(decodeURIComponent(p[1]));
    if (p[0] === 'leaderboard') return renderLeaderboard();
    if (p[0] === 'compare') return renderCompareStrategies(parseQuery(query).ids || '');
    if (p[0] === 'runs') return renderList();
    return renderHome();
  }
  window.addEventListener('hashchange', route);
  matchMedia('(prefers-color-scheme:dark)').addEventListener('change', function () { if ((localStorage.getItem('vbt-theme') || 'system') === 'system') { applyTheme('system'); route(); } });
  renderToolbar();
  route();
})();
