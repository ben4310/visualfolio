(function () {
  "use strict";

  var MARGIN = { top: 25, right: 25, bottom: 95, left: 25 };
  var TOOLTIP_CLS = "sg-tooltip";

  /* ── helpers ────────────────────────────────────────────── */

  function fmtValue(v) {
    return v.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  /**
   * Build a closed SVG path from two edges that may use different
   * curve interpolations (e.g. stepAfter top, linear bottom).
   */
  function compositeArea(pts, xScale, yScale, topCurve, botCurve) {
    var topLine = d3
      .line()
      .x(function (d) { return xScale(d.date); })
      .y(function (d) { return yScale(d.upper); })
      .curve(topCurve);

    var botLine = d3
      .line()
      .x(function (d) { return xScale(d.date); })
      .y(function (d) { return yScale(d.lower); })
      .curve(botCurve);

    var top = topLine(pts);
    var bot = botLine(pts.slice().reverse());
    return top + "L" + bot.substring(1) + "Z";
  }

  /* ── main render ───────────────────────────────────────── */

  function render(id, data) {
    var el = document.getElementById(id);
    if (!el || !data.dates || data.dates.length === 0) return;

    // Teardown
    d3.selectAll("." + TOOLTIP_CLS).remove();
    el.innerHTML = "";

    // Use container size minus 1px buffer to prevent overflow scrollbars
    var W = el.clientWidth - 1;
    var H = el.clientHeight - 1;
    var iW = W - MARGIN.left - MARGIN.right;
    var iH = H - MARGIN.top - MARGIN.bottom;
    if (iW <= 0 || iH <= 0) return;

    // Theme-specific text color
    var isDark = document.documentElement.classList.contains("dark");
    var mainColor = isDark ? "rgb(255,255,255)" : "#6b6b6b";

    var dates = data.dates.map(function (d) { return new Date(d + "T00:00:00"); });
    var n = dates.length;
    var layers = data.layers;

    // Scales
    var x = d3.scaleTime().domain(d3.extent(dates)).range([0, iW]);
    var y = d3.scaleLinear().domain([data.yMin, data.yMax]).range([iH, 0]);

    // SVG root
    var svg = d3
      .select(el)
      .append("svg")
      .attr("width", W)
      .attr("height", H)
      .style("display", "block");

    svg
      .append("defs")
      .append("clipPath")
      .attr("id", "sg-clip")
      .append("rect")
      .attr("width", iW)
      .attr("height", iH);

    var g = svg
      .append("g")
      .attr("transform", "translate(" + MARGIN.left + "," + MARGIN.top + ")");

    var clipped = g.append("g").attr("clip-path", "url(#sg-clip)");

    /* ── gridlines ── */
    // Filter out the bottom gridline that aligns with the x-axis
    var bottomY = data.yMin;
    data.yAxisLines.forEach(function (yv) {
      // Skip the gridline at the bottom (x-axis level)
      if (Math.abs(yv - bottomY) < 0.0001) return;
      clipped
        .append("line")
        .attr("x1", 0)
        .attr("x2", iW)
        .attr("y1", y(yv))
        .attr("y2", y(yv))
        .attr("stroke", "var(--border-color)")
        .attr("stroke-width", 1)
        .attr("shape-rendering", "crispEdges");
    });

    /* ── stacked areas ── */
    layers.forEach(function (layer, i) {
      var pts = dates.map(function (date, j) {
        return { date: date, upper: layer.upper[j], lower: layer.lower[j] };
      });

      var pathD;
      if (i === 0) {
        // First layer: step-after upper edge (transactions), linear lower edge
        pathD = compositeArea(pts, x, y, d3.curveStepAfter, d3.curveLinear);
      } else {
        pathD = d3
          .area()
          .x(function (d) { return x(d.date); })
          .y0(function (d) { return y(d.lower); })
          .y1(function (d) { return y(d.upper); })
          .curve(d3.curveLinear)(pts);
      }

      clipped.append("path").attr("d", pathD).attr("fill", layer.color);
    });

    /* ── ruler ("I" indicator) ── */
    var yal = data.yAxisLines;
    if (yal.length >= 3) {
      var span = dates[n - 1].getTime() - dates[0].getTime();
      var rX = x(new Date(dates[0].getTime() + 0.035 * span));
      var aX = x(new Date(dates[0].getTime() + 0.02 * span));
      var dashWidth = 4;
      var rT = y(yal[yal.length - 2]);
      var rB = y(yal[yal.length - 3]);

      var ruler = g.append("g");
      [
        [rX, rX, rT, rB],
        [rX, rX + dashWidth * 2, rT, rT],
        [rX, rX + dashWidth * 2, rB, rB],
      ].forEach(function (c) {
        ruler
          .append("line")
          .attr("x1", c[0])
          .attr("x2", c[1])
          .attr("y1", c[2])
          .attr("y2", c[3])
          .attr("stroke", mainColor)
          .attr("stroke-width", 2);
      });

      ruler
        .append("text")
        .attr("x", aX)
        .attr("y", (rT + rB) / 2)
        .attr("text-anchor", "end")
        .attr("dominant-baseline", "middle")
        .attr("fill", mainColor)
        .style("font-size", "12px")
        .text(data.rulerLabel);
    }

    /* ── x-axis (date ticks, inside) ── */
    var xAx = g
      .append("g")
      .attr("transform", "translate(0," + iH + ")");

    xAx.call(
      d3.axisBottom(x)
        .tickSizeInner(-10)
        .tickSizeOuter(0)
        .tickPadding(8)
        .tickFormat(d3.timeFormat("%b %Y"))
    );
    xAx.select(".domain").remove();
    xAx
      .selectAll("text")
      .attr("fill", mainColor)
      .style("font-size", "12px");
    xAx
      .selectAll(".tick line")
      .attr("stroke", "var(--border-color)")
      .attr("shape-rendering", "crispEdges");

    /* ── hover crosshair + tooltip ── */
    var hoverLine = g
      .append("line")
      .attr("y1", 0)
      .attr("y2", iH)
      .attr("stroke", "var(--third-text-color)")
      .attr("stroke-width", 1)
      .attr("stroke-dasharray", "4 3")
      .attr("pointer-events", "none")
      .style("display", "none");

    var tip = d3
      .select("body")
      .append("div")
      .attr("class", TOOLTIP_CLS)
      .style("position", "fixed")
      .style("display", "none")
      .style("background", "var(--second-bg-color)")
      .style("border", "1px solid var(--border-color)")
      .style("border-radius", "8px")
      .style("padding", "6px 10px")
      .style("font-size", "12px")
      .style("color", mainColor)
      .style("pointer-events", "none")
      .style("white-space", "nowrap")
      .style("z-index", "9999")
      .style("box-shadow", "0 2px 6px rgba(0,0,0,.08)");

    var bisect = d3.bisector(function (d) { return d; }).left;

    g.append("rect")
      .attr("width", iW)
      .attr("height", iH)
      .attr("fill", "transparent")
      .style("cursor", "crosshair")
      .on("pointermove", function (event) {
        var mx = d3.pointer(event, this)[0];
        var hovered = x.invert(mx);
        var idx = bisect(dates, hovered);
        if (idx > 0 && idx < n) {
          idx =
            hovered - dates[idx - 1] < dates[idx] - hovered ? idx - 1 : idx;
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

        tip
          .html(
            dateStr + "<br>" +
            "Total asset value: " +
              fmtValue(data.values[idx]) +
              " " +
              data.baseCurrency
          )
          .style("display", "block");

        var tipW = tip.node().offsetWidth;
        var left = event.clientX + 14;
        if (left + tipW > window.innerWidth - 12) {
          left = event.clientX - tipW - 14;
        }
        tip
          .style("left", left + "px")
          .style("top", event.clientY - 30 + "px");
      })
      .on("pointerleave", function () {
        hoverLine.style("display", "none");
        tip.style("display", "none");
      });

    /* ── legend ── */
    var legend = g
      .append("g")
      .attr("transform", "translate(0," + (iH + 60) + ")");

    var lx = 0;
    layers.forEach(function (layer) {
      var item = legend.append("g").attr("transform", "translate(" + lx + ",0)");

      item
        .append("rect")
        .attr("width", 10)
        .attr("height", 10)
        .attr("rx", 2)
        .attr("fill", layer.color);

      var txt = item
        .append("text")
        .attr("x", 14)
        .attr("y", 9)
        .attr("fill", mainColor)
        .style("font-size", "12px")
        .text(layer.name);

      lx += 14 + txt.node().getComputedTextLength() + 18;
    });
  }

  /* ── public init with debounced resize ── */
  var timer;
  var resizeListenerAdded = false;
  window.initStreamgraph = function (containerId, data) {
    render(containerId, data);
    if (!resizeListenerAdded) {
      resizeListenerAdded = true;
      window.addEventListener("resize", function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          render(containerId, data);
        }, 200);
      });
    }
  };
})();
