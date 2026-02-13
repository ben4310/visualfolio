(function () {
  "use strict";

  var MARGIN = { top: 25, right: 25, bottom: 40, left: 25 };
  var TOOLTIP_CLS = "rs-tooltip";

  function render(id, data) {
    var el = document.getElementById(id);
    if (!el || !data.dates || data.dates.length === 0) return;

    d3.selectAll("." + TOOLTIP_CLS).remove();
    el.innerHTML = "";

    var W = el.clientWidth;
    var H = el.clientHeight;
    var iW = W - MARGIN.left - MARGIN.right;
    var iH = H - MARGIN.top - MARGIN.bottom;
    if (iW <= 0 || iH <= 0) return;

    // Theme-specific text color
    var isDark = document.documentElement.classList.contains("dark");
    var mainColor = isDark ? "rgb(255,255,255)" : "#6b6b6b";

    var dates = data.dates.map(function (d) { return new Date(d + "T00:00:00"); });
    var n = dates.length;
    var series = data.series;

    // Build stack-friendly matrix: [{date, Series1: val, Series2: val, ...}, ...]
    var keys = series.map(function (s) { return s.name; });
    var colorMap = {};
    series.forEach(function (s) { colorMap[s.name] = s.color; });

    var rows = dates.map(function (_, j) {
      var row = { date: dates[j] };
      series.forEach(function (s) { row[s.name] = s.values[j]; });
      return row;
    });

    // Scales
    var x = d3.scaleTime().domain(d3.extent(dates)).range([0, iW]);
    var y = d3.scaleLinear().domain([0, 100]).range([iH, 0]);

    var stack = d3.stack().keys(keys).offset(d3.stackOffsetNone);
    var stacked = stack(rows);

    var area = d3.area()
      .x(function (d) { return x(d.data.date); })
      .y0(function (d) { return y(d[0]); })
      .y1(function (d) { return y(d[1]); })
      .curve(d3.curveStepAfter);

    // SVG
    var svg = d3.select(el).append("svg")
      .attr("width", W).attr("height", H).style("display", "block");

    svg.append("defs").append("clipPath").attr("id", "rs-clip")
      .append("rect").attr("width", iW).attr("height", iH);

    var g = svg.append("g")
      .attr("transform", "translate(" + MARGIN.left + "," + MARGIN.top + ")");

    var clipped = g.append("g").attr("clip-path", "url(#rs-clip)");

    // Gridlines at 20, 40, 60, 80%
    [20, 40, 60, 80].forEach(function (v) {
      clipped.append("line")
        .attr("x1", 0).attr("x2", iW)
        .attr("y1", y(v)).attr("y2", y(v))
        .attr("stroke", "var(--border-color)")
        .attr("stroke-width", 1)
        .attr("shape-rendering", "crispEdges");
    });

    // Stacked areas
    stacked.forEach(function (layer) {
      clipped.append("path")
        .attr("d", area(layer))
        .attr("fill", colorMap[layer.key]);
    });

    // X-axis (inside ticks)
    var xAx = g.append("g").attr("transform", "translate(0," + iH + ")");
    xAx.call(
      d3.axisBottom(x)
        .tickSizeInner(-10)
        .tickSizeOuter(0)
        .tickPadding(8)
        .tickFormat(d3.timeFormat("%b %Y"))
    );
    xAx.select(".domain").remove();
    xAx.selectAll("text")
      .attr("fill", mainColor).style("font-size", "12px");
    xAx.selectAll(".tick line")
      .attr("stroke", "var(--border-color)").attr("shape-rendering", "crispEdges");

    // Hover
    var hoverLine = g.append("line")
      .attr("y1", 0).attr("y2", iH)
      .attr("stroke", "var(--third-text-color)")
      .attr("stroke-width", 1).attr("stroke-dasharray", "4 3")
      .attr("pointer-events", "none").style("display", "none");

    var tip = d3.select("body").append("div").attr("class", TOOLTIP_CLS)
      .style("position", "fixed").style("display", "none")
      .style("background", "var(--second-bg-color)")
      .style("border", "1px solid var(--border-color)")
      .style("border-radius", "8px").style("padding", "6px 10px")
      .style("font-size", "12px").style("color", mainColor)
      .style("pointer-events", "none").style("white-space", "nowrap")
      .style("z-index", "9999").style("box-shadow", "0 2px 6px rgba(0,0,0,.08)");

    var bisect = d3.bisector(function (d) { return d; }).left;

    g.append("rect").attr("width", iW).attr("height", iH)
      .attr("fill", "transparent").style("cursor", "crosshair")
      .on("pointermove", function (event) {
        var mx = d3.pointer(event, this)[0];
        var hovered = x.invert(mx);
        var idx = bisect(dates, hovered);
        if (idx > 0 && idx < n) {
          idx = (hovered - dates[idx - 1] < dates[idx] - hovered) ? idx - 1 : idx;
        }
        idx = Math.max(0, Math.min(idx, n - 1));
        var sx = x(dates[idx]);
        hoverLine.attr("x1", sx).attr("x2", sx).style("display", null);

        var hoverDate = dates[idx];
        var dateStr = hoverDate.toLocaleDateString("en-US", {
          year: "numeric",
          month: "short",
          day: "numeric",
        });

        // Sort series in reverse order (top-to-bottom, matching visual stacking)
        var sortedSeries = series.slice().reverse();
        var lines = sortedSeries.map(function (s) {
          var v = s.values[idx];
          return "<span style='color:" + s.color + "'>\u25CF</span> " +
            s.name + ": " + v.toFixed(2) + "%";
        }).join("<br>");
        tip.html(dateStr + "<br>" + lines).style("display", "block");

        var tipW = tip.node().offsetWidth;
        var left = event.clientX + 14;
        if (left + tipW > window.innerWidth - 12) left = event.clientX - tipW - 14;
        tip.style("left", left + "px").style("top", (event.clientY - 30) + "px");
      })
      .on("pointerleave", function () {
        hoverLine.style("display", "none");
        tip.style("display", "none");
      });
  }

  var timer;
  var resizeListenerAdded = false;
  window.initRelativeStreamgraph = function (id, data) {
    render(id, data);
    if (!resizeListenerAdded) {
      resizeListenerAdded = true;
      window.addEventListener("resize", function () {
        clearTimeout(timer);
        timer = setTimeout(function () { render(id, data); }, 200);
      });
    }
  };
})();
