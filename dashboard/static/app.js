const numberFmt = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
const priceFmt = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });

const els = {
  errorBox: document.getElementById("errorBox"),
  updatedAt: document.getElementById("updatedAt"),
  refreshBtn: document.getElementById("refreshBtn"),
  totalAssets: document.getElementById("totalAssets"),
  coinValue: document.getElementById("coinValue"),
  totalPnl: document.getElementById("totalPnl"),
  riskExposure: document.getElementById("riskExposure"),
  botBadge: document.getElementById("botBadge"),
  positions: document.getElementById("positions"),
  tradeCards: document.getElementById("tradeCards"),
  orders: document.getElementById("orders"),
  logTrades: document.getElementById("logTrades"),
};

let chartTooltip = null;
let overviewInFlight = false;
let overviewRequestSeq = 0;

function getChartTooltip() {
  if (chartTooltip) return chartTooltip;
  chartTooltip = document.createElement("div");
  chartTooltip.className = "chart-tooltip";
  document.body.appendChild(chartTooltip);
  return chartTooltip;
}

function showChartTooltip(event, html) {
  const tooltip = getChartTooltip();
  tooltip.innerHTML = html;
  tooltip.classList.add("is-visible");

  const margin = 14;
  const rect = tooltip.getBoundingClientRect();
  let left = event.clientX + margin;
  let top = event.clientY + margin;

  // 화면 가장자리에서 툴팁이 잘리지 않도록 마우스 반대편으로 살짝 넘긴다.
  if (left + rect.width > window.innerWidth - 8) {
    left = event.clientX - rect.width - margin;
  }
  if (top + rect.height > window.innerHeight - 8) {
    top = event.clientY - rect.height - margin;
  }

  tooltip.style.transform = `translate3d(${Math.max(8, left)}px, ${Math.max(8, top)}px, 0)`;
}

function hideChartTooltip() {
  if (!chartTooltip) return;
  chartTooltip.classList.remove("is-visible");
  chartTooltip.style.transform = "translate3d(-9999px, -9999px, 0)";
}

function krw(value) {
  return `${numberFmt.format(Math.round(Number(value || 0)))}원`;
}

function pct(value) {
  const num = Number(value || 0);
  return `${num >= 0 ? "+" : ""}${num.toFixed(2)}%`;
}

function formatTradeTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 19);
  return date.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function price(value) {
  const num = Number(value || 0);
  if (num >= 1000) return `${numberFmt.format(Math.round(num))}원`;
  return `${priceFmt.format(num)}원`;
}

