(function () {
  'use strict';
  // 会话看板 v6（2026-06-12 重写）：只消费真实 sessions/分析端点，接口失败显式报错，无 mock。
  var app = document.getElementById('app');
  var crumbs = document.getElementById('crumbs');
  var charts = [];
  var state = { benchmark: '000300.SH', gran: 'daily', minuteDate: '', benchmarks: [] };

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
  function esc(s) { return String(s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); }
  function pct(x) { return x == null ? '—' : ((x >= 0 ? '+' : '') + (x * 100).toFixed(2) + '%'); }
  function num(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 2 : d); }
  function money(x) { return x == null ? '—' : Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function cls(x) { return x == null ? '' : (x >= 0 ? 'profit' : 'loss'); }
  function destroyCharts() { charts.forEach(function (c) { c.destroy(); }); charts = []; }

  window.addEventListener('hashchange', route);
  route();

  function route() {
    destroyCharts();
    var m = location.hash.match(/^#\/session\/([\w-]+)/);
    if (m) { renderDetail(m[1]); } else { renderList(); }
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
        var html = '<table class="kpi-table"><thead><tr><th>会话</th><th>账户</th><th>状态</th><th>区间</th>' +
          '<th>总收益</th><th>最大回撤</th><th>总资产</th><th>更新时间</th></tr></thead><tbody>';
        items.forEach(function (it) {
          var r = it.row, s = it.sum;
          html += '<tr><td><a href="#/session/' + r.session_id + '">' + esc(r.session_id.slice(0, 8)) + '…</a></td>' +
            '<td>' + esc(r.account_id) + '</td><td>' + esc(r.status) + '</td>' +
            '<td>' + esc(r.start_date || '') + ' ~ ' + esc(r.end_date || '') + '</td>' +
            '<td class="' + cls(s.total_return) + '">' + pct(s.total_return) + '</td>' +
            '<td>' + pct(s.max_drawdown) + '</td><td>' + money(s.total_value) + '</td>' +
            '<td>' + esc(String(r.updated_at || '').slice(0, 16).replace('T', ' ')) + '</td></tr>';
        });
        document.getElementById('list').innerHTML = html + '</tbody></table>';
      });
    }).catch(fail);
  }

  // ---------------- 详情页 ----------------
  function renderDetail(sid) {
    crumbs.innerHTML = '<a href="#/">会话列表</a> / ' + esc(sid.slice(0, 8)) + '…';
    app.innerHTML = '<div class="section">加载中…</div>';
    var benchQ = '?benchmark=' + encodeURIComponent(state.benchmark);
    var posQ = '?granularity=' + state.gran +
      (state.gran === 'minute' ? '&date=' + encodeURIComponent(state.minuteDate) : '');
    Promise.all([
      get('/sessions/' + sid),
      state.benchmarks.length ? Promise.resolve(state.benchmarks) : get('/benchmarks'),
      get('/sessions/' + sid + '/metrics' + benchQ),
      get('/sessions/' + sid + '/equity' + benchQ),
      get('/sessions/' + sid + '/rebalances'),
      (state.gran === 'minute' && !state.minuteDate)
        ? Promise.resolve(null) : get('/sessions/' + sid + '/positions' + posQ),
    ]).then(function (res) {
      state.benchmarks = res[1];
      draw(sid, res[0], res[2], res[3], res[4], res[5]);
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
      '<div class="section"><h2>净值曲线（起点 1.0，副轴回撤）</h2><canvas id="eq" height="90"></canvas></div>' +
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
    drawChart(eq);
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
    Array.prototype.forEach.call(document.querySelectorAll('.gran-switch button'), function (btn) {
      btn.addEventListener('click', function () { state.gran = btn.dataset.g; renderDetail(sid); });
    });
    var di = document.getElementById('minute-date');
    if (di) { di.addEventListener('change', function (e) { state.minuteDate = e.target.value; renderDetail(sid); }); }
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
})();
