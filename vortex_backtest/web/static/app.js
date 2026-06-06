(function () {
  'use strict';
  // 前端壳：默认 mock 数据，便于与后端并行开发。把 LIVE 置 true 即走同源真实 API。
  var LIVE = false;

  var app = document.getElementById('app');
  var toolbar = document.getElementById('toolbar');
  var crumbs = document.getElementById('crumbs');
  var srLive = document.getElementById('sr');
  var root = document.documentElement;
  var state = { benchmark: '000300.SH' };
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
    rejsum: { counts: { t_plus_1_not_sellable: 5, limit_up_buy_blocked: 3, insufficient_cash: 2, invalid_lot_size: 1 }, total: 11 }
  };

  function j(url) { return fetch(url).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }); }
  var api = {
    benchmarks: function () { return LIVE ? j('/benchmarks').catch(function () { return MOCK.benchmarks; }) : Promise.resolve(MOCK.benchmarks); },
    list: function (st) { return LIVE ? j('/backtests' + (st ? '?status=' + st : '')) : Promise.resolve(MOCK.jobs); },
    equity: function (id, bm) { return LIVE ? j('/backtests/' + id + '/equity?rebase=1' + (bm ? '&benchmark=' + bm : '')) : Promise.resolve(MOCK.equity); },
    metrics: function (id, bm) { return LIVE ? j('/backtests/' + id + '/metrics' + (bm ? '?benchmark=' + bm : '')) : Promise.resolve(MOCK.metrics); },
    rejsum: function (id) { return LIVE ? j('/backtests/' + id + '/rejections/summary') : Promise.resolve(MOCK.rejsum); }
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

  function drawEquity(eq) {
    if (!window.Chart) return;
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

  function renderOverview(id, tab) {
    destroyCharts();
    setCrumbs([{ t: '回测历史', h: '#/' }, { t: id }]);
    var bm = state.benchmark || '';
    Promise.all([api.equity(id, bm), api.metrics(id, bm), api.rejsum(id)]).then(function (res) {
      var eq = res[0], m = res[1], rj = res[2];
      var tabnames = ['overview', 'trades', 'rejections', 'positions', 'compare'];
      var tabs = '<div class="tabs" role="tablist">' + tabnames.map(function (t) {
        return '<a class="tab ' + (t === tab ? 'on' : '') + '" role="tab" href="#/job/' + id + '/' + t + '">' + t + '</a>';
      }).join('') + '</div>';
      var html = tabs;
      if (tab === 'overview') html += kpiBlock(m) + metricsBlock(m) + chartCard() + rejCard(rj);
      else html += '<div class="card muted">「' + tab + '」页为壳占位；真实数据接 /backtests/' + id + '/' + (tab === 'positions' ? 'positions' : tab) + '。</div>';
      app.innerHTML = html;
      if (tab === 'overview') { drawEquity(eq); if (srLive) srLive.textContent = '回测 ' + id + ' 概览已加载，累计收益 ' + pct((m.absolute || {}).cumulative_return) + '。'; }
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
