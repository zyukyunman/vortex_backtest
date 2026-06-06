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
  var state = { benchmark: '000300.SH', page: {}, reasonFilter: '', jobId: null };
  var PAGE = 25;
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
    var v = 100, out = [100], s = seed;
    for (var i = 1; i < n; i++) { s = (s * 9301 + 49297) % 233280; out.push(Math.round(v * (1 + drift + vol * (s / 233280 - 0.5)) * 100) / 100); v = out[i]; }
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
      { job_id: '57a2e1', account_id: 'demo', status: 'completed', start_date: '2026-01-05', end_date: '2026-03-31', frequency: '1min', summary: { total_return: EQ[N - 1] / 100 - 1, max_drawdown: Math.min.apply(null, DD), trades: 14, rejections: 11 } },
      { job_id: '3bd7aa', account_id: 'demo', status: 'running', start_date: '2026-01-05', end_date: '2026-03-31', frequency: '1min', progress: { trading_day: '2026-02-10', pct: 0.62 } },
      { job_id: 'dc0290', account_id: 'star', status: 'failed', start_date: '2026-01-05', end_date: '2026-01-10', frequency: '1min', summary: { error: 'minute_data_missing' } }],
    equity: { dates: DATES, equity: EQ, drawdown: DD, baseline: 100, rebase: true, benchmark: { symbol: '000300.SH', available: true, values: BM } },
    metrics: { sample_days: N, low_confidence: false,
      absolute: { cumulative_return: EQ[N - 1] / 100 - 1, annual_return: 0.221, annual_volatility: 0.183, max_drawdown: Math.min.apply(null, DD) },
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
    setCrumbs([{ t: '回测历史' }]);
    api.list().then(function (jobs) {
      var rows = jobs.map(function (jb) {
        var s = jb.summary || {};
        var ret = jb.status === 'completed' ? '<span class="' + cls(s.total_return) + '">' + arrow(s.total_return) + pct(s.total_return) + '</span>' : '';
        var dd = jb.status === 'completed' ? pct(s.max_drawdown) : '';
        var last = jb.status === 'failed' ? '<span class="loss">' + esc(s.error || 'failed') + '</span>' : (s.trades != null ? s.trades + ' 笔' : '');
        return '<tr class="row" data-id="' + jb.job_id + '"><td class="mono">' + jb.job_id + '</td><td>' + jb.account_id +
          '</td><td class="mono">' + jb.start_date + '→' + jb.end_date + '</td><td>' + badge(jb.status, jb.progress) +
          ((jb.status === 'queued' || jb.status === 'running') ? ' <button class="cancelbtn" data-id="' + jb.job_id + '" style="font-size:11px;padding:2px 6px">取消</button>' : '') +
          '</td><td class="num">' + ret + '</td><td class="num">' + dd + '</td><td>' + last + '</td></tr>';
      }).join('');
      app.innerHTML = '<div class="card"><table><thead><tr><th>job_id</th><th>账户</th><th>区间</th><th>状态</th>' +
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
    return '<div class="card"><div style="font-weight:500;margin-bottom:8px">净值 · 归一到 100</div>' +
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

  function tradesView(s) {
    var all = s.trades || [];
    var rows = slice(all, 'trades').map(function (t) {
      return '<tr><td class="mono">' + t.trade_date + '</td><td class="mono">' + esc(t.symbol) +
        '</td><td class="' + (t.side_name === 'SELL' ? 'loss' : 'profit') + '">' + (t.side_name === 'SELL' ? '卖' : '买') +
        '</td><td class="num">' + t.quantity + '</td><td class="num">' + fix(t.price) + '</td><td class="num">' + money(t.amount) +
        '</td><td class="num">' + fix(t.commission) + '</td><td class="num">' + money(t.cash_after) + '</td></tr>';
    }).join('') || '<tr><td colspan="8" class="muted">无成交</td></tr>';
    return '<div class="card"><table><thead><tr><th>日期</th><th>标的</th><th>方向</th><th class="num">数量</th>' +
      '<th class="num">价格</th><th class="num">金额</th><th class="num">佣金</th><th class="num">现金余额</th></tr></thead><tbody>' + rows + '</tbody></table>' + pager('trades', all.length) + '</div>';
  }

  function rejectionsView(s) {
    var all0 = s.rejections || [];
    var reasons = {};
    all0.forEach(function (r) { reasons[r.reason] = (reasons[r.reason] || 0) + 1; });
    var rf = state.reasonFilter || '';
    var all = rf ? all0.filter(function (r) { return r.reason === rf; }) : all0;
    var opts = '<option value="">全部原因 (' + all0.length + ')</option>' + Object.keys(reasons).map(function (k) {
      return '<option value="' + esc(k) + '"' + (k === rf ? ' selected' : '') + '>' + esc(k) + ' (' + reasons[k] + ')</option>';
    }).join('');
    var rows = slice(all, 'rejections').map(function (r) {
      return '<tr><td class="mono">' + r.trade_date + '</td><td class="mono">' + esc(r.symbol) +
        '</td><td>' + (r.side_name === 'SELL' ? '卖' : '买') + '</td><td class="num">' + r.quantity + '</td><td class="warn">' + esc(r.reason) + '</td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted">0 拒单 ✓ 全部通过</td></tr>';
    return '<div class="card"><div style="margin-bottom:10px"><select id="reasonf" aria-label="按原因筛选">' + opts + '</select></div>' +
      '<table><thead><tr><th>日期</th><th>标的</th><th>方向</th><th class="num">数量</th><th>原因</th></tr></thead><tbody>' + rows + '</tbody></table>' + pager('rejections', all.length) + '</div>';
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

  function positionsView(s) {
    var rows = (s.positions || []).map(function (p) {
      return '<tr><td class="mono">' + esc(p.symbol) + '</td><td class="num">' + p.quantity + '</td><td class="num">' + p.available_quantity +
        '</td><td class="num">' + fix(p.cost_basis) + '</td><td class="num">' + fix(p.last_price) + '</td><td class="num">' + money(p.market_value) +
        '</td><td class="num ' + cls(p.unrealized_pnl) + '">' + money(p.unrealized_pnl) + '</td><td class="num ' + cls(p.unrealized_pnl_ratio) + '">' + pct(p.unrealized_pnl_ratio) + '</td></tr>';
    }).join('') || '<tr><td colspan="8" class="muted">无持仓</td></tr>';
    return '<div class="card"><table><thead><tr><th>标的</th><th class="num">数量</th><th class="num">可卖</th><th class="num">成本</th>' +
      '<th class="num">现价</th><th class="num">市值</th><th class="num">浮盈</th><th class="num">浮盈率</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  function compareView(s) {
    var st = s.strategies || [];
    if (st.length <= 1) return '<div class="card muted">单策略回测，无对比。多策略时这里并列各子账户的收益/回撤/成交/拒单并叠加净值。</div>';
    var rows = st.map(function (x) {
      return '<tr><td class="mono">' + esc(x.strategy_id) + '</td><td class="num ' + cls(x.total_return) + '">' + pct(x.total_return) +
        '</td><td class="num loss">' + pct(x.max_drawdown) + '</td><td class="num">' + money(x.total_value) +
        '</td><td class="num">' + ((x.trades || []).length) + '</td><td class="num">' + ((x.rejections || []).length) + '</td></tr>';
    }).join('');
    var hasDaily = st.some(function (x) { return (x.daily || []).length; });
    var chart = hasDaily ? '<div class="card"><div style="font-weight:500;margin-bottom:8px">各策略净值（归一到 100）</div><div style="height:260px"><canvas id="cmp"></canvas></div></div>' : '';
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
        return { data: x.daily.map(function (d) { return d.total_value / base * 100; }), color: palette[i % palette.length] };
      });
      fallbackSvg('cmp', fs);
      return;
    }
    var labels = (st[0].daily || []).map(function (d) { return d.trade_date; });
    var ds = st.map(function (x, i) {
      var base = x.daily[0].total_value || 1;
      return { label: x.strategy_id, data: x.daily.map(function (d) { return r2(d.total_value / base * 100); }), borderColor: palette[i % palette.length], borderWidth: 2, pointRadius: 0, tension: 0.15 };
    });
    charts.push(new Chart(document.getElementById('cmp'), {
      type: 'line', data: { labels: labels, datasets: ds },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: cssVar('--text'), boxWidth: 14 } } }, scales: { x: { ticks: { color: cssVar('--muted'), maxTicksLimit: 8 }, grid: { display: false } }, y: { ticks: { color: cssVar('--muted') }, grid: { color: cssVar('--border') } } } }
    }));
  }

  function renderOverview(id, tab) {
    destroyCharts();
    state.jobId = id;
    setCrumbs([{ t: '回测历史', h: '#/' }, { t: id }]);
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
    api.summary(id).then(function (s) {
      var body = tab === 'trades' ? tradesView(s)
        : tab === 'rejections' ? rejectionsView(s)
          : tab === 'positions' ? positionsView(s)
            : compareView(s);
      app.innerHTML = tabsHtml(id, tab) + body;
      wireTabControls(id);
      if (tab === 'compare') drawCompare(s);
    });
  }

  function route() {
    var h = location.hash.replace(/^#\/?/, ''); var p = h.split('/');
    if (p[0] === 'job' && p[1]) renderOverview(p[1], p[2] || 'overview');
    else renderList();
  }
  window.addEventListener('hashchange', route);
  matchMedia('(prefers-color-scheme:dark)').addEventListener('change', function () { if ((localStorage.getItem('vbt-theme') || 'system') === 'system') { applyTheme('system'); route(); } });
  renderToolbar();
  route();
})();
