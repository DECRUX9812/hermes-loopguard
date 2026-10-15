(function () {
  'use strict';

  var SDK = window.__HERMES_PLUGIN_SDK__ || {};
  var React = SDK.React || window.React;
  if (!React) {
    console.error('LoopGuard: React is not available on window or SDK');
    return;
  }

  var h = React.createElement;
  var hooks = SDK.hooks || React;
  var useState = hooks.useState;
  var useEffect = hooks.useEffect;
  var useCallback = hooks.useCallback;
  var useMemo = hooks.useMemo;
  var useRef = hooks.useRef;

  var fetchJSON = SDK.fetchJSON || (async function (url, init) {
    var res = await fetch(url, init);
    if (!res.ok) {
      var text = await res.text();
      var msg = text;
      try {
        var parsed = JSON.parse(text);
        if (parsed.detail) msg = parsed.detail;
      } catch (e) {}
      throw new Error(msg || ('HTTP ' + res.status));
    }
    return res.json();
  });

  var utils = SDK.utils || {};
  var timeAgo = utils.timeAgo || function (ts) {
    if (!ts) return '';
    var diff = Math.max(0, Math.floor((Date.now() - Number(ts)) / 1000));
    if (diff < 5) return 'just now';
    if (diff < 60) return diff + 's ago';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
  };

  function formatTokens(n) {
    if (n === null || n === undefined || isNaN(n)) return '0';
    return new Intl.NumberFormat('en-US', {
      notation: 'compact',
      maximumFractionDigits: 1
    }).format(Number(n));
  }

  function formatCurrency(n) {
    if (n === null || n === undefined || isNaN(n)) return '$0.00';
    var val = Number(n);
    var abs = Math.abs(val);
    var fractionDigits = (abs > 0 && abs < 0.10) ? 4 : 2;
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits
    }).format(val);
  }

  function formatPercent(n) {
    if (n === null || n === undefined || isNaN(n)) return '0%';
    return Math.round(Number(n) * 100) + '%';
  }

  /* Route mix: stats.routes keys are the watcher's actual route names, which are
     usually custom ("bug", "duplicate_or_spam"), so never assume the five standard ones. */
  var KNOWN_ROUTES = ['agent', 'kanban', 'review', 'drop', 'shadow'];

  function routeEntries(routesMap) {
    var map = routesMap || {};
    var entries = [];
    for (var i = 0; i < KNOWN_ROUTES.length; i++) {
      if ((map[KNOWN_ROUTES[i]] || 0) > 0) {
        entries.push({ name: KNOWN_ROUTES[i], count: map[KNOWN_ROUTES[i]] });
      }
    }
    for (var rk in map) {
      if (KNOWN_ROUTES.indexOf(rk) === -1 && (map[rk] || 0) > 0) {
        entries.push({ name: rk, count: map[rk] });
      }
    }
    return entries;
  }

  function routeColor(name) {
    return KNOWN_ROUTES.indexOf(name) !== -1
      ? 'var(--lg-color-' + name + ')'
      : 'var(--lg-color-custom)';
  }

  function routeLabel(name) {
    return KNOWN_ROUTES.indexOf(name) !== -1
      ? name.charAt(0).toUpperCase() + name.slice(1)
      : name;
  }

  /* Icons */
  function ShieldIcon(props) {
    return h('svg', {
      width: props.size || 16,
      height: props.size || 16,
      viewBox: '0 0 24 24',
      fill: 'none',
      stroke: 'currentColor',
      strokeWidth: 2,
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
      className: props.className
    },
      h('path', { d: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z' })
    );
  }

  /* Inline SVG Sparkline */
  function Sparkline(props) {
    var data = props.data;
    var color = props.color || 'var(--color-primary, #3b82f6)';
    if (!data || data.length === 0) {
      return h('svg', { className: 'lg-sparkline', viewBox: '0 0 100 28' },
        h('line', { x1: 0, y1: 14, x2: 100, y2: 14, stroke: 'currentColor', strokeOpacity: 0.15, strokeWidth: 1.5 })
      );
    }
    var numericData = data.map(function (v) { return Number(v) || 0; });
    var max = Math.max.apply(null, numericData.concat([1]));
    var min = Math.min.apply(null, numericData.concat([0]));
    var range = (max - min) || 1;
    var len = numericData.length;

    var pts = numericData.map(function (v, i) {
      var x = len > 1 ? (i / (len - 1)) * 94 + 3 : 50;
      var y = 24 - (((v - min) / range) * 20);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');

    var lastX = len > 1 ? 97 : 50;
    var lastY = 24 - ((((numericData[len - 1] || 0) - min) / range) * 20);

    return h('svg', { className: 'lg-sparkline', viewBox: '0 0 100 28' },
      h('polyline', {
        fill: 'none',
        stroke: color,
        strokeWidth: 2,
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        points: pts
      }),
      h('circle', {
        cx: lastX.toFixed(1),
        cy: lastY.toFixed(1),
        r: 3,
        fill: color
      })
    );
  }

  /* Skipped vs Woken Stacked Bars */
  function StackedBarChart(props) {
    var timeline = props.timeline || [];
    if (!timeline.length) {
      return h('div', { className: 'lg-card-heading', style: { padding: '32px', textAlign: 'center', color: 'var(--color-muted-foreground)' } },
        'No timeline activity recorded yet'
      );
    }

    var maxVal = 1;
    for (var i = 0; i < timeline.length; i++) {
      var tot = (timeline[i].skipped || 0) + (timeline[i].woken || 0);
      if (tot > maxVal) maxVal = tot;
    }
    // Round maxVal up to nice tick
    var roundMax = maxVal <= 5 ? 5 : maxVal <= 10 ? 10 : Math.ceil(maxVal / 5) * 5;

    var svgWidth = 600;
    var svgHeight = 180;
    var padLeft = 40;
    var padRight = 20;
    var padTop = 20;
    var padBottom = 30;
    var chartW = svgWidth - padLeft - padRight;
    var chartH = svgHeight - padTop - padBottom;
    var count = timeline.length;
    var slotW = chartW / count;
    var barW = Math.max(10, Math.min(28, slotW * 0.65));

    var elements = [];

    // Accessible title
    elements.push(h('title', { key: 'chart-title' }, 'Daily skipped vs woken runs chart'));

    // Grid lines & labels
    var ticks = [0, roundMax / 2, roundMax];
    for (var t = 0; t < ticks.length; t++) {
      var val = ticks[t];
      var y = padTop + chartH - (val / roundMax) * chartH;
      elements.push(h('line', {
        key: 'grid-' + t,
        x1: padLeft,
        y1: y,
        x2: svgWidth - padRight,
        y2: y,
        className: 'lg-chart-grid-line'
      }));
      elements.push(h('text', {
        key: 'grid-lbl-' + t,
        x: padLeft - 8,
        y: y + 4,
        textAnchor: 'end',
        className: 'lg-chart-axis-text'
      }, String(Math.round(val))));
    }

    // Bars
    for (var b = 0; b < count; b++) {
      var item = timeline[b];
      var skipped = item.skipped || 0;
      var woken = item.woken || 0;
      var bx = padLeft + b * slotW + (slotW - barW) / 2;

      var skipH = (skipped / roundMax) * chartH;
      var wokeH = (woken / roundMax) * chartH;

      var skipY = padTop + chartH - skipH;
      var wokeY = skipY - wokeH;

      var tipText = (item.day || '') + ': ' + skipped + ' skipped, ' + woken + ' woken' +
        (item.tokens_avoided ? ' (' + formatTokens(item.tokens_avoided) + ' tokens avoided)' : '');

      // Woken bar (top)
      if (woken > 0) {
        elements.push(h('rect', {
          key: 'bar-woke-' + b,
          x: bx,
          y: wokeY,
          width: barW,
          height: Math.max(2, wokeH),
          fill: 'var(--lg-color-agent)',
          rx: 2
        }, h('title', null, tipText)));
      }

      // Skipped bar (bottom)
      if (skipped > 0) {
        elements.push(h('rect', {
          key: 'bar-skip-' + b,
          x: bx,
          y: skipY,
          width: barW,
          height: Math.max(2, skipH),
          fill: 'var(--lg-color-skip)',
          rx: 2
        }, h('title', null, tipText)));
      }

      // Day label
      var dayLabel = item.day ? (item.day.length > 5 ? item.day.slice(5) : item.day) : '';
      elements.push(h('text', {
        key: 'day-lbl-' + b,
        x: bx + barW / 2,
        y: svgHeight - 10,
        textAnchor: 'middle',
        className: 'lg-chart-axis-text'
      }, dayLabel));
    }

    return h('svg', {
      className: 'lg-chart-svg',
      viewBox: '0 0 ' + svgWidth + ' ' + svgHeight,
      preserveAspectRatio: 'xMidYMid meet'
    }, elements);
  }

  /* Decision Feed Row */
  function DecisionRow(props) {
    var d = props.decision;
    var expanded = props.expanded;
    var onToggle = props.onToggle;

    var kindClass = 'lg-chip-agent';
    var k = (d.kind || '').toLowerCase();
    if (k === 'skip' || k === 'change_gate') kindClass = 'lg-chip-skip';
    else if (k === 'semantic') kindClass = 'lg-chip-semantic';
    else if (k === 'route') kindClass = 'lg-chip-agent';
    else if (k === 'tool') kindClass = 'lg-chip-tool';
    else if (k === 'completion') kindClass = 'lg-chip-completion';
    else if (k === 'kanban') kindClass = 'lg-chip-kanban';
    else if (k === 'budget') kindClass = 'lg-chip-budget';
    else if (k === 'error') kindClass = 'lg-chip-error';

    // Top prob from answers
    var topProb = null;
    if (d.answers && typeof d.answers === 'object') {
      for (var qKey in d.answers) {
        var ans = d.answers[qKey];
        if (ans && typeof ans === 'object') {
          if (typeof ans.confidence === 'number') {
            topProb = Math.max(topProb || 0, ans.confidence);
          } else if (typeof ans.noul === 'number') {
            topProb = Math.max(topProb || 0, ans.noul);
          }
        }
      }
    }

    var routeDisplay = d.route || d.proposed_route || d.outcome || '-';
    var modelDisplay = (d.backend || '') + (d.model ? ' / ' + d.model : '');

    return h('div', { key: 'dec-' + d.id },
      h('div', {
        className: 'lg-feed-row',
        role: 'button',
        tabIndex: 0,
        onClick: onToggle,
        onKeyDown: function (e) { if (e.key === 'Enter' || e.key === ' ') onToggle(); }
      },
        h('div', { className: 'lg-feed-left' },
          h('span', { className: 'lg-chip ' + kindClass }, d.kind || 'decision'),
          h('span', { className: 'lg-feed-item-id' }, d.item_id ? '#' + d.item_id : (d.watcher || 'run')),
          h('span', { className: 'lg-code-badge' }, routeDisplay)
        ),
        h('div', { className: 'lg-feed-right' },
          topProb !== null ? h('div', {
            className: 'lg-meter-bar',
            title: 'Confidence: ' + Math.round(topProb * 100) + '%'
          },
            h('div', {
              className: 'lg-meter-fill',
              style: { width: Math.min(100, Math.round(topProb * 100)) + '%' }
            })
          ) : null,
          modelDisplay ? h('span', { className: 'lg-feed-model' }, modelDisplay) : null,
          h('span', { className: 'lg-feed-time lg-tabular' }, d.created_at ? timeAgo(d.created_at * 1000) : '')
        )
      ),
      expanded ? h('pre', { className: 'lg-json-block' },
        JSON.stringify({
          id: d.id,
          watcher: d.watcher,
          kind: d.kind,
          item_id: d.item_id,
          route: d.route,
          proposed_route: d.proposed_route,
          shadow: d.shadow,
          backend: d.backend,
          model: d.model,
          outcome: d.outcome,
          tokens_in: d.tokens_in,
          tokens_out: d.tokens_out,
          cost_usd: d.cost_usd,
          note: d.note,
          answers: d.answers
        }, null, 2)
      ) : null
    );
  }

  /* Cron Top Banner Component */
  function CronBanner() {
    var stateArr = useState({ skipped: 0, tokens: 0, loaded: false });
    var bannerData = stateArr[0];
    var setBannerData = stateArr[1];

    useEffect(function () {
      fetchJSON('/api/plugins/loopguard/stats?since=24h')
        .then(function (data) {
          setBannerData({
            skipped: data.skipped_runs || 0,
            tokens: data.tokens_avoided || 0,
            loaded: true
          });
        })
        .catch(function () {});
    }, []);

    if (!bannerData.loaded || bannerData.skipped === 0) return null;

    return h('div', { className: 'lg-slot-banner' },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        h(ShieldIcon, { size: 16 }),
        h('span', null,
          'LoopGuard skipped ' + bannerData.skipped + ' runs, ~' + formatTokens(bannerData.tokens) + ' tokens avoided (24h)'
        )
      ),
      h('a', {
        // The dashboard is a path-routed SPA: a hash href would not navigate.
        // Keep the current query (profile preselect) on the hop.
        href: '/loopguard' + (window.location.search || ''),
        className: 'lg-slot-banner-link'
      }, 'View LoopGuard')
    );
  }

  /* Main LoopGuard Dashboard Page */
  function LoopGuardPage() {
    var rangeState = useState('24h');
    var range = rangeState[0];
    var setRange = rangeState[1];

    var watcherFilterState = useState('');
    var watcherFilter = watcherFilterState[0];
    var setWatcherFilter = watcherFilterState[1];

    var statsState = useState(null);
    var stats = statsState[0];
    var setStats = statsState[1];

    var watchersState = useState([]);
    var watchers = watchersState[0];
    var setWatchers = watchersState[1];

    var reviewsState = useState([]);
    var reviews = reviewsState[0];
    var setReviews = reviewsState[1];

    var errorState = useState(null);
    var error = errorState[0];
    var setError = errorState[1];

    var lastUpdatedState = useState(Date.now());
    var lastUpdated = lastUpdatedState[0];
    var setLastUpdated = lastUpdatedState[1];

    var tickState = useState(0);
    var setTick = tickState[1];

    var expandedState = useState(null);
    var expandedId = expandedState[0];
    var setExpandedId = expandedState[1];

    var reviewActionPendingState = useState({});
    var reviewActionPending = reviewActionPendingState[0];
    var setReviewActionPending = reviewActionPendingState[1];

    // Fetch data
    var loadData = useCallback(function () {
      var query = '?since=' + (range === 'all' ? '' : range);
      if (watcherFilter) query += '&watcher=' + encodeURIComponent(watcherFilter);

      var pStats = fetchJSON('/api/plugins/loopguard/stats' + query);
      var pWatchers = fetchJSON('/api/plugins/loopguard/watchers');
      var pReview = fetchJSON('/api/plugins/loopguard/review');

      return Promise.all([pStats, pWatchers, pReview])
        .then(function (results) {
          setStats(results[0]);
          setWatchers(results[1] || []);
          setReviews(results[2] || []);
          setError(null);
          setLastUpdated(Date.now());
        })
        .catch(function (err) {
          setError(err.message || 'Failed to load LoopGuard data');
        });
    }, [range, watcherFilter]);

    // Initial load and filter change
    useEffect(function () {
      loadData();
    }, [loadData]);

    // Live dot seconds ticker
    useEffect(function () {
      var timer = setInterval(function () {
        setTick(function (t) { return t + 1; });
      }, 1000);
      return function () { clearInterval(timer); };
    }, []);

    // 15s auto-refresh, paused when document is hidden
    useEffect(function () {
      var refreshTimer = setInterval(function () {
        if (!document.hidden) {
          loadData();
        }
      }, 15000);
      return function () { clearInterval(refreshTimer); };
    }, [loadData]);

    // Review action handler
    var handleReviewAction = useCallback(function (reviewId, action) {
      setReviewActionPending(function (prev) {
        var copy = Object.assign({}, prev);
        copy[reviewId] = action;
        return copy;
      });

      fetchJSON('/api/plugins/loopguard/review/' + reviewId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action })
      })
        .then(function () {
          loadData();
        })
        .catch(function (err) {
          setError('Review action failed: ' + (err.message || String(err)));
        })
        .finally(function () {
          setReviewActionPending(function (prev) {
            var copy = Object.assign({}, prev);
            delete copy[reviewId];
            return copy;
          });
        });
    }, [loadData]);

    // Render Sparkline data arrays
    var timeline = (stats && stats.timeline) ? stats.timeline : [];
    var skippedSpark = timeline.map(function (d) { return d.skipped || 0; });
    var avoidedSpark = timeline.map(function (d) { return d.tokens_avoided || 0; });
    var savedSpark = timeline.map(function (d) { return d.usd_avoided || 0; });
    var completedSpark = timeline.map(function (d) { return d.woken || 0; });

    var secondsAgo = Math.max(0, Math.floor((Date.now() - lastUpdated) / 1000));

    // Empty state check
    var isEmpty = stats && (stats.polls === 0) && (stats.items_gated === 0);

    return h('div', { className: 'lg-root' },

      // Header row
      h('div', { className: 'lg-header' },
        h('div', { className: 'lg-title-group' },
          h('div', { className: 'lg-title-row' },
            h(ShieldIcon, { size: 28, className: 'lg-chip-agent' }),
            h('h1', { className: 'lg-title' }, 'LoopGuard')
          ),
          h('p', { className: 'lg-subtitle' },
            'Automations only wake the big model when it is worth it'
          )
        ),
        h('div', { className: 'lg-controls-group' },
          // Range switch
          h('div', { className: 'lg-range-switch' },
            ['24h', '7d', '30d', 'all'].map(function (r) {
              return h('button', {
                key: r,
                className: 'lg-range-btn' + (range === r ? ' lg-range-btn-active' : ''),
                onClick: function () { setRange(r); }
              }, r);
            })
          ),
          // Watcher filter
          h('select', {
            className: 'lg-select',
            value: watcherFilter,
            onChange: function (e) { setWatcherFilter(e.target.value); }
          },
            h('option', { value: '' }, 'All Watchers'),
            watchers.map(function (w) {
              return h('option', { key: w.name, value: w.name }, w.name);
            })
          ),
          // Live dot
          h('div', { className: 'lg-live-indicator' },
            h('span', { className: 'lg-live-dot' }),
            h('span', { className: 'lg-tabular' }, 'updated ' + secondsAgo + 's ago')
          ),
          // Refresh button
          h('button', {
            className: 'lg-btn lg-btn-sm',
            onClick: function () { loadData(); },
            title: 'Refresh now'
          }, 'Refresh')
        )
      ),

      // Error state banner
      error ? h('div', { className: 'lg-error-banner' },
        h('span', null, error),
        h('button', {
          className: 'lg-btn lg-btn-sm lg-btn-danger',
          onClick: function () { loadData(); }
        }, 'Retry')
      ) : null,

      // Empty state friendly card
      isEmpty ? h('div', { className: 'lg-empty-card' },
        h(ShieldIcon, { size: 36 }),
        h('h3', { className: 'lg-empty-title' }, 'No LoopGuard activity yet'),
        h('p', { className: 'lg-empty-desc' },
          'LoopGuard gates your cron jobs and automations so unchanged polls never call a model. Run these commands to get started:'
        ),
        h('pre', { className: 'lg-quickstart-box' },
          'hermes plugins enable loopguard\nhermes loopguard watch install examples/issue_triage.yaml\nhermes loopguard stats'
        )
      ) : null,

      // KPI Strip (4 cards)
      h('div', { className: 'lg-kpi-strip' },
        // Runs skipped
        h('div', { className: 'lg-kpi-card' },
          h('div', { className: 'lg-kpi-header' },
            h('span', { className: 'lg-kpi-title' }, 'Runs Skipped')
          ),
          h('div', { className: 'lg-kpi-number lg-tabular' },
            stats ? (stats.skipped_runs || 0) : '0'
          ),
          h('div', { className: 'lg-kpi-footer' },
            h('span', { className: 'lg-kpi-caption' },
              stats ? formatPercent(stats.skip_rate) + ' skip rate' : '0% skip rate'
            ),
            h(Sparkline, { data: skippedSpark, color: 'var(--lg-color-skip)' })
          )
        ),

        // Tokens avoided
        h('div', { className: 'lg-kpi-card' },
          h('div', { className: 'lg-kpi-header' },
            h('span', { className: 'lg-kpi-title' }, 'Tokens Avoided')
          ),
          h('div', { className: 'lg-kpi-number lg-tabular' },
            stats ? formatTokens(stats.tokens_avoided || 0) : '0'
          ),
          h('div', { className: 'lg-kpi-footer' },
            h('span', { className: 'lg-kpi-caption' },
              stats ? '~' + formatCurrency(stats.usd_avoided) + ' avoided' : '$0.00 avoided'
            ),
            h(Sparkline, { data: avoidedSpark, color: 'var(--lg-color-agent)' })
          )
        ),

        // Net saved
        h('div', { className: 'lg-kpi-card' },
          h('div', { className: 'lg-kpi-header' },
            h('span', { className: 'lg-kpi-title' }, 'Net Saved')
          ),
          h('div', { className: 'lg-kpi-number lg-tabular' },
            stats ? formatCurrency(stats.net_usd_saved || 0) : '$0.00'
          ),
          h('div', { className: 'lg-kpi-footer' },
            h('span', { className: 'lg-kpi-caption' },
              stats ? 'after ' + formatCurrency(stats.gate_usd) + ' gate cost' : 'after gate cost'
            ),
            h(Sparkline, { data: savedSpark, color: 'var(--lg-color-skip)' })
          )
        ),

        // Cost per completed task
        h('div', { className: 'lg-kpi-card' },
          h('div', { className: 'lg-kpi-header' },
            h('span', { className: 'lg-kpi-title' }, 'Cost / Completed Task')
          ),
          h('div', { className: 'lg-kpi-number lg-tabular' },
            stats && stats.cost_per_completed !== null && stats.cost_per_completed !== undefined
              ? formatCurrency(stats.cost_per_completed)
              : 'N/A'
          ),
          h('div', { className: 'lg-kpi-footer' },
            h('span', { className: 'lg-kpi-caption' },
              stats ? (stats.completed_items || 0) + ' completed items' : '0 completed items'
            ),
            h(Sparkline, { data: completedSpark, color: 'var(--color-primary)' })
          )
        )
      ),

      // Skipped vs Woken Stacked Bar Chart
      h('div', { className: 'lg-chart-card' },
        h('div', { className: 'lg-card-title-row' },
          h('h3', { className: 'lg-card-heading' }, 'Skipped vs Woken Runs'),
          h('div', { className: 'lg-chart-legend' },
            h('div', { className: 'lg-legend-item' },
              h('span', { className: 'lg-legend-swatch', style: { backgroundColor: 'var(--lg-color-skip)' } }),
              h('span', null, 'Skipped')
            ),
            h('div', { className: 'lg-legend-item' },
              h('span', { className: 'lg-legend-swatch', style: { backgroundColor: 'var(--lg-color-agent)' } }),
              h('span', null, 'Woken')
            )
          )
        ),
        h(StackedBarChart, { timeline: timeline })
      ),

      // Two columns: (a) Watchers table, (b) Route mix
      h('div', { className: 'lg-two-col' },
        // Watchers table
        h('div', { className: 'lg-subcard' },
          h('h3', { className: 'lg-card-heading' }, 'Configured Watchers'),
          h('div', { className: 'lg-table-wrapper' },
            h('table', { className: 'lg-table' },
              h('thead', null,
                h('tr', null,
                  h('th', null, 'Watcher'),
                  h('th', { className: 'lg-tabular' }, 'Polls'),
                  h('th', { className: 'lg-tabular' }, 'Skipped'),
                  h('th', { className: 'lg-tabular' }, 'Woken'),
                  h('th', { className: 'lg-tabular' }, 'Avoided'),
                  h('th', null, 'Route Mix')
                )
              ),
              h('tbody', null,
                watchers.length === 0 ? h('tr', null,
                  h('td', { colSpan: 6, style: { textAlign: 'center', color: 'var(--color-muted-foreground)' } },
                    'No active watchers'
                  )
                ) : watchers.map(function (w) {
                  var wStats = (stats && stats.per_watcher && stats.per_watcher[w.name]) || {};
                  var wEntries = routeEntries(wStats.routes);
                  var totalRoutes = 0;
                  for (var wi = 0; wi < wEntries.length; wi++) totalRoutes += wEntries[wi].count;

                  return h('tr', { key: w.name },
                    h('td', null,
                      h('span', { className: 'lg-code-badge' }, w.name)
                    ),
                    h('td', { className: 'lg-tabular' }, wStats.polls || 0),
                    h('td', { className: 'lg-tabular' }, wStats.skipped_runs || 0),
                    h('td', { className: 'lg-tabular' }, wStats.woken_runs || 0),
                    h('td', { className: 'lg-tabular' }, formatTokens(wStats.tokens_avoided || 0)),
                    h('td', null,
                      h('div', { className: 'lg-segmented-bar-small' },
                        totalRoutes > 0 ? wEntries.map(function (ent) {
                          var pct = (ent.count / totalRoutes) * 100;
                          return h('div', {
                            key: ent.name,
                            className: 'lg-seg-slice',
                            style: {
                              width: pct + '%',
                              backgroundColor: routeColor(ent.name)
                            },
                            title: ent.name + ': ' + ent.count
                          });
                        }) : h('div', { className: 'lg-seg-slice', style: { width: '100%', opacity: 0.2 } })
                      )
                    )
                  );
                })
              )
            )
          )
        ),

        // Route mix breakdown
        h('div', { className: 'lg-subcard' },
          h('h3', { className: 'lg-card-heading' }, 'Route Mix'),
          (function () {
            var entries = routeEntries(stats && stats.routes);
            var totalCount = 0;
            for (var i = 0; i < entries.length; i++) totalCount += entries[i].count;

            if (totalCount === 0) {
              return h('div', { className: 'lg-route-breakdown' },
                h('div', { className: 'lg-segmented-bar-large' },
                  h('div', { className: 'lg-seg-slice', style: { width: '100%', opacity: 0.15 } })
                ),
                h('div', { style: { color: 'var(--color-muted-foreground)', fontSize: '13px' } },
                  'No routed runs in this range'
                )
              );
            }

            return h('div', { className: 'lg-route-breakdown' },
              // Large segmented bar
              h('div', { className: 'lg-segmented-bar-large' },
                entries.map(function (ent) {
                  var pct = (ent.count / totalCount) * 100;
                  return h('div', {
                    key: ent.name,
                    className: 'lg-seg-slice',
                    style: {
                      width: pct + '%',
                      backgroundColor: routeColor(ent.name)
                    },
                    title: ent.name + ': ' + ent.count + ' (' + Math.round(pct) + '%)'
                  });
                })
              ),
              // Route list
              h('div', { className: 'lg-route-list' },
                entries.map(function (ent) {
                  var pct = Math.round((ent.count / totalCount) * 100);
                  return h('div', { key: ent.name, className: 'lg-route-row' },
                    h('div', { className: 'lg-route-name-group' },
                      h('span', {
                        className: 'lg-legend-swatch',
                        style: { backgroundColor: routeColor(ent.name) }
                      }),
                      h('span', null, routeLabel(ent.name))
                    ),
                    h('div', { className: 'lg-tabular', style: { display: 'flex', gap: '12px' } },
                      h('span', null, String(ent.count)),
                      h('span', { style: { color: 'var(--color-muted-foreground)', minWidth: '36px', textAlign: 'right' } },
                        pct + '%'
                      )
                    )
                  );
                })
              )
            );
          })()
        )
      ),

      // Decision Feed
      h('div', { className: 'lg-feed-card' },
        h('div', { className: 'lg-card-title-row' },
          h('h3', { className: 'lg-card-heading' }, 'Decision Feed (Recent 25)'),
          h('span', { className: 'lg-kpi-caption' }, 'Click row to inspect typed answers')
        ),
        (function () {
          var decs = (stats && stats.recent && stats.recent.length) ? stats.recent : [];
          if (!decs.length) {
            return h('div', { style: { padding: '24px', textAlign: 'center', color: 'var(--color-muted-foreground)' } },
              'No decisions recorded yet'
            );
          }
          return h('div', { className: 'lg-feed-list' },
            decs.map(function (d) {
              return h(DecisionRow, {
                key: d.id,
                decision: d,
                expanded: expandedId === d.id,
                onToggle: function () {
                  setExpandedId(expandedId === d.id ? null : d.id);
                }
              });
            })
          );
        })()
      ),

      // Review Queue
      h('div', { className: 'lg-review-card' },
        h('div', { className: 'lg-card-title-row' },
          h('h3', { className: 'lg-card-heading' }, 'Open Review Queue'),
          h('span', { className: 'lg-chip lg-chip-review' },
            (reviews.length) + ' pending'
          )
        ),
        reviews.length === 0 ? h('div', {
          style: { padding: '24px', textAlign: 'center', color: 'var(--color-muted-foreground)' }
        },
          'Review queue is clear. No items awaiting approval.'
        ) : h('div', { className: 'lg-review-grid' },
          reviews.map(function (r) {
            var isPending = reviewActionPending[r.id];
            return h('div', { key: r.id, className: 'lg-review-item' },
              h('div', { className: 'lg-review-item-header' },
                h('h4', { className: 'lg-review-item-title' },
                  r.title || ('Item #' + r.item_id)
                ),
                h('span', { className: 'lg-code-badge' }, r.watcher)
              ),
              h('p', { className: 'lg-review-item-reason' },
                r.reason || 'Pending operator review'
              ),
              h('div', { className: 'lg-review-actions' },
                h('button', {
                  className: 'lg-btn lg-btn-sm lg-btn-danger',
                  disabled: Boolean(isPending),
                  onClick: function () { handleReviewAction(r.id, 'drop'); }
                }, isPending === 'drop' ? 'Dropping...' : 'Drop'),
                h('button', {
                  className: 'lg-btn lg-btn-sm lg-btn-primary',
                  disabled: Boolean(isPending),
                  onClick: function () { handleReviewAction(r.id, 'approve'); }
                }, isPending === 'approve' ? 'Approving...' : 'Approve')
              )
            );
          })
        )
      )

    );
  }

  // Register plugin and slot with Hermes
  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === 'function') {
    window.__HERMES_PLUGINS__.register("loopguard", LoopGuardPage);
  }
  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.registerSlot === 'function') {
    window.__HERMES_PLUGINS__.registerSlot("loopguard", "cron:top", CronBanner);
  }

})();
