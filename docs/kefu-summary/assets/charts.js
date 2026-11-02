/* 图表初始化：颜色取自 CSS 变量，窄屏自动切换紧凑布局 */
(function () {
  'use strict';

  if (typeof echarts === 'undefined') return;

  var css = getComputedStyle(document.documentElement);
  var C = {
    bg2: css.getPropertyValue('--bg2').trim() || '#ffffff',
    ink: css.getPropertyValue('--ink').trim() || '#16191f',
    muted: css.getPropertyValue('--muted').trim() || '#6b7280',
    rule: css.getPropertyValue('--rule').trim() || '#e2dfd5',
    accent: css.getPropertyValue('--accent').trim() || '#17457a',
    accent2: css.getPropertyValue('--accent2').trim() || '#b4762a'
  };

  var FONT = '"Work Sans","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif';
  var MONO = '"IBM Plex Mono",monospace';

  function text(color, size, mono) {
    return {
      color: color,
      fontFamily: mono ? MONO : FONT,
      fontSize: size
    };
  }

  function axes(narrow) {
    return {
      line: { lineStyle: { color: C.rule } },
      label: text(C.muted, narrow ? 10 : 12, true),
      split: { lineStyle: { color: C.rule, type: 'dashed' } }
    };
  }

  function baseOption(option) {
    return Object.assign({}, option, {
      animation: false,
      renderer: 'svg',
      textStyle: text(C.ink, 13, false),
      tooltip: Object.assign({
        appendToBody: true,
        backgroundColor: C.bg2,
        borderColor: C.rule,
        borderWidth: 1,
        textStyle: text(C.ink, 13, false),
        extraCssText: 'box-shadow:0 4px 14px rgba(0,0,0,.08);border-radius:6px;'
      }, option.tooltip || {})
    });
  }

  /* ---------- 图 2：语料分布 ---------- */
  function corpusOption(narrow) {
    var a = axes(narrow);
    return baseOption({
      grid: { left: narrow ? 98 : 168, right: narrow ? 42 : 70, top: 16, bottom: 28 },
      tooltip: {
        trigger: 'item',
        formatter: function (p) {
          return p.name + '：<b>' + p.value + '</b> 条（占比 ' +
            (p.value / 141 * 100).toFixed(1) + '%）';
        }
      },
      xAxis: Object.assign({}, a, {
        type: 'value',
        max: 90,
        axisLine: a.line,
        axisTick: { show: false },
        axisLabel: Object.assign(a.label, { formatter: '{value}' }),
        splitLine: a.split
      }),
      yAxis: Object.assign({}, a, {
        type: 'category',
        data: ['售后 · aftersales', '支付 · payment', '物流 · logistics', '产品咨询 · product'],
        axisLabel: Object.assign(text(C.ink, narrow ? 9.5 : 13, false), {
          width: narrow ? 90 : 158,
          overflow: 'none'
        }),
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false }
      }),
      series: [{
        type: 'bar',
        barWidth: narrow ? 18 : 22,
        data: [
          { value: 18, itemStyle: { color: C.accent } },
          { value: 22, itemStyle: { color: C.accent } },
          { value: 21, itemStyle: { color: C.accent } },
          { value: 80, itemStyle: { color: C.accent2 } }
        ],
        itemStyle: { borderRadius: [0, 3, 3, 0] },
        label: {
          show: true,
          position: 'right',
          distance: 6,
          color: C.ink,
          fontFamily: MONO,
          fontSize: narrow ? 10 : 12,
          formatter: '{c}'
        }
      }]
    });
  }

  /* ---------- 图 3：dev vs test ---------- */
  function evalOption(narrow) {
    var a = axes(narrow);
    return baseOption({
      color: [C.muted, C.accent],
      legend: {
        top: 0,
        right: 0,
        icon: 'rect',
        itemWidth: 10,
        itemHeight: 10,
        textStyle: text(C.ink, narrow ? 10 : 12, false)
      },
      grid: { left: narrow ? 44 : 62, right: narrow ? 12 : 24, top: narrow ? 34 : 44, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        valueFormatter: function (v) { return v + '%'; }
      },
      xAxis: Object.assign({}, a, {
        type: 'category',
        data: ['R@1', '意图@1', 'R@3', 'R@5', 'MRR'],
        axisLine: a.line,
        axisTick: { show: false },
        axisLabel: text(C.ink, narrow ? 9.5 : 12, true),
        splitLine: { show: false }
      }),
      yAxis: Object.assign({}, a, {
        type: 'value',
        max: 104,
        min: 60,
        interval: 10,
        axisLabel: Object.assign(a.label, { formatter: '{value}%' }),
        splitLine: a.split
      }),
      series: [
        {
          name: 'dev 32 条',
          type: 'bar',
          barGap: '12%',
          barWidth: narrow ? 18 : 30,
          data: [81.2, 93.8, 96.9, 96.9, 89.1],
          itemStyle: { color: C.muted, borderRadius: [3, 3, 0, 0] },
          label: {
            show: true,
            position: 'insideTop',
            distance: 6,
            color: '#ffffff',
            fontFamily: MONO,
            fontSize: narrow ? 8 : 9,
            formatter: '{c}'
          }
        },
        {
          name: 'test 32 条',
          type: 'bar',
          barWidth: narrow ? 18 : 30,
          data: [96.9, 96.9, 100, 100, 98.4],
          itemStyle: { color: C.accent, borderRadius: [3, 3, 0, 0] },
          label: {
            show: true,
            position: 'insideTop',
            distance: 6,
            color: '#ffffff',
            fontFamily: MONO,
            fontSize: narrow ? 8 : 9,
            formatter: '{c}'
          }
        }
      ]
    });
  }

  /* ---------- 图 4：test 分场景 ---------- */
  function sceneOption(narrow) {
    var a = axes(narrow);
    return baseOption({
      color: [C.accent2, C.accent],
      legend: {
        top: 0,
        right: 0,
        icon: 'rect',
        itemWidth: 10,
        itemHeight: 10,
        textStyle: text(C.ink, narrow ? 10 : 12, false)
      },
      grid: { left: narrow ? 44 : 62, right: narrow ? 12 : 24, top: narrow ? 34 : 44, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        valueFormatter: function (v) { return v + '%'; }
      },
      xAxis: Object.assign({}, a, {
        type: 'category',
        data: ['product', 'logistics', 'payment', 'aftersales'],
        axisLine: a.line,
        axisTick: { show: false },
        axisLabel: text(C.ink, narrow ? 9 : 12, true),
        splitLine: { show: false }
      }),
      yAxis: Object.assign({}, a, {
        type: 'value',
        max: 104,
        min: 70,
        interval: 10,
        axisLabel: Object.assign(a.label, { formatter: '{value}%' }),
        splitLine: a.split
      }),
      series: [
        {
          name: 'R@1',
          type: 'bar',
          barGap: '12%',
          barWidth: narrow ? 18 : 34,
          data: [87.5, 100, 100, 100],
          itemStyle: { color: C.accent2, borderRadius: [3, 3, 0, 0] },
          label: {
            show: true,
            position: 'insideTop',
            distance: 5,
            color: '#ffffff',
            fontFamily: MONO,
            fontSize: narrow ? 8 : 9,
            formatter: '{c}%'
          }
        },
        {
          name: '意图@1',
          type: 'bar',
          barWidth: narrow ? 18 : 34,
          data: [87.5, 100, 100, 100],
          itemStyle: { color: C.accent, borderRadius: [3, 3, 0, 0] },
          label: {
            show: true,
            position: 'insideTop',
            distance: 5,
            color: '#ffffff',
            fontFamily: MONO,
            fontSize: narrow ? 8 : 9,
            formatter: '{c}%'
          }
        }
      ]
    });
  }

  /* ---------- 挂载与响应式重建 ---------- */
  var charts = [];
  var narrowNow = window.innerWidth < 640;

  function mount(id, factory) {
    var el = document.getElementById(id);
    if (!el) return;
    var chart = echarts.init(el, null, { renderer: 'svg' });
    chart.setOption(factory(narrowNow));
    charts.push({ chart: chart, factory: factory });
  }

  mount('chart-corpus', corpusOption);
  mount('chart-eval', evalOption);
  mount('chart-scene', sceneOption);

  var timer = null;
  window.addEventListener('resize', function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var narrow = window.innerWidth < 640;
      charts.forEach(function (item) {
        if (narrow !== narrowNow) {
          item.chart.setOption(item.factory(narrow), true);
        }
        item.chart.resize();
      });
      narrowNow = narrow;
    }, 150);
  });
})();
