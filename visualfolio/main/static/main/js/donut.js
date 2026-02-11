(function () {
  "use strict";

  var TOOLTIP_CLS = "donut-tooltip";

  function fmtValue(v) {
    return v.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function fmtPercent(v, total) {
    return ((v / total) * 100).toFixed(2) + "%";
  }

  /**
   * Render a donut chart.
   *
   * @param {string} id          Container element id
   * @param {object} data        Chart data:
   *   - slices:       [{label, value, color}, ...]
   *   - baseCurrency: string  (e.g. "€")
   *   - hole:         number  (0-1, default 0.6)
   *   - centerText:   string | null  (shown in the donut centre)
   *   - layout:       "labels-outside" | "legend-right"  (default "legend-right")
   */
  function render(id, data) {
    var el = document.getElementById(id);
    if (!el || !data.slices || data.slices.length === 0) return;

    d3.selectAll("." + TOOLTIP_CLS + "[data-owner='" + id + "']").remove();
    el.innerHTML = "";

    var W = el.clientWidth;
    var H = el.clientHeight;
    if (W <= 0 || H <= 0) return;

    // Theme-specific text color
    var isDark = document.documentElement.classList.contains("dark");
    var mainColor = isDark ? "rgb(255,255,255)" : "#6b6b6b";

    var layout = data.layout || "legend-right";
    var hole = data.hole != null ? data.hole : 0.6;
    var slices = data.slices;
    var total = d3.sum(slices, function (d) { return d.value; });
    var baseCurrency = data.baseCurrency || "";

    // Determine pie area vs legend area
    var pieAreaW, legendAreaW, pieCX, pieCY, radius;

    if (layout === "legend-right") {
      // Center donut in full container, reduce size by 20%
      var baseRadius = Math.min(W, H) / 2 - 10;
      radius = baseRadius * 0.8; // 20% smaller
      pieCX = W / 2; // Center horizontally in full container
      pieCY = H / 2;
    } else {
      // labels-outside: the pie takes the full area
      pieAreaW = W;
      legendAreaW = 0;
      radius = Math.min(W, H) / 2 - 75; // room for labels + extra spacing (~10% smaller)
      pieCX = W / 2;
      pieCY = H / 2;
    }

    var innerRadius = radius * hole;

    var svg = d3.select(el).append("svg")
      .attr("width", W).attr("height", H).style("display", "block");

    var pieG = svg.append("g")
      .attr("transform", "translate(" + pieCX + "," + pieCY + ")");

    // Pie generator
    var pie = d3.pie()
      .value(function (d) { return d.value; })
      .sort(null);

    var arc = d3.arc().innerRadius(innerRadius).outerRadius(radius);

    var arcs = pie(slices);

    // Draw slices
    pieG.selectAll("path").data(arcs).enter().append("path")
      .attr("d", arc)
      .attr("fill", function (d) { return d.data.color; })
      .attr("stroke", "var(--second-bg-color)")
      .attr("stroke-width", 1);

    // Center text
    if (data.centerText) {
      pieG.append("text")
        .attr("text-anchor", "middle")
        .attr("dominant-baseline", "middle")
        .attr("fill", mainColor)
        .style("font-size", "16px")
        .text(data.centerText);
    }

    // Labels-outside mode: polylines + text
    if (layout === "labels-outside") {
      var outerArc = d3.arc().innerRadius(radius * 1.05).outerRadius(radius * 1.05);

      pieG.selectAll("polyline").data(arcs).enter().append("polyline")
        .attr("points", function (d) {
          var pos = outerArc.centroid(d);
          var mid = outerArc.centroid(d);
          var out = outerArc.centroid(d);
          out[0] = radius * 1.18 * (midAngle(d) < Math.PI ? 1 : -1);
          return [arc.centroid(d), mid, out];
        })
        .attr("fill", "none")
        .attr("stroke", mainColor)
        .attr("stroke-width", 1);

      pieG.selectAll(".donut-label").data(arcs).enter().append("text")
        .attr("class", "donut-label")
        .attr("transform", function (d) {
          var pos = outerArc.centroid(d);
          pos[0] = radius * 1.22 * (midAngle(d) < Math.PI ? 1 : -1);
          return "translate(" + pos + ")";
        })
        .attr("text-anchor", function (d) {
          return midAngle(d) < Math.PI ? "start" : "end";
        })
        .attr("dominant-baseline", "middle")
        .attr("fill", mainColor)
        .style("font-size", "11px")
        .text(function (d) { return d.data.label; });
    }

    // Legend-right mode: vertical legend
    if (layout === "legend-right") {
      // Position legend to the right of centered donut with increased spacing
      var legendX = pieCX + radius + 20; // Center + radius + spacing
      // Align legend top with donut top
      var legendY = pieCY - radius;
      var legendG = svg.append("g")
        .attr("transform", "translate(" + legendX + "," + legendY + ")");

      var lineH = 22;
      slices.forEach(function (s, i) {
        var row = legendG.append("g")
          .attr("transform", "translate(0," + (i * lineH) + ")");
        row.append("rect")
          .attr("width", 10).attr("height", 10).attr("rx", 2)
          .attr("fill", s.color);
        row.append("text")
          .attr("x", 14).attr("y", 9)
          .attr("fill", mainColor)
          .style("font-size", "12px")
          .text(s.label);
      });
    }

    // Tooltip
    var tip = d3.select("body").append("div")
      .attr("class", TOOLTIP_CLS)
      .attr("data-owner", id)
      .style("position", "fixed").style("display", "none")
      .style("background", "var(--second-bg-color)")
      .style("border", "1px solid var(--border-color)")
      .style("border-radius", "8px").style("padding", "6px 10px")
      .style("font-size", "12px").style("color", mainColor)
      .style("pointer-events", "none").style("white-space", "nowrap")
      .style("z-index", "9999").style("box-shadow", "0 2px 6px rgba(0,0,0,.08)");

    pieG.selectAll("path")
      .on("pointermove", function (event, d) {
        tip.html(
          d.data.label + "<br>" +
          fmtValue(d.data.value) + " " + baseCurrency + "<br>" +
          fmtPercent(d.data.value, total)
        ).style("display", "block");
        var tipW = tip.node().offsetWidth;
        var left = event.clientX + 14;
        if (left + tipW > window.innerWidth - 12) left = event.clientX - tipW - 14;
        tip.style("left", left + "px").style("top", (event.clientY - 10) + "px");
      })
      .on("pointerleave", function () {
        tip.style("display", "none");
      });
  }

  function midAngle(d) {
    return d.startAngle + (d.endAngle - d.startAngle) / 2;
  }

  var timers = {};
  var resizeListenersAdded = {};
  window.initDonut = function (id, data) {
    render(id, data);
    if (!resizeListenersAdded[id]) {
      resizeListenersAdded[id] = true;
      window.addEventListener("resize", function () {
        clearTimeout(timers[id]);
        timers[id] = setTimeout(function () { render(id, data); }, 200);
      });
    }
  };
})();