function signedClass(value) {
  const num = Number(value || 0);
  if (num > 0) return "text-emerald-300";
  if (num < 0) return "text-red-300";
  return "text-zinc-300";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function candleChart(points, trades = []) {
  if (!points?.length) return `<div class="h-[76px] rounded-md bg-zinc-900"></div>`;

  const candles = points
    .map((p, idx) => ({
      idx,
      label: p.time || "",
      time: Number(new Date(p.timestamp || 0)),
      open: Number(p.open || p.close || 0),
      high: Number(p.high || p.close || 0),
      low: Number(p.low || p.close || 0),
      close: Number(p.close || 0),
      volume: Number(p.volume || 0),
    }))
    .filter((p) => [p.open, p.high, p.low, p.close, p.volume].every(Number.isFinite));

  if (!candles.length) return `<div class="h-[76px] rounded-md bg-zinc-900"></div>`;

  const min = Math.min(...candles.map((p) => p.low));
  const max = Math.max(...candles.map((p) => p.high));
  const span = max - min || 1;
  const width = 240;
  const height = 92;
  const priceHeight = 68;
  const volumeTop = 72;
  const volumeHeight = 16;
  const padY = 7;
  const step = width / candles.length;
  const bodyWidth = Math.max(2.4, Math.min(6, step * 0.58));
  const volumeWidth = Math.max(1.8, Math.min(5.2, step * 0.52));
  const firstTime = candles[0]?.time;
  const lastTime = candles[candles.length - 1]?.time;
  const candleMs = candles.length > 1 ? Math.max(1, candles[1].time - candles[0].time) : 5 * 60 * 1000;
  const maxVolume = Math.max(...candles.map((p) => p.volume), 1);

  const y = (value) => priceHeight - ((value - min) / span) * (priceHeight - padY * 2) - padY;

  // 실제 OHLC 값을 사용해 꼬리와 몸통을 각각 그린다. 몸통이 너무 얇아지는 도지 캔들은 최소 높이를 보장한다.
  const candleNodes = candles
    .map((c, idx) => {
      const x = idx * step + step / 2;
      const openY = y(c.open);
      const closeY = y(c.close);
      const highY = y(c.high);
      const lowY = y(c.low);
      const bullish = c.close >= c.open;
      const color = bullish ? "#34d399" : "#fb7185";
      const bodyTop = Math.min(openY, closeY);
      const bodyHeight = Math.max(1.6, Math.abs(openY - closeY));
      const volumeH = Math.max(1, (c.volume / maxVolume) * volumeHeight);
      const volumeY = volumeTop + volumeHeight - volumeH;
      const title = [
        `${c.label} 5분봉`,
        `시가 ${price(c.open)}`,
        `고가 ${price(c.high)}`,
        `저가 ${price(c.low)}`,
        `종가 ${price(c.close)}`,
        `거래량 ${numberFmt.format(Math.round(c.volume))}`,
      ].join(" · ");
      const tooltip = [
        `<strong>${escapeHtml(c.label)} 5분봉</strong>`,
        `<div><span>시가</span><b>${price(c.open)}</b></div>`,
        `<div><span>고가</span><b>${price(c.high)}</b></div>`,
        `<div><span>저가</span><b>${price(c.low)}</b></div>`,
        `<div><span>종가</span><b>${price(c.close)}</b></div>`,
        `<div><span>거래량</span><b>${numberFmt.format(Math.round(c.volume))}</b></div>`,
      ].join("");
      return `
        <g class="candle-point" data-tooltip="${escapeHtml(tooltip)}">
          <title>${title}</title>
          <line x1="${x.toFixed(1)}" y1="${highY.toFixed(1)}" x2="${x.toFixed(1)}" y2="${lowY.toFixed(1)}" stroke="${color}" stroke-width="1" vector-effect="non-scaling-stroke"></line>
          <rect x="${(x - bodyWidth / 2).toFixed(1)}" y="${bodyTop.toFixed(1)}" width="${bodyWidth.toFixed(1)}" height="${bodyHeight.toFixed(1)}" rx="0.8" fill="${color}"></rect>
          <rect x="${(x - volumeWidth / 2).toFixed(1)}" y="${volumeY.toFixed(1)}" width="${volumeWidth.toFixed(1)}" height="${volumeH.toFixed(1)}" rx="0.5" fill="${color}" opacity="0.38"></rect>
          <rect class="candle-hit" data-tooltip="${escapeHtml(tooltip)}" x="${(x - step / 2).toFixed(1)}" y="0" width="${step.toFixed(1)}" height="${height}" fill="#000" opacity="0.001" pointer-events="all"></rect>
        </g>
      `;
    })
    .join(" ");

  const tradeNodes = trades
    .map((trade) => {
      const tradeTime = Number(new Date(trade.created_at || ""));
      if (!Number.isFinite(tradeTime) || tradeTime < firstTime - candleMs || tradeTime > lastTime + candleMs) {
        return "";
      }
      const nearest = candles.reduce((best, candle) => {
        const currentGap = Math.abs(candle.time - tradeTime);
        const bestGap = Math.abs(best.time - tradeTime);
        return currentGap < bestGap ? candle : best;
      }, candles[0]);
      if (Math.abs(nearest.time - tradeTime) > candleMs) return "";

      const x = nearest.idx * step + step / 2;
      const isBuy = trade.side === "bid";
      const markerY = isBuy ? Math.min(priceHeight - 4, y(nearest.low) + 8) : Math.max(4, y(nearest.high) - 8);
      const color = isBuy ? "#22c55e" : "#ef4444";
      const label = isBuy ? "매수" : "매도";
      const pointsText = isBuy
        ? `${x.toFixed(1)},${(markerY + 4).toFixed(1)} ${(x - 4).toFixed(1)},${(markerY - 4).toFixed(1)} ${(x + 4).toFixed(1)},${(markerY - 4).toFixed(1)}`
        : `${x.toFixed(1)},${(markerY - 4).toFixed(1)} ${(x - 4).toFixed(1)},${(markerY + 4).toFixed(1)} ${(x + 4).toFixed(1)},${(markerY + 4).toFixed(1)}`;

      return `
        <polygon points="${pointsText}" fill="${color}" stroke="#09090b" stroke-width="1.2" vector-effect="non-scaling-stroke">
          <title>${label} ${trade.created_at || ""} ${price(trade.avg_price || 0)}</title>
        </polygon>
      `;
    })
    .join(" ");

  return `
    <svg class="candle-chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <line x1="0" y1="${y(max).toFixed(1)}" x2="${width}" y2="${y(max).toFixed(1)}" stroke="#27272a" stroke-width="1" vector-effect="non-scaling-stroke"></line>
      <line x1="0" y1="${y((max + min) / 2).toFixed(1)}" x2="${width}" y2="${y((max + min) / 2).toFixed(1)}" stroke="#27272a" stroke-width="1" vector-effect="non-scaling-stroke"></line>
      <line x1="0" y1="${y(min).toFixed(1)}" x2="${width}" y2="${y(min).toFixed(1)}" stroke="#27272a" stroke-width="1" vector-effect="non-scaling-stroke"></line>
      <line x1="0" y1="${volumeTop.toFixed(1)}" x2="${width}" y2="${volumeTop.toFixed(1)}" stroke="#3f3f46" stroke-width="1" vector-effect="non-scaling-stroke"></line>
      ${candleNodes}
      ${tradeNodes}
    </svg>
  `;
}

document.addEventListener("mousemove", (event) => {
  const target = event.target instanceof Element ? event.target.closest("[data-tooltip]") : null;
  if (!target) {
    if (!(event.target instanceof Element) || !event.target.closest(".candle-chart")) {
      hideChartTooltip();
    }
    return;
  }
  const html = target.getAttribute("data-tooltip");
  if (html) showChartTooltip(event, html);
});

document.addEventListener("mouseleave", hideChartTooltip);
document.addEventListener("scroll", hideChartTooltip, true);

function renderSummary(summary) {
  els.totalAssets.textContent = krw(summary.total_assets);
  els.coinValue.textContent = krw(summary.coin_value);
  els.totalPnl.innerHTML = `<span class="${signedClass(summary.total_pnl)}">${krw(summary.total_pnl)} (${pct(summary.total_pnl_pct)})</span>`;
  els.riskExposure.textContent = `${Number(summary.risk_exposure_pct || 0).toFixed(1)}%`;
}

function renderBot(bot) {
  if (bot.is_recent) {
    els.botBadge.textContent = "봇 실행 중";
    els.botBadge.className = "badge border-emerald-500/60 text-emerald-200";
    return;
  }
  els.botBadge.textContent = bot.log_exists ? "최근 로그 없음" : "로그 없음";
  els.botBadge.className = "badge border-amber-500/60 text-amber-200";
}

function renderPositions(positions, orders) {
  els.positions.innerHTML = positions.map((pos) => {
    const positionTrades = orders.filter((order) => order.market === pos.market && !order.error);
    const pnlClass = signedClass(pos.pnl_krw);
    const state = pos.has_position ? "LONG" : "NONE";
    const stateClass = pos.has_position ? "bg-emerald-400 text-zinc-950" : "bg-zinc-800 text-zinc-300";
    const stateMarket = pos.state?.market ? `<span class="text-zinc-500">선정 ${escapeHtml(pos.state.market)}</span>` : "";
    return `
      <article class="position-card">
        <div class="mb-2 flex items-start justify-between gap-3">
          <div>
            <div class="flex items-center gap-2">
              <h3 class="font-semibold text-white">${escapeHtml(pos.market)}</h3>
              <span class="rounded px-2 py-0.5 text-xs font-bold ${stateClass}">${state}</span>
            </div>
            <p class="mt-0.5 text-xs text-zinc-500">${escapeHtml(pos.label)} · ${escapeHtml(pos.session)} ${stateMarket}</p>
          </div>
        </div>
        ${candleChart(pos.chart, positionTrades)}
        <div class="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <div class="muted-row"><span>현재가</span><span>${price(pos.current_price)}</span></div>
          <div class="muted-row"><span>평단가</span><span>${price(pos.avg_buy_price)}</span></div>
          <div class="muted-row"><span>수익률</span><span class="${pnlClass}">${pct(pos.pnl_pct)}</span></div>
          <div class="muted-row"><span>수량</span><span>${Number(pos.balance || 0).toFixed(6)} ${escapeHtml(pos.asset)}</span></div>
        </div>
      </article>
    `;
  }).join("");
}

function renderTradeCards(orders) {
  if (!orders.length) {
    els.tradeCards.innerHTML = `<div class="rounded-md bg-zinc-900 p-3 text-sm text-zinc-500">최근 매매 거래내역이 없습니다.</div>`;
    return;
  }
  els.tradeCards.innerHTML = orders.slice(0, 20).map((order) => {
    if (order.error) {
      return `<div class="rounded-md bg-zinc-900 p-3 text-sm text-red-300">${escapeHtml(order.market)}: ${escapeHtml(order.error)}</div>`;
    }
    const sideText = order.side === "bid" ? "매수" : order.side === "ask" ? "매도" : order.side;
    const sideClass = order.side === "bid" ? "text-emerald-300" : "text-red-300";
    const tradePnlMeta = order.side === "ask"
      ? `<div class="muted-row text-sm"><span>매도사유</span><span>${escapeHtml(order.sell_reason || "확인불가")}</span></div>
         <div class="muted-row text-sm"><span>수익률</span><span class="${signedClass(order.pnl_pct)}">${pct(order.pnl_pct)}</span></div>`
      : `<div class="muted-row text-sm"><span>수익률</span><span class="${signedClass(order.pnl_pct)}">${pct(order.pnl_pct)} · ${escapeHtml(order.pnl_basis || "확인불가")}</span></div>`;
    return `
    <div class="rounded-md bg-zinc-900 px-3 py-2.5">
      <div class="flex items-center justify-between gap-2">
        <strong>${escapeHtml(order.market)}</strong>
        <span class="${sideClass}">${sideText}</span>
      </div>
      <div class="mt-2 muted-row text-sm"><span>거래시간</span><span>${formatTradeTime(order.created_at)}</span></div>
      <div class="muted-row text-sm"><span>평균가</span><span>${price(order.avg_price)}</span></div>
      <div class="muted-row text-sm"><span>체결금액</span><span>${krw(order.funds)}</span></div>
      ${tradePnlMeta}
    </div>
  `;
  }).join("");
}

function renderOrders(orders) {
  if (!orders.length) {
    els.orders.innerHTML = `<tr><td colspan="6" class="py-5 text-center text-zinc-500">주문 이력이 없습니다.</td></tr>`;
    return;
  }
  els.orders.innerHTML = orders.map((order) => {
    if (order.error) {
      return `<tr><td colspan="6" class="py-3 text-red-300">${escapeHtml(order.market)}: ${escapeHtml(order.error)}</td></tr>`;
    }
    const sideText = order.side === "bid" ? "매수" : order.side === "ask" ? "매도" : order.side;
    const sideClass = order.side === "bid" ? "text-emerald-300" : "text-red-300";
    const sideDetail = order.side === "ask"
      ? `${sideText}${order.sell_reason ? ` · ${escapeHtml(order.sell_reason)}` : ""}${order.pnl_pct !== undefined ? ` · ${pct(order.pnl_pct)}` : ""}`
      : `${sideText}${order.pnl_pct !== undefined ? ` · ${pct(order.pnl_pct)}` : ""}${order.pnl_basis ? ` · ${escapeHtml(order.pnl_basis)}` : ""}`;
    return `
      <tr>
        <td class="py-3 pr-4 text-zinc-400">${formatTradeTime(order.created_at)}</td>
        <td class="py-3 pr-4 font-medium text-zinc-200">${escapeHtml(order.market)}</td>
        <td class="py-3 pr-4 ${sideClass}">${sideDetail}</td>
        <td class="py-3 pr-4 text-right">${price(order.avg_price)}</td>
        <td class="py-3 pr-4 text-right text-zinc-400">${Number(order.volume || 0).toFixed(8)}</td>
        <td class="py-3 text-right">${krw(order.funds)}</td>
      </tr>
    `;
  }).join("");
}

function renderLogTrades(rows) {
  if (!rows.length) {
    els.logTrades.innerHTML = `<div class="rounded-md bg-zinc-900 p-3 text-zinc-500">최근 체결 로그가 없습니다.</div>`;
    return;
  }
  els.logTrades.innerHTML = rows.slice(0, 8).map((row) => `
    <div class="rounded-md bg-zinc-900 px-3 py-2.5">
      <div class="flex items-center justify-between gap-2">
        <strong>${escapeHtml(row.market)}</strong>
        <span class="${row.action === "BUY" ? "text-emerald-300" : "text-red-300"}">${escapeHtml(row.action)}</span>
      </div>
      <div class="mt-1 text-xs text-zinc-500">${escapeHtml(row.time)}</div>
      <div class="mt-1 text-zinc-400">${escapeHtml(row.message).slice(0, 80)}</div>
    </div>
  `).join("");
}

async function loadOverview() {
  if (overviewInFlight) return;
  const requestSeq = ++overviewRequestSeq;
  overviewInFlight = true;
  els.errorBox.classList.add("hidden");
  els.refreshBtn.disabled = true;
  els.refreshBtn.classList.add("opacity-60");
  try {
    const res = await fetch("/api/overview?limit=30", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "대시보드 데이터를 불러오지 못했습니다.");
    if (requestSeq !== overviewRequestSeq) return;
    renderSummary(data.summary);
    renderBot(data.bot);
    renderPositions(data.positions, data.orders || []);
    renderTradeCards(data.orders || []);
    renderOrders(data.orders);
    renderLogTrades(data.log_trades);
    els.updatedAt.textContent = `업데이트 ${new Date(data.generated_at).toLocaleString("ko-KR")}`;
  } catch (error) {
    els.errorBox.textContent = error.message;
    els.errorBox.classList.remove("hidden");
  } finally {
    overviewInFlight = false;
    els.refreshBtn.disabled = false;
    els.refreshBtn.classList.remove("opacity-60");
  }
}

els.refreshBtn.addEventListener("click", loadOverview);
loadOverview();
setInterval(loadOverview, 10_000);
