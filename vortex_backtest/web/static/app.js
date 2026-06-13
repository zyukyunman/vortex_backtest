(function () {
  'use strict';
  // 会话看板 v6（2026-06-12 重写）：只消费真实 sessions/分析端点，接口失败显式报错，无 mock。
  var app = document.getElementById('app');
  var crumbs = document.getElementById('crumbs');
  var charts = [];
  var state = { benchmark: '000300.SH', gran: 'daily', minuteDate: '', benchmarks: [], account: '', tab: 'equity',
                strat_sort: 'total_return', cmp_axis: 'calendar' };
  var detail = null;   // 详情页数据缓存 {sid, m, eq, dist}：切页签零请求
  var cmpSel = {};     // 排行榜对比篮：{strategy_id: true}
  var cmpItems = [];   // 叠加图曲线缓存 [{label, dates, values}]：切轴重画零请求

  function get(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status + ' ' + path); }
      return r.json();
    });
  }
  function fail(err) {
    app.innerHTML = '<div class="error-banner">后端不可达或数据缺失：' + esc(String(err && err.message || err)) +
      '<br>请确认服务在运行、会话存在；本看板不再展示演示数据。</div>';
  }
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function pct(x) { return x == null ? '—' : ((x >= 0 ? '+' : '') + (x * 100).toFixed(2) + '%'); }
  function num(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 2 : d); }
  function money(x) { return x == null ? '—' : Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function cls(x) { return x == null ? '' : (x >= 0 ? 'profit' : 'loss'); }
  function destroyCharts() { charts.forEach(function (c) { c.destroy(); }); charts = []; }

  // ---------------- 主题（浅/深/跟随系统 三态，localStorage 记忆）----------------
  var THEME_MODES = ['light', 'dark', 'system'];
  var THEME_LABEL = { light: '☀️ 浅色', dark: '🌙 深色', system: '🖥️ 跟随系统' };
  var _mql = window.matchMedia ? window.matchMedia('(prefers-color-scheme:dark)') : null;
  function themeMode() { return localStorage.getItem('vbt-theme') || 'system'; }
  function applyTheme(mode) {
    var dark = mode === 'dark' || (mode === 'system' && _mql && _mql.matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    localStorage.setItem('vbt-theme', mode);
    var btn = document.getElementById('theme-btn');
    if (btn) { btn.textContent = THEME_LABEL[mode]; }
    if (detail) { renderChartTab(); }   // 重画当前图，让随主题的颜色（热力图/分布图）刷新
  }
  function initTheme() {
    var tb = document.getElementById('toolbar');
    if (tb && !document.getElementById('theme-btn')) {
      var btn = document.createElement('button');
      btn.id = 'theme-btn';
      btn.title = '切换主题：浅 → 深 → 跟随系统';
      btn.addEventListener('click', function () {
        applyTheme(THEME_MODES[(THEME_MODES.indexOf(themeMode()) + 1) % THEME_MODES.length]);
      });
      tb.appendChild(btn);
    }
    if (_mql && _mql.addEventListener) {   // 跟随系统时，系统主题变化实时反映
      _mql.addEventListener('change', function () { if (themeMode() === 'system') { applyTheme('system'); } });
    }
    applyTheme(themeMode());
  }

  initTheme();
  window.addEventListener('hashchange', route);
  route();

  function route() {
    destroyCharts();
    var h = location.hash, m;
    if ((m = h.match(/^#\/strategy\/(.+)$/))) { renderStrategyDetail(decodeURIComponent(m[1])); }
    else if ((m = h.match(/^#\/compare\/(.+)$/))) { renderCompare(m[1].split(',').map(decodeURIComponent)); }
    else if (h.indexOf('#/strategies') === 0) { renderStrategies(); }
    else if ((m = h.match(/^#\/session\/([\w-]+)/))) { renderDetail(m[1]); }
    else { renderList(); }
  }

  // ---------------- 列表页 ----------------
  function renderList() {
    crumbs.innerHTML = '会话列表';
    app.innerHTML = '<div class="section"><h2>回测会话</h2><div id="list">加载中…</div></div>';
    get('/sessions').then(function (rows) {
      if (!rows.length) {
        document.getElementById('list').innerHTML = '<p>暂无会话。用 scripts/backtest_roundtrip.sh 或 POST /sessions 跑一笔回测。</p>';
        return;
      }
      return Promise.all(rows.map(function (r) {
        return get('/sessions/' + r.session_id + '/summary').then(function (s) { return { row: r, sum: s }; });
      })).then(function (items) {
        // 账户过滤走客户端：会话量小，切换下拉直接过滤已取回的行重渲染即可；
        // 量大时应改为重新请求 GET /sessions?account_id= 走服务端过滤。
        var accounts = [];
        items.forEach(function (it) {
          if (accounts.indexOf(it.row.account_id) < 0) { accounts.push(it.row.account_id); }
        });
        if (state.account && accounts.indexOf(state.account) < 0) { state.account = ''; }
        renderListTable(items, accounts);
      });
    }).catch(fail);
  }

  function renderListTable(items, accounts) {
    var acctSel = '<label>账户 <select id="acct-sel"><option value="">全部账户</option>' +
      accounts.map(function (a) {
        return '<option value="' + esc(a) + '"' + (a === state.account ? ' selected' : '') + '>' + esc(a) + '</option>';
      }).join('') + '</select></label>';
    var shown = state.account
      ? items.filter(function (it) { return it.row.account_id === state.account; }) : items;
    var html = '<p>' + acctSel + '</p>' +
      '<table class="kpi-table"><thead><tr><th>会话</th><th>账户</th><th>状态</th><th>区间</th>' +
      '<th>总收益</th><th>最大回撤</th><th>总资产</th><th>更新时间</th></tr></thead><tbody>';
    shown.forEach(function (it) {
      var r = it.row, s = it.sum;
      html += '<tr><td><a href="#/session/' + esc(r.session_id) + '">' + esc(r.session_id.slice(0, 8)) + '…</a></td>' +
        '<td>' + esc(r.account_id) + '</td><td>' + esc(r.status) + '</td>' +
        '<td>' + esc(r.start_date || '') + ' ~ ' + esc(r.end_date || '') + '</td>' +
        '<td class="' + cls(s.total_return) + '">' + pct(s.total_return) + '</td>' +
        '<td>' + pct(s.max_drawdown) + '</td><td>' + money(s.total_value) + '</td>' +
        '<td>' + esc(String(r.updated_at || '').slice(0, 16).replace('T', ' ')) + '</td></tr>';
    });
    document.getElementById('list').innerHTML = html + '</tbody></table>';
    document.getElementById('acct-sel').addEventListener('change', function (e) {
      state.account = e.target.value;
      renderListTable(items, accounts);
    });
  }

  // ---------------- 详情页 ----------------
  function renderDetail(sid) {
    destroyCharts(); // 基准下拉/粒度按钮/日期输入直调本函数不经 route，旧 Chart 实例须先销毁，否则被 Chart.js registry 持有泄漏
    crumbs.innerHTML = '<a href="#/">会话列表</a> / ' + esc(sid.slice(0, 8)) + '…';
    app.innerHTML = '<div class="section">加载中…</div>';
    var benchQ = '?benchmark=' + encodeURIComponent(state.benchmark);
    // hourly/minute 快照行多：默认 limit=500 取的是最前 500 行，slice(-30) 就不是"最近"；提到后端上限 5000。daily/weekly 量小不需要。
    var posQ = '?granularity=' + state.gran +
      (state.gran === 'minute' ? '&date=' + encodeURIComponent(state.minuteDate) : '') +
      (state.gran === 'hourly' || state.gran === 'minute' ? '&limit=5000' : '');
    Promise.all([
      get('/sessions/' + sid),
      state.benchmarks.length ? Promise.resolve(state.benchmarks) : get('/benchmarks'),
      get('/sessions/' + sid + '/metrics' + benchQ),
      get('/sessions/' + sid + '/equity' + benchQ),
      get('/sessions/' + sid + '/rebalances'),
      get('/sessions/' + sid + '/distributions'),
      (state.gran === 'minute' && !state.minuteDate)
        ? Promise.resolve(null) : get('/sessions/' + sid + '/positions' + posQ),
    ]).then(function (res) {
      state.benchmarks = res[1];
      detail = { sid: sid, m: res[2], eq: res[3], dist: res[5] };
      draw(sid, res[0], res[2], res[3], res[4], res[6]);
    }).catch(fail);
  }

  function metricRow(label, st, bm, rel, key, fmt) {
    fmt = fmt || pct;
    return '<tr><td>' + label + '</td>' +
      '<td class="' + cls(st && st[key]) + '">' + fmt(st && st[key]) + '</td>' +
      '<td>' + fmt(bm && bm[key]) + '</td>' +
      '<td>' + (rel == null ? '—' : fmt(rel)) + '</td></tr>';
  }

  function draw(sid, ses, m, eq, rebalances, positions) {
    // 契约：基准缺数时 benchmark_stats/relative 为 null —— bm 走 `bm && bm[key]`、rel 兜底空对象。
    var st = m.strategy, bm = m.benchmark_stats, rel = m.relative || {};
    var benchSel = '<select id="bench-sel">' + state.benchmarks.map(function (b) {
      return '<option value="' + esc(b.code) + '"' + (b.code === state.benchmark ? ' selected' : '') + '>' +
        esc(b.name) + ' (' + esc(b.code) + ')</option>';
    }).join('') + '</select>';
    var lowConf = m.low_confidence ? ' <span style="color:#9a6700">⚠︎ 样本&lt;60交易日，风险指标仅供参考</span>' : '';
    var html =
      '<div class="section"><h2>指标对比 · 基准 ' + benchSel + lowConf +
      (m.error ? ' <span style="color:#cf222e">（基准数据缺失，相对指标不可用）</span>' : '') + '</h2>' +
      '<table class="kpi-table"><thead><tr><th>指标</th><th>本策略</th><th>基准</th><th>相对</th></tr></thead><tbody>' +
      metricRow('总收益', st, bm, rel.excess_return, 'total_return') +
      metricRow('年化收益', st, bm, null, 'annual_return') +
      metricRow('夏普比率', st, bm, null, 'sharpe', function (x) { return num(x); }) +
      metricRow('最大回撤', st, bm, null, 'max_drawdown') +
      metricRow('收益波动率', st, bm, rel.tracking_error, 'volatility') +
      metricRow('正收益日占比', st, bm, null, 'win_days_ratio') +
      '<tr><td>信息比率 / Beta / Alpha</td><td colspan="3">' +
      num(rel.information_ratio) + ' / ' + num(rel.beta) + ' / ' + pct(rel.alpha) + '</td></tr>' +
      '</tbody></table></div>' +
      '<div class="section"><h2>图表 <span class="gran-switch" id="chart-tabs">' + chartTabsHtml() + '</span></h2>' +
      '<div id="chart-area"></div></div>' +
      periodTable('年度收益统计', m.annual) + periodTable('月度收益统计', m.monthly) +
      positionsSection(positions) + rebalanceSection(rebalances) +
      '<div class="section"><h2>原始数据</h2><p>' +
      '<a href="/sessions/' + sid + '/trades" target="_blank">成交</a> · ' +
      '<a href="/sessions/' + sid + '/rejections" target="_blank">拒单</a> · ' +
      '<a href="/sessions/' + sid + '/summary" target="_blank">汇总 JSON</a></p></div>';
    app.innerHTML = html;

    document.getElementById('bench-sel').addEventListener('change', function (e) {
      state.benchmark = e.target.value; renderDetail(sid);
    });
    bindGranSwitch(sid);
    bindChartTabs();
    renderChartTab();
  }

  function periodTable(title, rows) {
    if (!rows || !rows.length) { return ''; }
    var body = rows.map(function (r) {
      return '<tr><td>' + esc(r.period) + '</td>' +
        '<td class="' + cls(r.strategy_return) + '">' + pct(r.strategy_return) + '</td>' +
        '<td>' + pct(r.benchmark_return) + '</td><td>' + pct(r.excess) + '</td>' +
        '<td>' + pct(r.max_drawdown) + '</td><td>' + pct(r.benchmark_max_drawdown) + '</td>' +
        '<td>' + pct(r.volatility) + '</td><td>' + num(r.sharpe) + '</td></tr>';
    }).join('');
    return '<div class="section"><h2>' + title + '</h2><table class="kpi-table"><thead>' +
      '<tr><th>期间</th><th>策略收益</th><th>基准收益</th><th>超额</th><th>最大回撤</th><th>基准回撤</th><th>波动率</th><th>夏普</th></tr>' +
      '</thead><tbody>' + body + '</tbody></table></div>';
  }

  function positionsSection(rows) {
    var grans = [['daily', '日'], ['weekly', '周'], ['hourly', '时'], ['minute', '分']];
    var btns = grans.map(function (g) {
      return '<button data-g="' + g[0] + '" class="' + (state.gran === g[0] ? 'active' : '') + '">' + g[1] + '</button>';
    }).join('');
    var dateInput = state.gran === 'minute'
      ? ' <input id="minute-date" type="date" value="' + esc(state.minuteDate) + '"> （分钟粒度须选日期）' : '';
    var body = '';
    if (rows === null) {
      body = '<p>请选择日期后查看分钟级持仓。</p>';
    } else if (!rows.length) {
      body = '<p>该粒度暂无持仓快照。</p>';
    } else {
      body = rows.slice(-30).map(function (s) {     // 默认展示最近 30 个快照
        var ps = (s.positions || []).map(function (p) {
          return '<tr><td>' + esc(p.symbol) + '</td><td>' + p.quantity + '</td><td>' + num(p.last_price) +
            '</td><td>' + money(p.market_value) + '</td><td>' + pct(p.weight) +
            '</td><td class="' + cls(p.unrealized_pnl) + '">' + money(p.unrealized_pnl) + '</td></tr>';
        }).join('') || '<tr><td colspan="6">空仓</td></tr>';
        return '<details class="rebalance"><summary>' + esc(s.timestamp) + (s.week ? '（' + esc(s.week) + '）' : '') +
          ' · 总资产 ' + money(s.total_value) + ' · 现金 ' + money(s.cash) + '</summary>' +
          '<table class="kpi-table"><thead><tr><th>代码</th><th>数量</th><th>现价</th><th>市值</th><th>权重</th><th>浮盈</th></tr></thead>' +
          '<tbody>' + ps + '</tbody></table></details>';
      }).join('');
    }
    return '<div class="section"><h2>持仓快照 <span class="gran-switch">' + btns + '</span>' + dateInput +
      '</h2>' + body + '</div>';
  }

  function rebalanceSection(events) {
    if (!events || !events.length) {
      return '<div class="section"><h2>调仓记录</h2><p>无成交。</p></div>';
    }
    var body = events.map(function (e) {
      var legs = function (xs, label) {
        return (xs || []).map(function (x) {
          return label + ' ' + esc(x.symbol) + ' × ' + x.quantity + ' @ ' + num(x.avg_price, 4) + '（' + money(x.amount) + '）';
        }).join('<br>');
      };
      var diff = (e.position_diff || []).map(function (d) {
        return '<tr><td>' + esc(d.symbol) + '</td><td>' + d.qty_before + ' → ' + d.qty_after + '</td>' +
          '<td>' + pct(d.weight_before) + ' → ' + pct(d.weight_after) + '</td></tr>';
      }).join('');
      return '<details class="rebalance"><summary>' + esc(e.trade_date) + ' · ' + e.n_trades + ' 笔成交 · 费用 ' +
        money(e.fees_total) + ' · 已实现盈亏 <span class="' + cls(e.realized_pnl_total) + '">' +
        money(e.realized_pnl_total) + '</span></summary>' +
        '<p>' + [legs(e.buys, '买入'), legs(e.sells, '卖出')].filter(Boolean).join('<br>') + '</p>' +
        '<table class="kpi-table"><thead><tr><th>代码</th><th>数量变化</th><th>权重变化</th></tr></thead><tbody>' +
        diff + '</tbody></table>' +
        '<p>调仓后现金 ' + money(e.cash_after) + ' · 总资产 ' + money(e.total_value_after) + '</p></details>';
    }).join('');
    return '<div class="section"><h2>调仓记录（' + events.length + ' 次）</h2>' + body + '</div>';
  }

  function bindGranSwitch(sid) {
    // [data-g] 限定：图表页签容器复用了 .gran-switch 样式 class，不限定会误绑页签按钮（点页签→gran=undefined→422 崩页）
    Array.prototype.forEach.call(document.querySelectorAll('.gran-switch button[data-g]'), function (btn) {
      btn.addEventListener('click', function () { state.gran = btn.dataset.g; renderDetail(sid); });
    });
    var di = document.getElementById('minute-date');
    if (di) { di.addEventListener('change', function (e) { state.minuteDate = e.target.value; renderDetail(sid); }); }
  }

  // ---------------- 图表页签（二期：分布图表，spec 2026-06-13）----------------
  var TABS = [['equity', '净值曲线'], ['returns', '收益分布'], ['drawdowns', '回撤分布'],
              ['turnover', '换手率'], ['exposure', '仓位'], ['heatmap', '月度热力']];

  function chartTabsHtml() {
    return TABS.map(function (t) {
      return '<button data-tab="' + t[0] + '" class="' + (state.tab === t[0] ? 'active' : '') + '">' + t[1] + '</button>';
    }).join('');
  }

  function bindChartTabs() {
    Array.prototype.forEach.call(document.querySelectorAll('#chart-tabs button'), function (btn) {
      btn.addEventListener('click', function () {
        state.tab = btn.dataset.tab;
        Array.prototype.forEach.call(document.querySelectorAll('#chart-tabs button'), function (b) {
          b.className = b.dataset.tab === state.tab ? 'active' : '';
        });
        renderChartTab();   // 用 detail 缓存，零请求
      });
    });
  }

  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim() || '#888'; }

  function renderChartTab() {
    var area = document.getElementById('chart-area');
    if (!area || !detail) { return; }
    destroyCharts();   // 页签互斥，全量销毁再画（详情页同时只有一个 chart）
    var fns = { equity: tabEquity, returns: tabReturns, drawdowns: tabDrawdowns,
                turnover: tabTurnover, exposure: tabExposure, heatmap: tabHeatmap };
    fns[state.tab](area);
  }

  function tabEquity(area) {
    area.innerHTML = '<canvas id="eq" height="90"></canvas>';
    drawChart(detail.eq);
  }

  function tabReturns(area) {
    var h = detail.dist.return_histogram;
    if (!h.buckets.length) { area.innerHTML = '<p>数据不足。</p>'; return; }
    area.innerHTML = '<canvas id="dist-c" height="90"></canvas>';
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      type: 'bar',
      data: {
        labels: h.buckets.map(function (b) { return (b.lo * 100).toFixed(1) + '~' + (b.hi * 100).toFixed(1) + '%'; }),
        datasets: [{ label: '天数', data: h.buckets.map(function (b) { return b.count; }),
          backgroundColor: h.buckets.map(function (b) { return b.hi <= 0 ? cssVar('--loss') : cssVar('--profit'); }) }],
      },
      options: { animation: false, plugins: { legend: { display: false } } },
    }));
  }

  function tabDrawdowns(area) {
    var eps = detail.dist.drawdown_episodes;
    if (!eps.length) { area.innerHTML = '<p>无回撤事件。</p>'; return; }
    var rows = eps.map(function (e) {
      return '<tr><td>' + esc(e.peak_date) + '</td><td>' + esc(e.trough_date) + '</td>' +
        '<td class="loss">' + pct(e.depth) + '</td><td>' + e.drawdown_days + '</td>' +
        '<td>' + (e.recovered ? e.recovery_days : '进行中') + '</td></tr>';
    }).join('');
    area.innerHTML = '<table class="kpi-table"><thead><tr><th>峰值日</th><th>谷底日</th><th>深度</th>' +
      '<th>回撤天数</th><th>恢复天数</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<canvas id="dist-c" height="70"></canvas>';
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      type: 'bar',
      data: { labels: eps.map(function (e) { return e.peak_date; }),
        datasets: [{ label: '回撤深度%', data: eps.map(function (e) { return +(e.depth * 100).toFixed(2); }),
          backgroundColor: cssVar('--loss') }] },
      options: { animation: false, plugins: { legend: { display: false } } },
    }));
  }

  function tabTurnover(area) {
    var mt = detail.dist.monthly_turnover;
    if (!mt.length) { area.innerHTML = '<p>无成交。</p>'; return; }
    var mean = detail.dist.turnover_mean;
    area.innerHTML = '<canvas id="dist-c" height="90"></canvas>' +
      '<p class="muted">月度单边换手率 = min(月买入额, 月卖出额) ÷ 月日均总资产；均值 ' + pct(mean) + '</p>';
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      data: {
        labels: mt.map(function (r) { return r.month; }),
        datasets: [
          { type: 'bar', label: '换手率%', data: mt.map(function (r) { return r.turnover == null ? null : +(r.turnover * 100).toFixed(2); }),
            backgroundColor: '#0969da' },
          { type: 'line', label: '均值%', data: mt.map(function () { return mean == null ? null : +(mean * 100).toFixed(2); }),
            borderColor: '#9a6700', pointRadius: 0, borderWidth: 1.5, borderDash: [6, 4] },
        ],
      },
      options: { animation: false },
    }));
  }

  function tabExposure(area) {
    var ex = detail.dist.exposure;
    if (!ex.dates.length) { area.innerHTML = '<p>数据不足。</p>'; return; }
    area.innerHTML = '<canvas id="dist-c" height="90"></canvas>';
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      type: 'line',
      data: { labels: ex.dates,
        datasets: [{ label: '仓位%', data: ex.ratio.map(function (r) { return +(r * 100).toFixed(2); }),
          borderColor: '#0969da', backgroundColor: 'rgba(9,105,218,.15)', fill: true,
          pointRadius: 0, borderWidth: 1.5 }] },
      options: { animation: false, scales: { y: { min: 0, max: 100 } } },
    }));
  }

  function tabHeatmap(area) {
    var monthly = detail.m.monthly || [];
    if (!monthly.length) { area.innerHTML = '<p>数据不足。</p>'; return; }
    var byPeriod = {};
    var years = [];
    monthly.forEach(function (r) {
      byPeriod[r.period] = r.strategy_return;
      var y = r.period.slice(0, 4);
      if (years.indexOf(y) < 0) { years.push(y); }
    });
    var head = '<tr><th>年\\月</th>';
    for (var mm = 1; mm <= 12; mm++) { head += '<th>' + mm + '月</th>'; }
    head += '</tr>';
    var body = years.sort().map(function (y) {
      var tds = '';
      for (var i = 1; i <= 12; i++) {
        var key = y + '-' + (i < 10 ? '0' : '') + i;
        var v = byPeriod[key];
        if (v == null) { tds += '<td></td>'; continue; }
        // 透明度按 |收益| 线性映射，10% 封顶；红涨绿跌（A 股口径，与 --profit/--loss 一致）
        var a = Math.min(Math.abs(v) / 0.10, 1) * 0.85 + 0.1;
        var bg = 'rgba(var(' + (v >= 0 ? '--profit-rgb' : '--loss-rgb') + '),' + a.toFixed(2) + ')';  // 走主题变量，深色主题自动适配
        var fg = a >= 0.45 ? '#fff' : 'inherit';   // 低透明度底色配白字不可读 → 用默认前景色
        tds += '<td style="background:' + bg + ';color:' + fg + '" title="' + esc(key) + ' ' + pct(v) + '">' + pct(v) + '</td>';
      }
      return '<tr><th>' + esc(y) + '</th>' + tds + '</tr>';
    }).join('');
    area.innerHTML = '<table class="kpi-table heatmap"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
  }

  function drawChart(eq) {
    var canvas = document.getElementById('eq');
    if (!canvas || typeof Chart === 'undefined' || !eq.dates.length) { return; }
    // 契约：基线日（首日前一天）benchmark[0] 可能为 null —— Chart.js 对 null 默认断线即可，不开 spanGaps。
    var datasets = [
      { label: '策略', data: eq.strategy, borderColor: '#0969da', pointRadius: 0, borderWidth: 2, yAxisID: 'y' },
    ];
    if (eq.benchmark) {
      datasets.push({ label: '基准', data: eq.benchmark, borderColor: '#9a6700', pointRadius: 0, borderWidth: 1.5, yAxisID: 'y' });
    }
    datasets.push({ label: '回撤', data: eq.drawdown, borderColor: 'rgba(207,34,46,.6)',
      backgroundColor: 'rgba(207,34,46,.12)', fill: true, pointRadius: 0, borderWidth: 1, yAxisID: 'dd' });
    charts.push(new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels: eq.dates, datasets: datasets },
      options: {
        animation: false, interaction: { mode: 'index', intersect: false },
        scales: { y: { position: 'left' }, dd: { position: 'right', max: 0, grid: { display: false } } },
        plugins: { legend: { position: 'top' } },
      },
    }));
  }

  // ════════════════════ 策略中心：排行榜（spec 2026-06-13）════════════════════
  // 代表行指标列：[字段, 表头]
  var STRAT_COLS = [['total_return', '总收益'], ['annual_return', '年化'],
                    ['sharpe', '夏普'], ['max_drawdown', '最大回撤']];

  // 短样本护栏：年化/夏普/波动率等风险调整与年化指标 <60 交易日时置灰 + title（累计收益/回撤不受此限，沿用一期 design13 §7.2）
  function lowConfAttr(lowConf, key) {
    return (lowConf && (key === 'annual_return' || key === 'sharpe' || key === 'volatility'))
      ? ' style="opacity:.5" title="样本<60交易日，风险指标仅供参考"' : '';
  }

  function fmtStratCell(key, v, lowConf) {
    var disp = key === 'sharpe' ? num(v) : pct(v);
    var c = (key === 'total_return' || key === 'annual_return') ? cls(v) : '';
    return '<td class="' + c + '"' + lowConfAttr(lowConf, key) + '>' + disp + '</td>';
  }

  function sortStrategies(rows) {
    var key = state.strat_sort;
    return rows.slice().sort(function (a, b) {
      var av = a.latest[key], bv = b.latest[key];
      av = av == null ? -Infinity : av; bv = bv == null ? -Infinity : bv;
      return bv - av;   // 一律降序（收益/夏普越大越好；回撤为负数，越大=越浅亦合理）
    });
  }

  function renderStrategies() {
    crumbs.innerHTML = '策略中心';
    app.innerHTML = '<div class="section"><h2>策略排行榜</h2><div id="lb">加载中…</div></div>';
    get('/strategies').then(function (rows) {
      if (!rows.length) {
        document.getElementById('lb').innerHTML = '<p>暂无策略。跑几笔带 strategy_id 的回测后再来。</p>';
        return;
      }
      renderLeaderboard(rows);
    }).catch(fail);
  }

  function renderLeaderboard(rows) {
    var sorted = sortStrategies(rows);
    var heads = STRAT_COLS.map(function (c) {
      return '<th class="sortable" data-k="' + c[0] + '" style="cursor:pointer">' + c[1] +
        (state.strat_sort === c[0] ? ' ▾' : '') + '</th>';
    }).join('');
    var body = sorted.map(function (r) {
      var checked = cmpSel[r.strategy_id] ? ' checked' : '';
      return '<tr>' +
        '<td><input type="checkbox" class="cmp-cb" data-sid="' + esc(r.strategy_id) + '"' + checked + '></td>' +
        '<td><a href="#/strategy/' + encodeURIComponent(r.strategy_id) + '">' + esc(r.strategy_id) + '</a></td>' +
        '<td>' + r.n_runs + '</td>' +
        '<td>' + esc(r.accounts.join(', ')) + '</td>' +
        STRAT_COLS.map(function (c) { return fmtStratCell(c[0], r.latest[c[0]], r.latest.low_confidence); }).join('') +
        '<td class="' + cls(r.best.total_return) + '">' + pct(r.best.total_return) + '</td>' +
        '<td>' + esc(r.first_run || '') + ' ~ ' + esc(r.last_run || '') + '</td></tr>';
    }).join('');
    document.getElementById('lb').innerHTML =
      '<div id="cmp-bar" class="muted" style="margin-bottom:8px"></div>' +
      '<table class="kpi-table"><thead><tr><th></th><th>策略</th><th>回测数</th><th>账户</th>' +
      heads + '<th>历史最优</th><th>区间</th></tr></thead><tbody>' + body + '</tbody></table>';
    Array.prototype.forEach.call(document.querySelectorAll('th.sortable'), function (th) {
      th.addEventListener('click', function () { state.strat_sort = th.dataset.k; renderLeaderboard(rows); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('.cmp-cb'), function (cb) {
      cb.addEventListener('change', function () {
        if (cb.checked) { cmpSel[cb.dataset.sid] = true; } else { delete cmpSel[cb.dataset.sid]; }
        updateCmpBar();
      });
    });
    updateCmpBar();
  }

  function updateCmpBar() {
    var bar = document.getElementById('cmp-bar');
    if (!bar) { return; }
    var ids = Object.keys(cmpSel);
    if (ids.length < 2) { bar.innerHTML = '勾选 ≥2 个策略以横向对比。'; return; }
    bar.innerHTML = '已选 ' + ids.length + ' 个：' + esc(ids.join(', ')) +
      ' <a href="#/compare/' + ids.map(encodeURIComponent).join(',') + '">对比选中 →</a>';
  }

  // ──────────── 多曲线叠加（日历轴 / 相对日轴），策略详情与对比共用 ────────────
  var CURVE_PALETTE = ['#0969da', '#9a6700', '#1a7f37', '#cf222e', '#8250df', '#bf3989', '#0550ae', '#e16f24'];

  function drawMultiCurve(canvasId, series, axis) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === 'undefined' || !series.length) { return; }
    var labels, datasets;
    if (axis === 'relative') {
      // 对齐起点：x = 第 N 个交易日；各曲线按自身下标对齐（短曲线点少，自然在前段对齐）
      var maxLen = series.reduce(function (m, s) { return Math.max(m, s.values.length); }, 0);
      labels = []; for (var i = 0; i < maxLen; i++) { labels.push('D' + i); }
      datasets = series.map(function (s, k) {
        return { label: s.label, data: s.values, borderColor: CURVE_PALETTE[k % CURVE_PALETTE.length],
          pointRadius: 0, borderWidth: 1.6 };
      });
    } else {
      // 日历轴：并所有日期为统一 x 轴，各曲线缺日填 null（spanGaps 连线）
      var seen = {};
      series.forEach(function (s) { s.dates.forEach(function (d) { seen[d] = 1; }); });
      labels = Object.keys(seen).sort();
      datasets = series.map(function (s, k) {
        var byDate = {}; s.dates.forEach(function (d, i) { byDate[d] = s.values[i]; });
        return { label: s.label, spanGaps: true,
          data: labels.map(function (d) { return d in byDate ? byDate[d] : null; }),
          borderColor: CURVE_PALETTE[k % CURVE_PALETTE.length], pointRadius: 0, borderWidth: 1.6 };
      });
    }
    charts.push(new Chart(canvas.getContext('2d'), {
      type: 'line', data: { labels: labels, datasets: datasets },
      options: { animation: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'top' } } },
    }));
  }

  function axisSwitchHtml() {
    return ['calendar', 'relative'].map(function (a) {
      return '<button data-axis="' + a + '" class="' + (state.cmp_axis === a ? 'active' : '') + '">' +
        (a === 'calendar' ? '日历轴' : '对齐起点') + '</button>';
    }).join('');
  }

  function bindAxisSwitch(canvasId) {
    // cmpItems 为当前页缓存的曲线；切轴只销毁重画 canvas，不重取数据
    Array.prototype.forEach.call(document.querySelectorAll('#axis-sw button'), function (b) {
      b.addEventListener('click', function () {
        state.cmp_axis = b.dataset.axis;
        destroyCharts();
        Array.prototype.forEach.call(document.querySelectorAll('#axis-sw button'), function (x) {
          x.className = x.dataset.axis === state.cmp_axis ? 'active' : '';
        });
        drawMultiCurve(canvasId, cmpItems, state.cmp_axis);
      });
    });
  }

  // ──────────── 策略详情页 #/strategy/<id> ────────────
  function renderStrategyDetail(stratId) {
    destroyCharts();
    crumbs.innerHTML = '<a href="#/strategies">策略中心</a> / ' + esc(stratId);
    app.innerHTML = '<div class="section">加载中…</div>';
    get('/strategies/' + encodeURIComponent(stratId)).then(function (d) {
      return Promise.all(d.runs.map(function (run) {
        return get('/sessions/' + run.session_id + '/equity').then(function (eq) {
          return { run: run, eq: eq };
        });
      })).then(function (curves) { drawStrategyDetail(d, curves); });
    }).catch(fail);
  }

  function drawStrategyDetail(d, curves) {
    var runRows = d.runs.map(function (r) {
      return '<tr><td><a href="#/session/' + esc(r.session_id) + '">' + esc(r.session_id.slice(0, 8)) + '…</a></td>' +
        '<td>' + esc(r.start_date || '') + ' ~ ' + esc(r.end_date || '') + '</td>' +
        '<td>' + esc(r.status) + '</td>' +
        '<td class="' + cls(r.total_return) + '">' + pct(r.total_return) + '</td>' +
        '<td' + lowConfAttr(r.low_confidence, 'annual_return') + '>' + pct(r.annual_return) + '</td>' +
        '<td' + lowConfAttr(r.low_confidence, 'sharpe') + '>' + num(r.sharpe) + '</td>' +
        '<td>' + pct(r.max_drawdown) + '</td>' +
        '<td>' + esc(String(r.created_at || '').slice(0, 16).replace('T', ' ')) + '</td></tr>';
    }).join('');
    app.innerHTML =
      '<div class="section"><h2>' + esc(d.strategy_id) + '</h2><p class="muted">回测数 ' + d.n_runs +
      ' · 账户 ' + esc(d.accounts.join(', ')) +
      ' · 最新一次 <a href="#/session/' + esc(d.latest.session_id) + '">' + esc(d.latest.session_id.slice(0, 8)) +
      '…</a>（' + pct(d.latest.total_return) + '）· 历史最优 <a href="#/session/' + esc(d.best.session_id) + '">' +
      esc(d.best.session_id.slice(0, 8)) + '…</a>（' + pct(d.best.total_return) + '）</p></div>' +
      '<div class="section"><h2>历次回测</h2><table class="kpi-table"><thead><tr><th>会话</th><th>区间</th>' +
      '<th>状态</th><th>总收益</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>发起时间</th></tr></thead><tbody>' +
      runRows + '</tbody></table></div>' +
      '<div class="section"><h2>净值叠加 <span class="gran-switch" id="axis-sw">' + axisSwitchHtml() + '</span></h2>' +
      '<canvas id="sd-c" height="90"></canvas></div>';
    cmpItems = curves.map(function (c) {
      return { label: c.run.session_id.slice(0, 8), dates: c.eq.dates, values: c.eq.strategy };
    });
    drawMultiCurve('sd-c', cmpItems, state.cmp_axis);
    bindAxisSwitch('sd-c');
  }

  // ──────────── 对比视图 #/compare/<stratId,stratId,...> ────────────
  function renderCompare(stratIds) {
    destroyCharts();
    crumbs.innerHTML = '<a href="#/strategies">策略中心</a> / 对比';
    app.innerHTML = '<div class="section">加载中…</div>';
    get('/strategies').then(function (rows) {
      var byId = {}; rows.forEach(function (r) { byId[r.strategy_id] = r; });
      var picks = stratIds.filter(function (id) { return byId[id]; });
      if (picks.length < 2) {
        app.innerHTML = '<div class="error-banner">对比需要 ≥2 个有效策略。</div>'; return;
      }
      return Promise.all(picks.map(function (id) {
        var sess = byId[id].latest.session_id;   // 各策略取最新一次回测做代表
        return Promise.all([
          get('/sessions/' + sess + '/equity'),
          get('/sessions/' + sess + '/metrics'),
        ]).then(function (res) { return { id: id, eq: res[0], m: res[1] }; });
      })).then(drawCompare);
    }).catch(fail);
  }

  var CMP_METRICS = [['总收益', 'total_return'], ['年化', 'annual_return'], ['夏普', 'sharpe'],
                     ['最大回撤', 'max_drawdown'], ['波动率', 'volatility']];

  function drawCompare(items) {
    var head = '<tr><th>指标</th>' + items.map(function (it) { return '<th>' + esc(it.id) + '</th>'; }).join('') + '</tr>';
    var mbody = CMP_METRICS.map(function (mk) {
      return '<tr><td>' + mk[0] + '</td>' + items.map(function (it) {
        return fmtStratCell(mk[1], it.m.strategy[mk[1]], it.m.low_confidence);
      }).join('') + '</tr>';
    }).join('');
    app.innerHTML =
      '<div class="section"><h2>多策略净值叠加 <span class="gran-switch" id="axis-sw">' + axisSwitchHtml() + '</span></h2>' +
      '<canvas id="cmp-c" height="90"></canvas></div>' +
      '<div class="section"><h2>指标并排</h2><table class="kpi-table"><thead>' + head +
      '</thead><tbody>' + mbody + '</tbody></table></div>';
    cmpItems = items.map(function (it) { return { label: it.id, dates: it.eq.dates, values: it.eq.strategy }; });
    drawMultiCurve('cmp-c', cmpItems, state.cmp_axis);
    bindAxisSwitch('cmp-c');
  }
})();
