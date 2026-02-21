(function () {
  "use strict";

  var MARGIN = { top: 20, right: 20, bottom: 30, left: 50 };
  var TOOLTIP_CLS = "bp-tooltip";

  function fmtValue(v) {
    return v.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function render(id, data) {
    var el = document.getElementById(id);
    if (!el || !data.records || data.records.length === 0) return;

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

    var records = data.records;
    var baseCurrency = data.baseCurrency || "";

    // Derive unique months (in order) and entities
    var months = [];
    var monthSet = {};
    var entities = [];
    var entitySet = {};
    var entityColor = {};

    records.forEach(function (r) {
      if (!monthSet[r.month]) { months.push(r.month); monthSet[r.month] = true; }
      if (!entitySet[r.entity]) { entities.push(r.entity); entitySet[r.entity] = true; }
      entityColor[r.entity] = r.color;
    });

    // Build lookup: month -> entity -> amount
    var lookup = {};
    records.forEach(function (r) {
      if (!lookup[r.month]) lookup[r.month] = {};
      lookup[r.month][r.entity] = r.amount;
    });

    var maxVal = d3.max(records, function (r) { return r.amount; }) || 1;

    // Scales
    var x0 = d3.scaleBand().domain(months).range([0, iW]).paddingInner(0.4).paddingOuter(0.1);
    var x1 = d3.scaleBand().domain(entities).range([0, x0.bandwidth()]).padding(0.15);
    var y = d3.scaleLinear().domain([0, maxVal * 1.1]).nice().range([iH, 0]);

    // SVG
    var svg = d3.select(el).append("svg")
      .attr("width", W).attr("height", H).style("display", "block");

    var g = svg.append("g")
      .attr("transform", "translate(" + MARGIN.left + "," + MARGIN.top + ")");

    // Y gridlines
    g.append("g").selectAll("line").data(y.ticks(6)).enter().append("line")
      .attr("x1", 0).attr("x2", iW)
      .attr("y1", function (d) { return y(d); })
      .attr("y2", function (d) { return y(d); })
      .attr("stroke", "var(--border-color)")
      .attr("stroke-width", 1)
      .attr("shape-rendering", "crispEdges");

    // Bars
    var maxBarWidth = 6; // Match w-3 h-3 dot size (12px)

    months.forEach(function (month) {
      var mg = g.append("g")
        .attr("transform", "translate(" + x0(month) + ",0)");

      entities.forEach(function (entity) {
        var val = (lookup[month] && lookup[month][entity]) || 0;
        if (val <= 0) return;

        var allocatedWidth = x1.bandwidth();
        var bw = Math.min(allocatedWidth, maxBarWidth);
        var bx = x1(entity) + (allocatedWidth - bw) / 2; // Center bar within allocated space
        var radius = bw / 2; // Top corner radius (half bar width)
        var by = y(val);
        var bh = Math.max(iH - by, radius); // Minimum height equals radius (semicircle)

        // Path with rounded top corners only
        var path = [
          "M", bx, by + bh, // Bottom-left (sharp)
          "L", bx + bw, by + bh, // Bottom-right (sharp)
          "L", bx + bw, by + radius, // To top-right before rounding
          "A", radius, radius, 0, 0, 0, bx + bw - radius, by, // Top-right arc
          "L", bx + radius, by, // To top-left before rounding
          "A", radius, radius, 0, 0, 0, bx, by + radius, // Top-left arc
          "Z" // Close path
        ].join(" ");

        mg.append("path")
          .attr("d", path)
          .attr("fill", entityColor[entity])
          .attr("class", "bar-rect")
          .attr("data-month", month)
          .attr("data-entity", entity)
          .attr("data-value", val);
      });
    });

    // X-axis
    var xAx = g.append("g").attr("transform", "translate(0," + iH + ")");
    xAx.call(d3.axisBottom(x0).tickSizeOuter(0));
    xAx.select(".domain").remove();
    xAx.selectAll(".tick line").remove();
    xAx.selectAll("text")
      .attr("fill", mainColor).style("font-size", "12px");

    // Y-axis (hidden labels, just grid)
    var yAx = g.append("g");
    yAx.call(d3.axisLeft(y).ticks(6).tickSize(0).tickPadding(6));
    yAx.select(".domain").remove();
    yAx.selectAll("text")
      .attr("fill", mainColor).style("font-size", "11px");

    // Tooltip
    var tip = d3.select("body").append("div").attr("class", TOOLTIP_CLS)
      .style("position", "fixed").style("display", "none")
      .style("background", "var(--second-bg-color)")
      .style("border", "1px solid var(--border-color)")
      .style("border-radius", "8px").style("padding", "6px 10px")
      .style("font-size", "12px").style("color", mainColor)
      .style("pointer-events", "none").style("white-space", "nowrap")
      .style("z-index", "9999").style("box-shadow", "0 2px 6px rgba(0,0,0,.08)");

    g.selectAll(".bar-rect")
      .on("pointermove", function (event) {
        var rect = d3.select(this);
        var val = +rect.attr("data-value");
        tip.html(fmtValue(val) + " " + baseCurrency).style("display", "block");
        var tipW = tip.node().offsetWidth;
        var left = event.clientX + 14;
        if (left + tipW > window.innerWidth - 12) left = event.clientX - tipW - 14;
        tip.style("left", left + "px").style("top", (event.clientY - 30) + "px");
      })
      .on("pointerleave", function () {
        tip.style("display", "none");
      });
  }

  var timer;
  var resizeListenerAdded = false;
  window.initBarplot = function (id, data) {
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
