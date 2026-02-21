(function () {
  "use strict";

  var MARGIN = { top: 25, right: 25, bottom: 95, left: 25 };

  /* ── helpers ────────────────────────────────────────────── */

  function fmtValue(v) {
    return v.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function displayDailyDetails(dateStr, totalValue, baseCurrency, transactions, trades) {
    var hoverTotalEl = document.getElementById("hover-total-value");
    var transactionsEl = document.getElementById("transactions-list");
    var tradesEl = document.getElementById("trades-list");
    if (!hoverTotalEl || !transactionsEl || !tradesEl) return;

    if (hoverTotalEl) {
      hoverTotalEl.innerHTML =
        "<div class='text-(--first-text-color) text-2xl font-normal'>" +
        fmtValue(totalValue) + " " + baseCurrency +
        "</div>";
    }

    var MAX_ITEMS = 6;

    // Build transactions list
    var transactionsHtml = "";
    if (transactions && transactions.length > 0) {
      // Sort transactions by descending absolute value and limit to 6
      var sortedTransactions = transactions.slice().sort(function(a, b) {
        return Math.abs(b.value) - Math.abs(a.value);
      }).slice(0, MAX_ITEMS);
      
      transactionsHtml = "<div class='space-y-3'>";
      sortedTransactions.forEach(function(txn) {
        transactionsHtml += 
          "<div class='flex justify-between items-start gap-3'>" +
          "<div class='flex-1 min-w-0'>" +
          "<div class='text-sm text-(--first-text-color) truncate'>" + txn.entity + "</div>" +
          "</div>" +
          "<div class='text-sm text-right flex-shrink-0'>" +
          "<div class='text-sm " + (txn.amount > 0 ? "text-green-600" : "text-(--first-text-color)") + " font-medium'>" +
          (txn.amount > 0 ? "+" : "") + fmtValue(txn.amount) + " " + txn.asset +
          "</div>" +
          "</div>" +
          "</div>";
      });
      transactionsHtml += "</div>";
    } else {
      transactionsHtml = "<div class='text-sm text-(--mid-bg-color)'>—</div>";
    }
    
    // Build trades list
    var tradesHtml = "";
    if (trades && trades.length > 0) {
      // Sort trades by descending average value and limit to 6
      var sortedTrades = trades.slice().sort(function(a, b) {
        var avgA = (a.from_value + a.to_value) / 2;
        var avgB = (b.from_value + b.to_value) / 2;
        return avgB - avgA;
      }).slice(0, MAX_ITEMS);
      
      tradesHtml = "<div class='space-y-3'>";
      sortedTrades.forEach(function(trade) {
        tradesHtml += 
          "<div class='flex justify-between items-start gap-4'>" +
          "<div class='flex-1 min-w-0'>" +
          "<div class='text-sm text-(--first-text-color)'>" +
          fmtValue(trade.from_amount) + " " + trade.from_asset + " → " +
          fmtValue(trade.to_amount) + " " + trade.to_asset +
          "</div>" +
          "</div>" +
          "<div class='text-right flex-shrink-0'>" +
          "<div class='text-sm text-(--third-text-color)'>" +
          "~" + fmtValue((trade.from_value + trade.to_value) / 2) + " " + baseCurrency +
          "</div>" +
          "</div>" +
          "</div>";
      });
      tradesHtml += "</div>";
    } else {
      tradesHtml = "<div class='text-sm text-(--mid-bg-color)'>—</div>";
    }
    
    transactionsEl.innerHTML = transactionsHtml;
    tradesEl.innerHTML = tradesHtml;
  }

  function clearDailyDetails() {
    var hoverTotalEl = document.getElementById("hover-total-value");
    var transactionsEl = document.getElementById("transactions-list");
    var tradesEl = document.getElementById("trades-list");

    var placeholderText = "<span class='italic text-(--mid-bg-color)'>Hover to display daily data</span>";
    
    if (hoverTotalEl) {
      hoverTotalEl.innerHTML = placeholderText;
    }
    if (transactionsEl) {
      transactionsEl.innerHTML = placeholderText;
    }
    if (tradesEl) {
      tradesEl.innerHTML = placeholderText;
    }
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
    
    // Add one extra day to ensure the last data point is visually represented with curveStepAfter
    var lastDate = dates[dates.length - 1];
    var extraDate = new Date(lastDate);
    extraDate.setDate(extraDate.getDate() + 1);
    dates.push(extraDate);
    
    var n = dates.length;
    
    // Create extended layers for rendering (without mutating original data)
    var layers = data.layers.map(function(layer) {
      return {
        name: layer.name,
        color: layer.color,
        upper: layer.upper.concat([layer.upper[layer.upper.length - 1]]),
        lower: layer.lower.concat([layer.lower[layer.lower.length - 1]])
      };
    });
    
    // Create extended values array for hover (without mutating original data)
    var values = data.values.concat([data.values[data.values.length - 1]]);

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
    var area = d3
      .area()
      .x(function (d) { return x(d.date); })
      .y0(function (d) { return y(d.lower); })
      .y1(function (d) { return y(d.upper); })
      .curve(d3.curveStepAfter);

    layers.forEach(function (layer, i) {
      var pts = dates.map(function (date, j) {
        return { date: date, upper: layer.upper[j], lower: layer.lower[j] };
      });

      clipped.append("path").attr("d", area(pts)).attr("fill", layer.color);
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

    /* ── hover crosshair ── */
    var hoverLine = g
      .append("line")
      .attr("y1", 0)
      .attr("y2", iH)
      .attr("stroke", "var(--third-text-color)")
      .attr("stroke-width", 1)
      .attr("stroke-dasharray", "4 3")
      .attr("pointer-events", "none")
      .style("display", "none");

    var hoverTooltip = g
      .append("g")
      .attr("pointer-events", "none")
      .style("display", "none");
    hoverTooltip
      .append("rect")
      .attr("x", -50)
      .attr("y", 0)
      .attr("width", 100)
      .attr("height", 18)
      .attr("rx", 4)
      .attr("fill", "var(--second-bg-color)")
      .attr("stroke", "var(--border-color)")
      .attr("stroke-width", 1);
    hoverTooltip
      .append("text")
      .attr("text-anchor", "middle")
      .attr("y", 14)
      .attr("fill", mainColor)
      .style("font-size", "11px");

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
        hoverTooltip
          .attr("transform", "translate(" + sx + ",0)")
          .select("text")
          .text(dateStr);
        hoverTooltip.style("display", null);

        // Cap the data index to the original data length (before we added the extra day)
        var dataIdx = Math.min(idx, data.dates.length - 1);
        var dateKey = data.dates[dataIdx];
        var totalValue = values[idx];

        var dayData = data.transactionsByDate[dateKey] || { transactions: [], trades: [] };
        displayDailyDetails(dateStr, totalValue, data.baseCurrency, dayData.transactions, dayData.trades);
      })
      .on("pointerleave", function () {
        hoverLine.style("display", "none");
        hoverTooltip.style("display", "none");
        clearDailyDetails();
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
