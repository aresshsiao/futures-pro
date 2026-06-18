import { useState, useEffect, useRef, useCallback, useMemo } from "react";

// ─── 語音提示 ────────────────────────────────────────────────────────
function speakVolumeAlert(text) {
  if (!("speechSynthesis" in window)) return;
  // 用瀏覽器內建的語音佇列依序播放（同一根棒最多 400/1500 兩則，不會堆積太多）
  const utter = new SpeechSynthesisUtterance(text);
  utter.lang = "zh-TW";
  utter.rate = 1.1;
  window.speechSynthesis.speak(utter);
}

// ─── Mock Data ───────────────────────────────────────────────────────
const generateKlineData = (count = 120) => {
  const data = [];
  let price = 17500;
  const now = Date.now();
  for (let i = count; i >= 0; i--) {
    const open = price + (Math.random() - 0.5) * 80;
    const close = open + (Math.random() - 0.5) * 120;
    const high = Math.max(open, close) + Math.random() * 60;
    const low = Math.min(open, close) - Math.random() * 60;
    const volume = Math.floor(3000 + Math.random() * 8000);
    data.push({
      time: now - i * 60000,
      date: new Date(now - i * 60000).toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit" }),
      open: +open.toFixed(0),
      close: +close.toFixed(0),
      high: +high.toFixed(0),
      low: +low.toFixed(0),
      volume,
    });
    price = close;
  }
  return data;
};

const MOCK_POSITIONS = [
  { id: 1, symbol: "TX", name: "台指期", direction: "多", qty: 2, avgPrice: 17420, currentPrice: 17535, pnl: 23000, pnlPercent: 1.32 },
  { id: 2, symbol: "MTX", name: "小台指", direction: "空", qty: 5, avgPrice: 17560, currentPrice: 17535, pnl: 6250, pnlPercent: 0.71 },
  { id: 3, symbol: "TE", name: "電子期", direction: "多", qty: 1, avgPrice: 920, currentPrice: 915, pnl: -5000, pnlPercent: -0.54 },
];

const MOCK_TRADES = [
  { id: 1, time: "13:42:18", symbol: "TX", direction: "買", price: 17530, qty: 1, status: "成交", fee: 60 },
  { id: 2, time: "13:38:05", symbol: "MTX", direction: "賣", price: 17545, qty: 2, status: "成交", fee: 24 },
  { id: 3, time: "13:25:11", symbol: "TX", direction: "買", price: 17420, qty: 1, status: "成交", fee: 60 },
  { id: 4, time: "13:10:33", symbol: "TE", direction: "買", price: 920, qty: 1, status: "成交", fee: 60 },
  { id: 5, time: "12:55:47", symbol: "MTX", direction: "賣", price: 17560, qty: 3, status: "成交", fee: 36 },
  { id: 6, time: "11:22:09", symbol: "TX", direction: "買", price: 17380, qty: 2, status: "已取消", fee: 0 },
];

const BROKER_LIST = [
  { id: "sinopac", name: "永豐金", status: "connected", type: "both" },
  { id: "fubon", name: "富邦期貨", status: "disconnected", type: "both" },
  { id: "yuanta", name: "元大期貨", status: "disconnected", type: "both" },
  { id: "masterlink", name: "元富期貨", status: "disconnected", type: "trade" },
];

// ─── Styles ──────────────────────────────────────────────────────────
const COLORS = {
  bg: "#0a0e17",
  bgPanel: "#111827",
  bgCard: "#1a2235",
  bgHover: "#1e293b",
  border: "#1e2d42",
  borderLight: "#2a3a52",
  text: "#e2e8f0",
  textDim: "#7a8ba7",
  textMuted: "#4a5568",
  accent: "#3b82f6",
  accentDim: "#1e40af",
  up: "#22c55e",
  upBg: "rgba(34,197,94,0.12)",
  down: "#ef4444",
  downBg: "rgba(239,68,68,0.12)",
  warn: "#f59e0b",
  warnBg: "rgba(245,158,11,0.12)",
  // 狀態用顏色（連線中/成功＝綠、斷線/失敗＝紅），跟漲跌色「up/down」是兩件事，
  // 不會被 CANDLE_COLOR_SCHEME（紅漲/綠漲）影響
  success: "#22c55e",
  successBg: "rgba(34,197,94,0.12)",
  danger: "#ef4444",
  dangerBg: "rgba(239,68,68,0.12)",
};

// 漲跌顏色慣例（由 config/settings.py 的 CANDLE_COLOR_SCHEME 設定，經 /api/config 傳入）
// "green-up"：漲＝綠、跌＝紅（國際慣例）；"red-up"：漲＝紅、跌＝綠（台股慣例）
function applyCandleColorScheme(scheme) {
  if (scheme === "red-up") {
    COLORS.up = "#ef4444";
    COLORS.upBg = "rgba(239,68,68,0.12)";
    COLORS.down = "#22c55e";
    COLORS.downBg = "rgba(34,197,94,0.12)";
  } else {
    COLORS.up = "#22c55e";
    COLORS.upBg = "rgba(34,197,94,0.12)";
    COLORS.down = "#ef4444";
    COLORS.downBg = "rgba(239,68,68,0.12)";
  }
}

// ─── WebSocket Hook ───────────────────────────────────────────────────
function useWebSocket(url) {
  const wsRef = useRef(null);
  const handlersRef = useRef({});
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let ws;
    let stopped = false;

    function connect() {
      if (stopped) return;
      ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);

      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          const handler = handlersRef.current[msg.type];
          if (handler) handler(msg);
        } catch (_) { }
      };

      ws.onclose = () => {
        setConnected(false);
        if (!stopped) setTimeout(connect, 3000);
      };
    }

    connect();
    return () => {
      stopped = true;
      ws?.close();
    };
  }, [url]);

  const send = useCallback((action, data = {}) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action, data }));
    }
  }, []);

  // addHandler returns a cleanup function
  const addHandler = useCallback((type, fn) => {
    handlersRef.current[type] = fn;
    return () => { delete handlersRef.current[type]; };
  }, []);

  return { send, addHandler, connected };
}

// ─── Candlestick Chart Component (K-line only) ──────────────────────
function CandlestickChart({ data, indicators = [], timeframe = "15", visibleCount, setVisibleCount, offset, setOffset, setTooltip }) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const [crosshair, setCrosshair] = useState(null);

  const dragRef = useRef(null); // { startX, startOffset }

  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));

  const visibleData = useMemo(() => {
    if (!data.length) return [];
    const end = data.length - offset;
    const start = Math.max(0, end - visibleCount);
    return data.slice(start, end);
  }, [data, offset, visibleCount]);

  // 鍵盤左右方向鍵
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "ArrowLeft") setOffset(o => clamp(o + 5, 0, data.length - 10));
      if (e.key === "ArrowRight") setOffset(o => clamp(o - 5, 0, data.length - 10));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [data.length]);

  // 滾輪縮放/平移 K 棒
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e) => {
      e.preventDefault();
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        setOffset(o => clamp(o + (e.deltaX > 0 ? 3 : -3), 0, Math.max(0, data.length - 10)));
      } else {
        setVisibleCount(c => clamp(c + (e.deltaY > 0 ? 5 : -5), 10, 1800));
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => window.removeEventListener("wheel", onWheel);
  }, [data.length, setOffset, setVisibleCount]);

  // 拖拉平移
  const handleMouseDown = (e) => {
    dragRef.current = { startX: e.clientX, startOffset: offset };
  };
  const handleDragMove = (e) => {
    if (!dragRef.current) return;
    const rect = canvasRef.current.getBoundingClientRect();
    const candleW = (rect.width - 50) / visibleCount;
    const dx = e.clientX - dragRef.current.startX;
    const delta = Math.round(-dx / candleW);
    setOffset(clamp(dragRef.current.startOffset + delta, 0, data.length - 10));
  };
  const handleMouseUp = () => { dragRef.current = null; };

  const drawChart = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    const W = canvas.width = canvas.offsetWidth * 2;
    const H = canvas.height = canvas.offsetHeight * 2;
    ctx.scale(2, 2);
    const w = W / 2;
    const h = H / 2;

    ctx.clearRect(0, 0, w, h);
    if (visibleData.length === 0) return;

    const prices = visibleData.flatMap(d => [d.high, d.low]);
    const minP = Math.min(...prices);
    const maxP = Math.max(...prices);
    const priceRange = maxP - minP || 1;
    const padding = priceRange * 0.08;
    const adjMin = minP - padding;
    const adjMax = maxP + padding;

    const candleW = (w - 50) / visibleCount;
    const bodyW = Math.max(candleW * 0.65, 2);
    const startIdx = visibleCount - visibleData.length;

    // Grid (Price & Time)
    // 目標顯示 15~25 條水平線，選最接近的步距
    const adjPriceRange = adjMax - adjMin;
    const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000].reverse();
    let step = 5;
    for (const s of steps) {
      if (adjPriceRange / s >= 12 && adjPriceRange / s <= 20) {
        step = s;
        break;
      }
    }
    const startPrice = Math.ceil(adjMin / step) * step;

    ctx.strokeStyle = "#1a2235";
    ctx.lineWidth = 0.5;
    ctx.fillStyle = COLORS.textMuted;
    ctx.font = "10px monospace";
    ctx.textAlign = "right";

    let curP = startPrice;
    while (curP <= adjMax) {
      const y = 10 + ((adjMax - curP) / (adjMax - adjMin)) * (h - 20);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w - 45, y); ctx.stroke();
      ctx.fillText(curP.toFixed(0), w - 4, y + 3);
      curP += step;
    }

    const targetTimes = new Set(["08:45", "09:00", "09:15", "09:45", "10:15", "11:00", "12:00", "12:30", "13:00", "13:30", "13:45", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00", "20:30", "21:00", "21:30", "22:00", "22:30", "23:00", "00:00", "01:00", "02:00", "03:00", "04:00", "05:00"]);
    visibleData.forEach((d, i) => {
      const dDate = new Date(d.time);
      const hhmm = dDate.getHours().toString().padStart(2, '0') + ":" + dDate.getMinutes().toString().padStart(2, '0');
      if (targetTimes.has(hhmm)) {
        const x = (startIdx + i) * candleW + candleW / 2;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      }
    });

    // Candles
    visibleData.forEach((d, i) => {
      const x = (startIdx + i) * candleW + candleW / 2;
      const isUp = d.close >= d.open;
      const color = isUp ? COLORS.up : COLORS.down;

      const oY = 10 + ((adjMax - d.open) / (adjMax - adjMin)) * (h - 20);
      const cY = 10 + ((adjMax - d.close) / (adjMax - adjMin)) * (h - 20);
      const hY = 10 + ((adjMax - d.high) / (adjMax - adjMin)) * (h - 20);
      const lY = 10 + ((adjMax - d.low) / (adjMax - adjMin)) * (h - 20);

      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, hY);
      ctx.lineTo(x, lY);
      ctx.stroke();

      const top = Math.min(oY, cY);
      const bodyH = Math.max(Math.abs(oY - cY), 1);
      ctx.fillStyle = color;
      ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);
    });

    // MA lines (from indicators) — 用全域索引回溯計算
    const globalStart = data.length - offset - visibleData.length;
    if (indicators.includes("MA_Cross")) {
      [{ period: 5, color: "#f59e0b" }, { period: 20, color: "#8b5cf6" }].forEach(({ period, color }) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < visibleData.length; i++) {
          const gi = globalStart + i;
          if (gi < period - 1) continue;
          let sum = 0;
          for (let j = gi - period + 1; j <= gi; j++) sum += data[j].close;
          const ma = sum / period;
          const x = (startIdx + i) * candleW + candleW / 2;
          const y = 10 + ((adjMax - ma) / (adjMax - adjMin)) * (h - 20);
          if (!started) { ctx.moveTo(x, y); started = true; }
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      });
    }

    // Crosshair
    if (crosshair) {
      ctx.strokeStyle = "rgba(255,255,255,0.15)";
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 0.5;
      ctx.beginPath();
      ctx.moveTo(crosshair.x, 0);
      ctx.lineTo(crosshair.x, h);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, crosshair.y);
      ctx.lineTo(w, crosshair.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }, [visibleData, indicators, crosshair, data, offset]);

  useEffect(() => { drawChart(); }, [drawChart]);

  const calcIndicatorValues = (globalIdx) => {
    const result = {};
    if (indicators.includes("MA_Cross")) {
      for (const period of [5, 20]) {
        if (globalIdx >= period - 1) {
          let sum = 0;
          for (let j = globalIdx - period + 1; j <= globalIdx; j++) sum += data[j].close;
          result[`MA${period}`] = (sum / period).toFixed(0);
        }
      }
    }
    if (indicators.includes("RSI_Signal")) {
      const period = 14;
      if (globalIdx >= period) {
        let gain = 0, loss = 0;
        for (let j = globalIdx - period + 1; j <= globalIdx; j++) {
          const diff = data[j].close - data[j - 1].close;
          if (diff > 0) gain += diff; else loss -= diff;
        }
        const rs = gain / (loss || 1);
        result["RSI"] = (100 - 100 / (1 + rs)).toFixed(1);
      }
    }
    return result;
  };

  const handleMouseMove = (e) => {
    const rect = canvasRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    setCrosshair({ x, y });

    const w = rect.width - 50;
    const candleW = w / visibleCount;
    const startIdx = visibleCount - visibleData.length;
    const visibleIdx = Math.floor(x / candleW) - startIdx;

    if (visibleIdx >= 0 && visibleIdx < visibleData.length) {
      // visibleData 是 data 的最後 visibleCount 筆，換算全域索引
      const globalIdx = data.length - visibleData.length + visibleIdx;
      const indVals = calcIndicatorValues(globalIdx);
      if (setTooltip) setTooltip({ ...visibleData[visibleIdx], x: e.clientX, y: e.clientY, indVals });
    } else if (setTooltip) {
      setTooltip(null);
    }
  };

  return (
    <div ref={containerRef} style={{ position: "relative", width: "100%", height: "100%" }}>
      <canvas
        ref={canvasRef}
        onMouseDown={handleMouseDown}
        onMouseMove={(e) => { handleDragMove(e); handleMouseMove(e); }}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => { dragRef.current = null; if (setTooltip) setTooltip(null); setCrosshair(null); }}
        style={{ width: "100%", height: "100%", cursor: dragRef.current ? "grabbing" : "crosshair", display: "block" }}
      />
    </div>
  );
}

// ─── Volume Chart Component ──────────────────────────────────────────
function VolumeChart({ data, visibleCount, offset, setTooltip, refLines = [{ level: 1500, label: "爆大量" }, { level: 400, label: "大量" }] }) {
  const canvasRef = useRef(null);
  const [crosshair, setCrosshair] = useState(null);

  const visibleData = useMemo(() => {
    if (!data.length) return [];
    const end = data.length - offset;
    const start = Math.max(0, end - visibleCount);
    return data.slice(start, end);
  }, [data, offset, visibleCount]);

  const drawVolume = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width = canvas.offsetWidth * 2;
    const H = canvas.height = canvas.offsetHeight * 2;
    ctx.scale(2, 2);
    const w = W / 2;
    const h = H / 2;
    ctx.clearRect(0, 0, w, h);
    if (visibleData.length === 0) return;

    const maxVol = Math.max(...visibleData.map(d => d.volume), 1);
    const barW = (w - 50) / visibleCount;
    const startIdx = visibleCount - visibleData.length;
    const bottomY = h - 14;

    // Grid lines
    const vSteps = [10, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000].reverse();
    let vStep = 100;
    for (const s of vSteps) {
      if (maxVol / s >= 3 && maxVol / s <= 6) {
        vStep = s;
        break;
      }
    }
    const startV = vStep;

    ctx.strokeStyle = "#1a2235";
    ctx.lineWidth = 0.5;
    ctx.fillStyle = COLORS.textMuted;
    ctx.font = "9px monospace";
    ctx.textAlign = "right";

    let curV = startV;
    while (curV <= maxVol) {
      const y = bottomY - (curV / maxVol) * (bottomY - 10);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w - 45, y); ctx.stroke();
      ctx.fillText(curV.toLocaleString(), w - 4, y + 3);
      curV += vStep;
    }

    const targetTimes = new Set(["08:45", "09:00", "09:15", "09:45", "10:30", "11:15", "12:00", "12:30", "13:00", "13:30", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00", "20:30", "21:00", "21:30", "22:00", "22:30", "23:00", "00:00", "01:00", "04:00"]);
    ctx.textAlign = "center";
    ctx.fillStyle = COLORS.textDim;
    visibleData.forEach((d, i) => {
      const dDate = new Date(d.time);
      const hhmm = dDate.getHours().toString().padStart(2, '0') + ":" + dDate.getMinutes().toString().padStart(2, '0');
      if (targetTimes.has(hhmm)) {
        const x = (startIdx + i) * barW + barW / 2;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, bottomY); ctx.stroke();
        ctx.fillText(hhmm, x, h - 2);
      }
    });

    visibleData.forEach((d, i) => {
      const isUp = d.close >= d.open;
      const volH = (d.volume / maxVol) * (bottomY - 10);
      ctx.fillStyle = isUp ? "rgba(34,197,94,0.45)" : "rgba(239,68,68,0.45)";
      ctx.fillRect((startIdx + i) * barW + 1, bottomY - volH, Math.max(barW - 2, 1), volH);
    });

    // 成交量水平參考線（由 config/settings.py 的 VOLUME_REFERENCE_LINES 設定）
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 2]);
    ctx.textAlign = "right";
    ctx.font = "9px monospace";
    refLines.forEach(({ level, label }) => {
      if (level > maxVol) return; // 量太小、超出目前範圍時不畫，不去撐大縱軸
      const y = bottomY - (level / maxVol) * (bottomY - 10);
      ctx.strokeStyle = "#f59e0b";
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w - 45, y); ctx.stroke();
      ctx.fillStyle = "#f59e0b";
      ctx.fillText(`${label} ${level.toLocaleString()}`, w - 4, y + 3);
    });
    ctx.setLineDash([]);

    // Crosshair
    if (crosshair) {
      ctx.strokeStyle = "rgba(255,255,255,0.15)";
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 0.5;
      ctx.beginPath();
      ctx.moveTo(crosshair.x, 0);
      ctx.lineTo(crosshair.x, h);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, crosshair.y);
      ctx.lineTo(w, crosshair.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }, [visibleData, crosshair, refLines]);

  useEffect(() => { drawVolume(); }, [drawVolume]);

  const handleMouseMove = (e) => {
    const rect = canvasRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    setCrosshair({ x, y });

    const w = rect.width - 50;
    const barW = w / visibleCount;
    const startIdx = visibleCount - visibleData.length;
    const visibleIdx = Math.floor(x / barW) - startIdx;

    if (visibleIdx >= 0 && visibleIdx < visibleData.length && setTooltip) {
      setTooltip({ ...visibleData[visibleIdx], x: e.clientX, y: e.clientY });
    } else if (setTooltip) {
      setTooltip(null);
    }
  };

  return (
    <div style={{ width: "100%", height: "100%", position: "relative" }}>
      <canvas
        ref={canvasRef}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => { setCrosshair(null); if (setTooltip) setTooltip(null); }}
        style={{ width: "100%", height: "100%", cursor: "crosshair", display: "block" }}
      />
    </div>
  );
}

// ─── Timeline Navigator Component ─────────────────────────────────────────
function TimelineNavigator({ data, visibleCount, setVisibleCount, offset, setOffset }) {
  const containerRef = useRef(null);
  const dragRef = useRef(null);

  const pathData = useMemo(() => {
    if (!data || data.length === 0) return "";
    const minP = Math.min(...data.map(d => d.close));
    const maxP = Math.max(...data.map(d => d.close));
    const range = maxP - minP || 1;
    let path = "";
    for (let i = 0; i < data.length; i++) {
      const x = (i / (data.length - 1)) * 100;
      const y = 90 - ((data[i].close - minP) / range) * 80;
      path += `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)} `;
    }
    return path;
  }, [data]);

  const handleMouseDown = (e, type) => {
    e.stopPropagation();
    dragRef.current = { type, startX: e.clientX, startOffset: offset, startVisibleCount: visibleCount };
  };

  useEffect(() => {
    const clampData = (v, min, max) => Math.max(min, Math.min(max, v));
    const handleMouseMove = (e) => {
      if (!dragRef.current) return;
      const { type, startX, startOffset, startVisibleCount } = dragRef.current;
      const rect = containerRef.current.getBoundingClientRect();
      const dx = e.clientX - startX;
      const deltaBars = Math.round((dx / rect.width) * data.length);

      if (type === "pan") {
        const maxOffset = Math.max(0, data.length - startVisibleCount);
        setOffset(clampData(startOffset - deltaBars, 0, maxOffset));
      } else if (type === "resize-left") {
        const newVisibleCount = clampData(startVisibleCount - deltaBars, 10, data.length - startOffset);
        setVisibleCount(newVisibleCount);
      } else if (type === "resize-right") {
        const startIdx = data.length - startOffset - startVisibleCount;
        let newOffset = startOffset - deltaBars;
        newOffset = clampData(newOffset, 0, Math.max(0, data.length - startIdx - 10));
        const newVisibleCount = data.length - newOffset - startIdx;
        setOffset(newOffset);
        setVisibleCount(clampData(newVisibleCount, 10, 1800));
      }
    };
    const handleMouseUp = () => { dragRef.current = null; };
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => { window.removeEventListener("mousemove", handleMouseMove); window.removeEventListener("mouseup", handleMouseUp); };
  }, [data.length, setOffset, setVisibleCount, offset, visibleCount]);

  if (!data.length) return null;

  const clampData = (v, min, max) => Math.max(min, Math.min(max, v));
  const leftPerc = clampData((data.length - offset - visibleCount) / data.length * 100, 0, 100);
  const rightPerc = clampData((data.length - offset) / data.length * 100, 0, 100);
  const widthPerc = rightPerc - leftPerc;

  return (
    <div ref={containerRef} style={{ height: 32, position: "relative", backgroundColor: COLORS.bgCard, borderRadius: 4, overflow: "hidden", border: `1px solid ${COLORS.border}`, flexShrink: 0 }}>
      {/* Background Micro Chart */}
      <svg width="100%" height="100%" preserveAspectRatio="none" viewBox="0 0 100 100" style={{ position: "absolute", top: 0, left: 0 }}>
        <path d={pathData} fill="none" stroke="rgba(59,130,246,0.3)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      {/* Background Dimmer (Left of window) */}
      <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${leftPerc}%`, backgroundColor: "rgba(0,0,0,0.4)" }} />
      {/* Background Dimmer (Right of window) */}
      <div style={{ position: "absolute", top: 0, bottom: 0, left: `${rightPerc}%`, right: 0, backgroundColor: "rgba(0,0,0,0.4)" }} />

      {/* Draggable Window Pane */}
      <div
        style={{
          position: "absolute", top: 0, bottom: 0,
          left: `${leftPerc}%`, width: `${widthPerc}%`,
          backgroundColor: "rgba(59,130,246,0.15)",
          borderTop: `1px solid ${COLORS.accentDim}`,
          borderBottom: `1px solid ${COLORS.accentDim}`,
          cursor: "grab",
          boxSizing: "border-box"
        }}
        onMouseDown={(e) => handleMouseDown(e, "pan")}
      >
        {/* Left Resize Handle */}
        <div style={{ position: "absolute", left: 0, width: 6, top: 0, bottom: 0, cursor: "ew-resize", backgroundColor: COLORS.accent, borderRadius: "2px 0 0 2px", opacity: 0.8 }} onMouseDown={(e) => handleMouseDown(e, "resize-left")} />
        {/* Right Resize Handle */}
        <div style={{ position: "absolute", right: 0, width: 6, top: 0, bottom: 0, cursor: "ew-resize", backgroundColor: COLORS.accent, borderRadius: "0 2px 2px 0", opacity: 0.8 }} onMouseDown={(e) => handleMouseDown(e, "resize-right")} />
      </div>
    </div>
  );
}

// ─── Order Panel (Lightning Order — Price Ladder) ───────────────────
function OrderPanel({ brokerConfig, currentPrice = 17535, activeSymbol, setActiveSymbol, orderbook, myBuyOrders, setMyBuyOrders, mySellOrders, setMySellOrders, stopBuys, setStopBuys, stopSells, setStopSells }) {
  const [qty, setQty] = useState(1);
  const [centerOnPrice, setCenterOnPrice] = useState(true); // 成交置中 toggle
  const scrollRef = useRef(null);

  const tickSize = 1;

  const ladderData = useMemo(() => {
    const rows = [];
    // 成交價往上200 tick, 往下200 tick = 總共401個價格
    const topPrice = currentPrice + 200 * tickSize;

    const bidMap = {};
    const askMap = {};
    if (orderbook) {
      orderbook.bids.forEach(b => { bidMap[b.price] = b.qty; });
      orderbook.asks.forEach(a => { askMap[a.price] = a.qty; });
    }

    for (let i = 0; i < 401; i++) {
      const price = topPrice - i * tickSize;
      const isBid = price < currentPrice;
      const isAsk = price > currentPrice;
      const isCurrent = price === currentPrice;

      let bidQty = bidMap[price] || 0;
      let askQty = askMap[price] || 0;

      rows.push({ price, bidQty, askQty, isBid, isAsk, isCurrent });
    }
    return rows;
  }, [currentPrice, orderbook]);

  const maxQty = Math.max(...ladderData.map(r => Math.max(r.bidQty, r.askQty)), 1);

  // 自動置中滾動效果
  useEffect(() => {
    if (centerOnPrice && scrollRef.current) {
      // 找到當前成交價在列表中的索引 (應該是第200個，索引199)
      const currentPriceIndex = 200;
      const rowHeight = 22; // ROW_H
      const containerHeight = scrollRef.current.clientHeight;
      const headerHeight = 30; // sticky header 高度

      // 計算滾動位置：讓成交價顯示在可視區域中間
      const scrollTop = (currentPriceIndex * rowHeight) - (containerHeight / 2) + (rowHeight / 2) + headerHeight;

      scrollRef.current.scrollTop = Math.max(0, scrollTop);
    }
  }, [centerOnPrice, currentPrice]);

  // Unified handler: left-click = place order, right-click = delete order
  const handleCell = (setter, price, e) => {
    if (e) e.preventDefault();
    if (e && e.type === "contextmenu") {
      setter(o => { const n = { ...o }; delete n[price]; return n; });
    } else {
      setter(o => ({ ...o, [price]: (o[price] || 0) + qty }));
    }
  };

  const ROW_H = 22;
  const GRID_COLS = "28px 1.1fr 38px 54px 38px 1.1fr 28px";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      {/* Header */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "6px 10px", borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0
      }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: COLORS.text, letterSpacing: 1 }}>⚡ 閃電下單</span>
        <span style={{ fontSize: 9, color: COLORS.textDim }}>
          <span style={{ color: COLORS.accent }}>{brokerConfig.tradeBroker}</span>
        </span>
      </div>

      {/* Symbol + Qty bar */}
      <div style={{
        display: "flex", alignItems: "center", gap: 4, padding: "5px 8px",
        borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0
      }}>
        <div style={{ display: "flex", gap: 2 }}>
          {["TX", "MTX", "TMF"].map(s => (
            <button key={s} onClick={() => setActiveSymbol(s)} style={{
              padding: "3px 8px", fontSize: 10, fontWeight: activeSymbol === s ? 700 : 400,
              background: activeSymbol === s ? "rgba(59,130,246,0.15)" : "transparent",
              border: `1px solid ${activeSymbol === s ? COLORS.accent : COLORS.border}`,
              color: activeSymbol === s ? COLORS.accent : COLORS.textDim, borderRadius: 3, cursor: "pointer"
            }}>{s}</button>
          ))}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 0 }}>
          <span style={{ fontSize: 9, color: COLORS.textDim, marginRight: 4 }}>口數</span>
          <button onClick={() => setQty(q => Math.max(1, q - 1))} style={{
            width: 20, height: 20, border: `1px solid ${COLORS.border}`, background: COLORS.bgCard,
            color: COLORS.textDim, borderRadius: "3px 0 0 3px", cursor: "pointer", fontSize: 12, padding: 0,
            display: "flex", alignItems: "center", justifyContent: "center"
          }}>−</button>
          <div style={{
            width: 28, height: 20, border: `1px solid ${COLORS.border}`, borderLeft: "none", borderRight: "none",
            background: COLORS.bg, color: COLORS.text, display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 12, fontFamily: "monospace", fontWeight: 700
          }}>{qty}</div>
          <button onClick={() => setQty(q => q + 1)} style={{
            width: 20, height: 20, border: `1px solid ${COLORS.border}`, background: COLORS.bgCard,
            color: COLORS.textDim, borderRadius: "0 3px 3px 0", cursor: "pointer", fontSize: 12, padding: 0,
            display: "flex", alignItems: "center", justifyContent: "center"
          }}>+</button>
        </div>
      </div>

      {/* Hint */}
      <div style={{
        padding: "2px 8px", fontSize: 8, color: COLORS.textMuted, textAlign: "center",
        borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0, letterSpacing: 0.5
      }}>左鍵下單 ／ 右鍵刪單</div>

      {/* Price Ladder with sticky header */}
      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", overflowX: "hidden", position: "relative" }} onContextMenu={e => e.preventDefault()}>
        {/* Column headers - sticky */}
        <div style={{
          display: "grid", gridTemplateColumns: GRID_COLS,
          borderBottom: `1px solid ${COLORS.border}`, padding: "3px 0",
          position: "sticky", top: 0, background: COLORS.bgPanel, zIndex: 10
        }}>
          {["觸買", "買進", "委買", "價格", "委賣", "賣出", "觸賣"].map((h, i) => (
            <div key={i} style={{
              fontSize: 9, textAlign: "center", fontWeight: 600, padding: "0 2px",
              color: (i === 0 || i === 6) ? COLORS.warn : COLORS.textMuted
            }}>{h}</div>
          ))}
        </div>

        {/* Price ladder rows */}
        {ladderData.map((row) => {
          const isBidZone = row.price < currentPrice;
          const isAskZone = row.price > currentPrice;
          const hasBuyOrder = myBuyOrders[row.price];
          const hasSellOrder = mySellOrders[row.price];
          const hasStopBuy = stopBuys[row.price];
          const hasStopSell = stopSells[row.price];

          const cellBase = {
            height: "100%", cursor: "pointer", position: "relative",
            display: "flex", alignItems: "center", justifyContent: "center", userSelect: "none",
          };
          const tagStyle = (color, bgColor) => ({
            fontSize: 9, fontFamily: "monospace", fontWeight: 700, color,
            background: bgColor, padding: "0 3px", borderRadius: 2, lineHeight: "16px"
          });

          return (
            <div key={row.price} style={{
              display: "grid", gridTemplateColumns: GRID_COLS,
              height: ROW_H, alignItems: "center",
              borderBottom: `1px solid ${COLORS.border}10`,
              background: row.isCurrent ? "rgba(250,204,21,0.12)"
                : isAskZone ? "rgba(239,68,68,0.04)" : isBidZone ? "rgba(34,197,94,0.04)" : "transparent",
            }}>
              {/* 觸買 */}
              <div style={cellBase}
                onClick={e => handleCell(setStopBuys, row.price, e)}
                onContextMenu={e => handleCell(setStopBuys, row.price, e)}
                title="左鍵:觸價買 / 右鍵:刪除">
                {hasStopBuy && <span style={tagStyle(COLORS.warn, "rgba(245,158,11,0.18)")}>{hasStopBuy}</span>}
              </div>

              {/* 買進 */}
              <div style={{
                ...cellBase, justifyContent: "flex-end", paddingRight: 4,
                background: isBidZone ? `linear-gradient(to right, transparent ${100 - (row.bidQty / maxQty) * 100}%, rgba(239,147,147,0.2) 100%)` : "transparent",
              }}
                onClick={e => handleCell(setMyBuyOrders, row.price, e)}
                onContextMenu={e => handleCell(setMyBuyOrders, row.price, e)}
                title="左鍵:買進 / 右鍵:刪除">
                {hasBuyOrder && <span style={tagStyle(COLORS.up, "rgba(34,197,94,0.15)")}>{hasBuyOrder}</span>}
              </div>

              {/* 委買 */}
              <div style={{
                textAlign: "center", fontSize: 11, fontFamily: "monospace", fontWeight: 600,
                color: row.bidQty > 0 ? COLORS.text : "transparent"
              }}>{row.bidQty > 0 ? row.bidQty : ""}</div>

              {/* 價格 */}
              <div style={{
                textAlign: "center", fontSize: 11, fontFamily: "monospace", fontWeight: 700,
                color: row.isCurrent ? "#facc15" : COLORS.text,
                background: row.isCurrent ? "rgba(250,204,21,0.15)" : "transparent",
                borderRadius: 2, padding: "1px 0"
              }}>{row.price}</div>

              {/* 委賣 */}
              <div style={{
                textAlign: "center", fontSize: 11, fontFamily: "monospace", fontWeight: 600,
                color: row.askQty > 0 ? COLORS.text : "transparent"
              }}>{row.askQty > 0 ? row.askQty : ""}</div>

              {/* 賣出 */}
              <div style={{
                ...cellBase, justifyContent: "flex-start", paddingLeft: 4,
                background: isAskZone ? `linear-gradient(to left, transparent ${100 - (row.askQty / maxQty) * 100}%, rgba(147,176,239,0.2) 100%)` : "transparent",
              }}
                onClick={e => handleCell(setMySellOrders, row.price, e)}
                onContextMenu={e => handleCell(setMySellOrders, row.price, e)}
                title="左鍵:賣出 / 右鍵:刪除">
                {hasSellOrder && <span style={tagStyle(COLORS.down, "rgba(239,68,68,0.15)")}>{hasSellOrder}</span>}
              </div>

              {/* 觸賣 */}
              <div style={cellBase}
                onClick={e => handleCell(setStopSells, row.price, e)}
                onContextMenu={e => handleCell(setStopSells, row.price, e)}
                title="左鍵:觸價賣 / 右鍵:刪除">
                {hasStopSell && <span style={tagStyle(COLORS.warn, "rgba(245,158,11,0.18)")}>{hasStopSell}</span>}
              </div>
            </div>
          );
        })}
      </div>

      {/* Bottom info row */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr auto 1fr",
        padding: "4px 8px", borderTop: `1px solid ${COLORS.border}`,
        fontSize: 10, fontFamily: "monospace", color: COLORS.textDim, flexShrink: 0,
        alignItems: "center"
      }}>
        <div style={{ textAlign: "left" }}>
          <span style={{ color: COLORS.up, fontWeight: 600 }}>
            {Object.values(myBuyOrders).reduce((s, v) => s + v, 0) || "—"}
          </span>
          <span style={{ marginLeft: 3 }}>買委</span>
        </div>
        <button
          onClick={() => setCenterOnPrice(!centerOnPrice)}
          style={{
            textAlign: "center",
            fontSize: 9,
            fontWeight: 600,
            padding: "3px 10px",
            background: centerOnPrice ? "rgba(59,130,246,0.15)" : "transparent",
            border: `1px solid ${centerOnPrice ? COLORS.accent : COLORS.border}`,
            color: centerOnPrice ? COLORS.accent : COLORS.textMuted,
            borderRadius: 3,
            cursor: "pointer",
            transition: "all 0.2s"
          }}
          title={centerOnPrice ? "點擊關閉成交置中" : "點擊開啟成交置中"}
        >
          {centerOnPrice ? "🎯" : "○"} 成交置中
        </button>
        <div style={{ textAlign: "right" }}>
          <span>賣委</span>
          <span style={{ marginLeft: 3, color: COLORS.down, fontWeight: 600 }}>
            {Object.values(mySellOrders).reduce((s, v) => s + v, 0) || "—"}
          </span>
        </div>
      </div>

      {/* Action buttons */}
      <div style={{
        display: "flex", gap: 4, padding: "5px 8px", borderTop: `1px solid ${COLORS.border}`, flexShrink: 0
      }}>
        <button onClick={() => setMyBuyOrders({})} style={{
          flex: 1, padding: "5px 0", fontSize: 10, fontWeight: 600,
          background: "rgba(34,197,94,0.08)", border: `1px solid rgba(34,197,94,0.25)`,
          color: COLORS.up, borderRadius: 3, cursor: "pointer"
        }}>買單全刪</button>
        <button style={{
          padding: "5px 10px", fontSize: 10, fontWeight: 700,
          background: "linear-gradient(135deg, #16a34a, #22c55e)", border: "none",
          color: "#fff", borderRadius: 3, cursor: "pointer"
        }}>市買</button>
        <button style={{
          padding: "5px 10px", fontSize: 10, fontWeight: 700,
          background: "linear-gradient(135deg, #dc2626, #ef4444)", border: "none",
          color: "#fff", borderRadius: 3, cursor: "pointer"
        }}>市賣</button>
        <button onClick={() => setMySellOrders({})} style={{
          flex: 1, padding: "5px 0", fontSize: 10, fontWeight: 600,
          background: "rgba(239,68,68,0.08)", border: `1px solid rgba(239,68,68,0.25)`,
          color: COLORS.down, borderRadius: 3, cursor: "pointer"
        }}>賣單全刪</button>
      </div>

      {/* Stop orders delete buttons */}
      <div style={{
        display: "flex", gap: 4, padding: "4px 8px", borderTop: `1px solid ${COLORS.border}`, flexShrink: 0
      }}>
        <button onClick={() => setStopBuys({})} style={{
          flex: 1, padding: "4px 0", fontSize: 9, fontWeight: 600,
          background: "rgba(245,158,11,0.08)", border: `1px solid rgba(245,158,11,0.25)`,
          color: COLORS.warn, borderRadius: 3, cursor: "pointer"
        }}>觸買全刪</button>
        <button onClick={() => setStopSells({})} style={{
          flex: 1, padding: "4px 0", fontSize: 9, fontWeight: 600,
          background: "rgba(245,158,11,0.08)", border: `1px solid rgba(245,158,11,0.25)`,
          color: COLORS.warn, borderRadius: 3, cursor: "pointer"
        }}>觸賣全刪</button>
      </div>

    </div>
  );
}

// ─── Position & Orders Panel ─────────────────────────────────────────
function PositionOrdersPanel({ myBuyOrders, mySellOrders, stopBuys, stopSells, setMyBuyOrders, setMySellOrders, setStopBuys, setStopSells }) {
  const [tab, setTab] = useState("positions");
  const totalPnl = MOCK_POSITIONS.reduce((s, p) => s + p.pnl, 0);

  const allOrders = [
    ...Object.entries(myBuyOrders).map(([p, q]) => ({ price: +p, qty: q, type: "限價買", color: COLORS.up, setter: setMyBuyOrders })),
    ...Object.entries(mySellOrders).map(([p, q]) => ({ price: +p, qty: q, type: "限價賣", color: COLORS.down, setter: setMySellOrders })),
    ...Object.entries(stopBuys).map(([p, q]) => ({ price: +p, qty: q, type: "觸價買", color: COLORS.warn, setter: setStopBuys })),
    ...Object.entries(stopSells).map(([p, q]) => ({ price: +p, qty: q, type: "觸價賣", color: COLORS.warn, setter: setStopSells })),
  ].sort((a, b) => b.price - a.price);

  const tabBtn = (id, label, count) => (
    <button key={id} onClick={() => setTab(id)} style={{
      padding: "5px 10px", fontSize: 11, fontWeight: tab === id ? 700 : 400,
      color: tab === id ? COLORS.accent : COLORS.textDim,
      borderBottom: tab === id ? `2px solid ${COLORS.accent}` : "2px solid transparent",
      background: "transparent", border: "none", borderBottomStyle: "solid", cursor: "pointer",
      display: "flex", alignItems: "center", gap: 4
    }}>
      {label}
      {count > 0 && <span style={{
        fontSize: 9, background: tab === id ? "rgba(59,130,246,0.2)" : COLORS.bgCard,
        padding: "0 5px", borderRadius: 8, color: tab === id ? COLORS.accent : COLORS.textDim,
        fontWeight: 600, lineHeight: "16px"
      }}>{count}</span>}
    </button>
  );

  const deleteOrder = (order) => {
    order.setter(o => { const n = { ...o }; delete n[order.price]; return n; });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div style={{ display: "flex", borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0 }}>
        {tabBtn("positions", "倉位", MOCK_POSITIONS.length)}
        {tabBtn("orders", "委託", allOrders.length)}
      </div>

      <div style={{ flex: 1, overflowY: "auto", fontSize: 11 }}>
        {tab === "positions" && (
          <>
            <div style={{ padding: "6px 6px 4px", display: "flex", justifyContent: "space-between", fontSize: 10, position: "sticky", top: 0, background: COLORS.bgPanel, zIndex: 10 }}>
              <span style={{ color: COLORS.textDim }}>持倉部位</span>
              <span style={{ color: totalPnl >= 0 ? COLORS.up : COLORS.down, fontWeight: 700, fontFamily: "monospace" }}>
                總損益 {totalPnl >= 0 ? "+" : ""}{totalPnl.toLocaleString()}
              </span>
            </div>
            <div style={{ padding: "0 6px 6px" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ color: COLORS.textMuted, fontSize: 9, borderBottom: `1px solid ${COLORS.border}`, position: "sticky", top: 34, background: COLORS.bgPanel, zIndex: 9 }}>
                    {["商品", "方向", "口", "均價", "現價", "損益"].map(h => (
                      <th key={h} style={{ padding: "3px 4px", textAlign: "right", fontWeight: 500 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {MOCK_POSITIONS.map(p => (
                    <tr key={p.id} style={{ borderBottom: `1px solid ${COLORS.border}08` }}>
                      <td style={{ padding: "4px", textAlign: "left", color: COLORS.text, fontWeight: 600 }}>{p.symbol}</td>
                      <td style={{ padding: "4px", textAlign: "right", color: p.direction === "多" ? COLORS.up : COLORS.down, fontWeight: 600 }}>{p.direction}</td>
                      <td style={{ padding: "4px", textAlign: "right", color: COLORS.text, fontFamily: "monospace" }}>{p.qty}</td>
                      <td style={{ padding: "4px", textAlign: "right", color: COLORS.textDim, fontFamily: "monospace" }}>{p.avgPrice}</td>
                      <td style={{ padding: "4px", textAlign: "right", color: COLORS.text, fontFamily: "monospace" }}>{p.currentPrice}</td>
                      <td style={{ padding: "4px", textAlign: "right", fontFamily: "monospace", fontWeight: 600, color: p.pnl >= 0 ? COLORS.up : COLORS.down }}>
                        {p.pnl >= 0 ? "+" : ""}{p.pnl.toLocaleString()}
                        <span style={{ fontSize: 9, marginLeft: 2, opacity: 0.7 }}>({p.pnlPercent}%)</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        {tab === "orders" && (
          <>
            {allOrders.length === 0 ? (
              <div style={{ textAlign: "center", padding: 20, color: COLORS.textMuted, fontSize: 12 }}>尚無委託單</div>
            ) : (
              <div style={{ padding: "0 6px 6px" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ color: COLORS.textMuted, fontSize: 9, borderBottom: `1px solid ${COLORS.border}`, position: "sticky", top: 0, background: COLORS.bgPanel, zIndex: 10 }}>
                      {["類型", "價格", "口數", ""].map((h, i) => (
                        <th key={i} style={{ padding: "3px 4px", textAlign: h === "" ? "center" : "right", fontWeight: 500 }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {allOrders.map((o, i) => (
                      <tr key={i} style={{ borderBottom: `1px solid ${COLORS.border}08` }}>
                        <td style={{ padding: "4px", textAlign: "left", color: o.color, fontWeight: 600, fontSize: 10 }}>{o.type}</td>
                        <td style={{ padding: "4px", textAlign: "right", color: COLORS.text, fontFamily: "monospace" }}>{o.price}</td>
                        <td style={{ padding: "4px", textAlign: "right", color: COLORS.text, fontFamily: "monospace" }}>{o.qty}</td>
                        <td style={{ padding: "4px", textAlign: "center" }}>
                          <button onClick={() => deleteOrder(o)} style={{
                            padding: "1px 8px", border: `1px solid rgba(239,68,68,0.3)`,
                            background: "rgba(239,68,68,0.08)", color: COLORS.danger,
                            fontSize: 9, borderRadius: 3, cursor: "pointer"
                          }}>刪</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ─── Trade History Panel ─────────────────────────────────────────────
function TradeHistoryPanel() {
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "6px 10px", borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0
      }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: COLORS.text }}>成交明細</span>
        <span style={{ fontSize: 9, color: COLORS.textDim }}>
          今日 {MOCK_TRADES.filter(t => t.status === "成交").length} 筆
        </span>
      </div>

      <div style={{ flex: 1, overflowY: "auto", fontSize: 10 }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: COLORS.textMuted, fontSize: 9, borderBottom: `1px solid ${COLORS.border}`, position: "sticky", top: 0, background: COLORS.bgPanel }}>
              {["時間", "商品", "方向", "價格", "口"].map(h => (
                <th key={h} style={{ padding: "4px 6px", textAlign: h === "時間" ? "left" : "right", fontWeight: 500 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {MOCK_TRADES.filter(t => t.status === "成交").map(t => (
              <tr key={t.id} style={{ borderBottom: `1px solid ${COLORS.border}08` }}>
                <td style={{ padding: "4px 6px", textAlign: "left", color: COLORS.textDim, fontFamily: "monospace", fontSize: 9 }}>{t.time}</td>
                <td style={{ padding: "4px 6px", textAlign: "right", color: COLORS.text, fontWeight: 600 }}>{t.symbol}</td>
                <td style={{ padding: "4px 6px", textAlign: "right", fontWeight: 600, color: t.direction === "買" ? COLORS.up : COLORS.down }}>{t.direction}</td>
                <td style={{ padding: "4px 6px", textAlign: "right", color: COLORS.text, fontFamily: "monospace" }}>{t.price}</td>
                <td style={{ padding: "4px 6px", textAlign: "right", color: COLORS.text, fontFamily: "monospace" }}>{t.qty}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Broker Config Panel ──────────────────────────────────────────────
function BrokerConfigPanel({ brokerConfig, setBrokerConfig, onClose, send, addHandler }) {
  // 以 BROKER_LIST 為基礎，connected 由後端實際狀態決定
  const [brokers, setBrokers] = useState(
    BROKER_LIST.map(b => ({ ...b, status: "disconnected" }))
  );
  const [pending, setPending] = useState(null); // 正在連線/斷線的 broker_id
  const [message, setMessage] = useState(null); // { text, ok }

  // 初始化：查詢後端目前連線狀態
  useEffect(() => {
    const cleanup = addHandler("broker_status", (msg) => {
      const connectedId = msg.quote?.broker_id;
      setBrokers(bs => bs.map(b => ({
        ...b,
        status: b.id === connectedId && msg.quote?.connected ? "connected" : "disconnected",
      })));
    });
    send("broker_status", {});
    return cleanup;
  }, [send, addHandler]);

  // 連線/斷線結果回調
  useEffect(() => {
    const cleanup = addHandler("broker_config_result", (msg) => {
      setPending(null);
      setMessage({ text: msg.message, ok: msg.success });
      if (msg.success) {
        setBrokers(bs => bs.map(b => ({
          ...b,
          status: b.id === msg.broker_id
            ? (msg.connected ? "connected" : "disconnected")
            : b.status,
        })));
      }
    });
    return cleanup;
  }, [addHandler]);

  const toggleConnect = (id, currentStatus) => {
    setPending(id);
    setMessage(null);
    if (currentStatus === "connected") {
      send("broker_config", { action: "disconnect", broker_id: id });
    } else {
      send("broker_config", { action: "connect", broker_id: id });
    }
  };

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "flex",
      alignItems: "center", justifyContent: "center", zIndex: 1000, backdropFilter: "blur(4px)"
    }}>
      <div style={{
        background: COLORS.bgPanel, border: `1px solid ${COLORS.borderLight}`,
        borderRadius: 12, padding: 24, width: 520, maxHeight: "80vh", overflow: "auto",
        boxShadow: "0 20px 60px rgba(0,0,0,0.6)"
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 20 }}>
          <h2 style={{ color: COLORS.text, fontSize: 16, fontWeight: 700, margin: 0 }}>券商 API 設定</h2>
          <button onClick={onClose} style={{
            background: "none", border: "none", color: COLORS.textDim, cursor: "pointer", fontSize: 18
          }}>✕</button>
        </div>

        <div style={{ fontSize: 11, color: COLORS.textDim, marginBottom: 16, padding: "8px 12px", background: COLORS.bgCard, borderRadius: 6, borderLeft: `3px solid ${COLORS.accent}` }}>
          問價與交易是獨立模塊 — 可以使用不同券商的 API 分別處理問價與下單
        </div>

        {/* 操作回饋訊息 */}
        {message && (
          <div style={{
            marginBottom: 12, padding: "8px 12px", borderRadius: 6, fontSize: 11,
            background: message.ok ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)",
            border: `1px solid ${message.ok ? COLORS.success : COLORS.danger}`,
            color: message.ok ? COLORS.success : COLORS.danger,
          }}>{message.text}</div>
        )}

        {/* Broker List */}
        <div style={{ marginBottom: 20 }}>
          <div style={{ fontSize: 12, color: COLORS.textDim, fontWeight: 600, marginBottom: 8 }}>已設定券商</div>
          {brokers.map(b => {
            const isConnected = b.status === "connected";
            const isLoading = pending === b.id;
            return (
              <div key={b.id} style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "10px 12px", background: COLORS.bgCard, borderRadius: 6, marginBottom: 6,
                border: `1px solid ${isConnected ? "rgba(34,197,94,0.3)" : COLORS.border}`
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{
                    width: 8, height: 8, borderRadius: "50%",
                    background: isLoading ? COLORS.warn : isConnected ? COLORS.success : COLORS.textMuted,
                    boxShadow: isConnected ? `0 0 8px ${COLORS.success}` : "none",
                    animation: isLoading ? "pulse 1s infinite" : "none",
                  }} />
                  <span style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>{b.name}</span>
                  {isConnected && (
                    <span style={{ fontSize: 10, color: COLORS.success, background: "rgba(34,197,94,0.1)", padding: "1px 6px", borderRadius: 3 }}>已連線</span>
                  )}
                </div>
                <button
                  onClick={() => toggleConnect(b.id, b.status)}
                  disabled={isLoading || (pending !== null && pending !== b.id)}
                  style={{
                    padding: "4px 14px",
                    border: `1px solid ${isConnected ? COLORS.danger : COLORS.success}`,
                    background: "transparent", borderRadius: 4, cursor: isLoading ? "wait" : "pointer", fontSize: 11,
                    color: isConnected ? COLORS.danger : COLORS.success,
                    opacity: (pending !== null && pending !== b.id) ? 0.4 : 1,
                  }}
                >
                  {isLoading ? "處理中..." : isConnected ? "斷線" : "連線"}
                </button>
              </div>
            );
          })}
        </div>

        {/* Module Assignment */}
        <div style={{ display: "flex", gap: 12 }}>
          {[["quoteBroker", "問價模塊"], ["tradeBroker", "交易模塊"]].map(([key, label]) => (
            <div key={key} style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: COLORS.textDim, marginBottom: 4 }}>{label}</div>
              <select value={brokerConfig[key]}
                onChange={e => setBrokerConfig(c => ({ ...c, [key]: e.target.value }))}
                style={{
                  width: "100%", padding: "8px 10px", background: COLORS.bg,
                  border: `1px solid ${COLORS.border}`, borderRadius: 4,
                  color: COLORS.text, fontSize: 12, outline: "none"
                }}>
                {brokers.filter(b => b.status === "connected").map(b => (
                  <option key={b.id} value={b.name}>{b.name}</option>
                ))}
              </select>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Scripts Manager ──────────────────────────────────────────────────
function ScriptsPanel({ scripts, send, activeView }) {
  const [selectedScript, setSelectedScript] = useState(null);
  const [editCode, setEditCode] = useState("");

  if (activeView !== "scripts") return null;

  return (
    <div style={{ display: "flex", height: "100%", gap: 0 }}>
      {/* Script list */}
      <div style={{ width: 260, borderRight: `1px solid ${COLORS.border}`, padding: 12, overflowY: "auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: COLORS.text }}>📜 Scripts</span>
          <button style={{
            padding: "3px 10px", background: COLORS.accent, color: "#fff", border: "none",
            borderRadius: 4, fontSize: 11, cursor: "pointer"
          }}>+ 新增</button>
        </div>

        {["indicator", "strategy"].map(type => (
          <div key={type} style={{ marginBottom: 12 }}>
            <div style={{
              fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", letterSpacing: 1.5,
              marginBottom: 6, fontWeight: 600
            }}>{type === "indicator" ? "技術指標" : "交易策略"}</div>
            {scripts.filter(s => s.type === type).map(s => (
              <div key={s.id} onClick={() => { setSelectedScript(s); setEditCode(s.code); }} style={{
                padding: "8px 10px", background: selectedScript?.id === s.id ? COLORS.bgHover : "transparent",
                borderRadius: 6, marginBottom: 2, cursor: "pointer", display: "flex",
                justifyContent: "space-between", alignItems: "center",
                border: selectedScript?.id === s.id ? `1px solid ${COLORS.borderLight}` : "1px solid transparent",
                transition: "all .15s"
              }}>
                <div>
                  <div style={{ fontSize: 12, color: COLORS.text, fontWeight: 600, fontFamily: "monospace" }}>{s.name}</div>
                  <div style={{ fontSize: 10, color: COLORS.textDim, marginTop: 2 }}>{s.desc}</div>
                </div>
                <div onClick={e => {
                  e.stopPropagation();
                  send("toggle_script", { id: s.id }); // 後端切換 enabled 後會回傳 script_toggled 事件來更新畫面
                }} style={{
                  width: 36, height: 18, borderRadius: 9, cursor: "pointer", position: "relative",
                  background: s.enabled ? COLORS.success : COLORS.textMuted, transition: "all .2s"
                }}>
                  <div style={{
                    width: 14, height: 14, borderRadius: "50%", background: "#fff",
                    position: "absolute", top: 2, left: s.enabled ? 20 : 2, transition: "all .2s"
                  }} />
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>

      {/* Code editor */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", padding: 12 }}>
        {selectedScript ? (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div>
                <span style={{ fontSize: 14, fontWeight: 700, color: COLORS.text, fontFamily: "monospace" }}>
                  {selectedScript.name}
                </span>
                <span style={{
                  marginLeft: 8, fontSize: 10, padding: "2px 8px", borderRadius: 10,
                  background: selectedScript.type === "indicator" ? "rgba(59,130,246,0.15)" : "rgba(245,158,11,0.15)",
                  color: selectedScript.type === "indicator" ? COLORS.accent : COLORS.warn
                }}>{selectedScript.type === "indicator" ? "指標" : "策略"}</span>
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <button style={{
                  padding: "4px 14px", background: "rgba(34,197,94,0.1)", border: `1px solid ${COLORS.success}`,
                  color: COLORS.success, borderRadius: 4, fontSize: 11, cursor: "pointer"
                }}>▶ 執行</button>
                <button style={{
                  padding: "4px 14px", background: "rgba(59,130,246,0.1)", border: `1px solid ${COLORS.accent}`,
                  color: COLORS.accent, borderRadius: 4, fontSize: 11, cursor: "pointer"
                }}>💾 儲存</button>
              </div>
            </div>
            <textarea value={editCode} onChange={e => setEditCode(e.target.value)} spellCheck={false} style={{
              flex: 1, background: "#0d1117", border: `1px solid ${COLORS.border}`, borderRadius: 6,
              padding: 12, color: "#c9d1d9", fontFamily: "'Fira Code', 'SF Mono', monospace", fontSize: 12,
              lineHeight: 1.6, resize: "none", outline: "none", tabSize: 4
            }} />
          </>
        ) : (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: COLORS.textMuted }}>
            <div style={{ textAlign: "center" }}>
              <div style={{ fontSize: 40, marginBottom: 8 }}>📜</div>
              <div style={{ fontSize: 13 }}>選擇一個 Script 來編輯</div>
              <div style={{ fontSize: 11, marginTop: 4, color: COLORS.textMuted }}>支援技術指標 & 交易策略</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Database Page ───────────────────────────────────────────────────
function DatabasePage({ send, addHandler }) {
  const [downloading, setDownloading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [progress, setProgress] = useState(null); // { current, total, filename, bars_so_far }
  const [logs, setLogs] = useState([]);
  const [summary, setSummary] = useState([]);
  const [importDir, setImportDir] = useState("data/raw/taifex");
  const [selectedSymbols, setSelectedSymbols] = useState(["TX"]);
  const logsEndRef = useRef(null);

  const SYMBOL_OPTIONS = [
    { id: "TX", label: "TX", desc: "臺股期貨（大台）" },
    { id: "MTX", label: "MTX", desc: "小型臺指（小台）" },
    { id: "TMF", label: "TMF", desc: "微型臺指期貨" },
  ];

  const toggleSymbol = (id) =>
    setSelectedSymbols(prev =>
      prev.includes(id) ? prev.filter(s => s !== id) : [...prev, id]
    );

  const addLog = (msg, type = "info") =>
    setLogs(l => [...l, { time: new Date().toLocaleTimeString("zh-TW"), msg, type }]);

  // 自動捲到最新日誌
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  // 初始化: 載入 DB 摘要並註冊 WS 回調
  useEffect(() => {
    send("db_summary", {});

    const cleanups = [
      addHandler("db_summary", (msg) => {
        const data = msg.data || [];
        setSummary(data);
        if (data.length > 0) {
          addLog(`資料庫已載入 — ${data.length} 個商品/週期`, "info");
          data.forEach(d =>
            addLog(`  ${d.symbol} ${d.timeframe}: ${d.count.toLocaleString()} 筆　(${d.start?.slice(0, 10)} ~ ${d.end?.slice(0, 10)})`, "info")
          );
        } else {
          addLog("資料庫空白，請先匯入期交所 CSV 或從券商同步", "info");
        }
      }),

      addHandler("import_progress", (msg) => {
        setProgress({
          current: msg.current,
          total: msg.total,
          filename: msg.filename,
          bars_so_far: msg.bars_so_far,   // 匯入模式
          skipped: msg.skipped,           // 下載模式
        });
      }),

      addHandler("import_result", (msg) => {
        setDownloading(false);
        setImporting(false);
        setProgress(null);
        if (msg.source === "download") {
          const total = (msg.downloaded ?? 0) + (msg.skipped ?? 0);
          addLog(
            `下載完成 ✓ — 共 ${total} 個 ZIP，新下載 ${msg.downloaded ?? 0} 個，已快取略過 ${msg.skipped ?? 0} 個`,
            "success"
          );
          addLog(`儲存位置: ${msg.save_dir}`, "info");
        } else if (msg.parsed !== undefined) {
          const dupNote = msg.inserted < msg.parsed
            ? `（${(msg.parsed - msg.inserted).toLocaleString()} 筆已存在略過）`
            : "";
          addLog(`匯入完成 ✓ — 解析 ${msg.parsed.toLocaleString()} 筆，新增 ${msg.inserted.toLocaleString()} 筆 ${dupNote}`, "success");
          setSummary(msg.summary || []);
        } else {
          addLog(`操作失敗，請確認來源是否正確`, "error");
        }
      }),

      addHandler("broker_sync_result", (msg) => {
        setSyncing(false);
        if (msg.success) {
          addLog(`券商同步完成 ✓ — 共 ${msg.total.toLocaleString()} 筆`, "success");
          Object.entries(msg.results || {}).forEach(([k, v]) => {
            if (v > 0) addLog(`  ${k}: +${v} 筆`, "info");
          });
          setSummary(msg.summary || []);
        } else {
          addLog(`券商同步失敗: ${msg.message}`, "error");
        }
      }),
    ];

    return () => cleanups.forEach(fn => fn());
  }, [send, addHandler]);

  const startDownload = () => {
    setDownloading(true);
    addLog(`連線至期交所網站，下載近 30 個交易日行情 ZIP... (${selectedSymbols.join(", ")})`, "info");
    send("import_taifex", { source: "download", symbols: selectedSymbols });
  };

  const startImport = () => {
    setImporting(true);
    addLog(`匯入本地 ZIP/CSV: ${importDir} (${selectedSymbols.join(", ")})`, "info");
    send("import_taifex", { source: "local", directory: importDir, symbols: selectedSymbols });
  };

  const startBrokerSync = () => {
    setSyncing(true);
    addLog("從券商 API 同步歷史資料 (TX, MTX, TE — 日K/小時K)...", "info");
    send("broker_sync", {
      symbols: ["TX", "MTX", "TE"],
      timeframes: ["1d", "1h", "15m"],
      count: 200,
    });
  };

  const refreshSummary = () => {
    addLog("重新整理資料庫統計...", "info");
    send("db_summary", {});
  };

  const busy = downloading || importing || syncing;

  return (
    <div style={{ padding: 20, maxWidth: 960, margin: "0 auto", height: "100%", overflowY: "auto" }}>
      <h2 style={{ color: COLORS.text, fontSize: 18, fontWeight: 700, marginBottom: 16 }}>期貨資料庫管理</h2>

      {/* ─── 操作按鈕 ─── */}
      <div style={{ display: "flex", gap: 12, marginBottom: 20 }}>
        {[
          {
            label: "期交所下載", icon: "⬇",
            desc: "從期交所網站下載近 30 個交易日行情 ZIP",
            action: startDownload,
            active: downloading,
            color: COLORS.warn,
          },
          {
            label: "匯入本地 CSV", icon: "📂",
            desc: "解析下方目錄中的 .csv 檔案",
            action: startImport,
            active: importing,
            color: COLORS.accent,
          },
          {
            label: "券商同步", icon: "↻",
            desc: "從已連線券商 API 取得歷史 K 棒",
            action: startBrokerSync,
            active: syncing,
            color: COLORS.accent,
          },
          {
            label: "重新整理", icon: "⟳",
            desc: "重新讀取資料庫統計",
            action: refreshSummary,
            active: false,
            color: COLORS.success,
          },
        ].map((item) => (
          <button key={item.label} onClick={item.action} disabled={busy} style={{
            flex: 1, padding: "14px 12px",
            background: item.active ? `${item.color}18` : COLORS.bgCard,
            border: `1px solid ${item.active ? item.color : COLORS.border}`,
            borderRadius: 8, cursor: busy ? "not-allowed" : "pointer",
            textAlign: "left", opacity: busy && !item.active ? 0.5 : 1,
            transition: "all .15s",
          }}>
            <div style={{ fontSize: 18, color: item.color, marginBottom: 5 }}>{item.icon}</div>
            <div style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
              {item.active ? `${item.label}中...` : item.label}
            </div>
            <div style={{ color: COLORS.textDim, fontSize: 11, marginTop: 2 }}>{item.desc}</div>
          </button>
        ))}
      </div>

      {/* ─── 進度條 ─── */}
      {(downloading || importing) && (
        <div style={{
          marginBottom: 12, padding: "10px 14px",
          background: COLORS.bgCard, border: `1px solid ${COLORS.border}`, borderRadius: 8,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ color: COLORS.textDim, fontSize: 11 }}>
              {progress
                ? `處理中: ${progress.filename}`
                : downloading ? "連線期交所網站，取得下載清單..." : "掃描目錄..."}
            </span>
            <span style={{ color: COLORS.textMuted, fontSize: 11 }}>
              {progress ? `${progress.current} / ${progress.total}` : ""}
            </span>
          </div>
          <div style={{ height: 6, background: COLORS.border, borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              height: "100%", borderRadius: 3,
              background: downloading ? COLORS.warn : COLORS.accent,
              width: progress && progress.total > 0
                ? `${Math.round((progress.current / progress.total) * 100)}%`
                : "0%",
              transition: "width 0.3s ease",
            }} />
          </div>
          {progress && progress.bars_so_far !== undefined && (
            <div style={{ marginTop: 4, fontSize: 10, color: COLORS.textMuted }}>
              已解析 {progress.bars_so_far.toLocaleString()} 筆 K 線
            </div>
          )}
          {progress && progress.skipped !== undefined && (
            <div style={{ marginTop: 4, fontSize: 10, color: COLORS.textMuted }}>
              {progress.skipped ? "已下載，略過" : "下載中..."}
            </div>
          )}
        </div>
      )}

      {/* ─── 商品選擇 ─── */}
      <div style={{
        padding: "10px 14px", marginBottom: 12,
        background: COLORS.bgCard, border: `1px solid ${COLORS.border}`, borderRadius: 8,
      }}>
        <div style={{ fontSize: 11, color: COLORS.textDim, marginBottom: 8, fontWeight: 600 }}>
          匯入商品
          <span style={{ color: COLORS.textMuted, fontWeight: 400, marginLeft: 8 }}>（期交所下載 & 本地匯入 & 券商同步 共用）</span>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {SYMBOL_OPTIONS.map(({ id, label, desc }) => {
            const on = selectedSymbols.includes(id);
            return (
              <button key={id} onClick={() => toggleSymbol(id)} disabled={busy} style={{
                padding: "4px 12px", borderRadius: 4, fontSize: 11, cursor: busy ? "not-allowed" : "pointer",
                background: on ? "rgba(59,130,246,0.15)" : "transparent",
                border: `1px solid ${on ? COLORS.accent : COLORS.border}`,
                color: on ? COLORS.accent : COLORS.textDim,
                transition: "all .15s",
              }}>
                <span style={{ fontWeight: 700 }}>{label}</span>
                <span style={{ marginLeft: 4, color: on ? COLORS.textDim : COLORS.textMuted }}>{desc}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* ─── 期交所目錄輸入 ─── */}
      <div style={{
        display: "flex", alignItems: "center", gap: 8, marginBottom: 16,
        padding: "10px 14px", background: COLORS.bgCard,
        border: `1px solid ${COLORS.border}`, borderRadius: 8,
      }}>
        <span style={{ color: COLORS.textDim, fontSize: 11, whiteSpace: "nowrap" }}>CSV 目錄:</span>
        <input
          value={importDir}
          onChange={e => setImportDir(e.target.value)}
          disabled={busy}
          style={{
            flex: 1, background: "transparent", border: "none", outline: "none",
            color: COLORS.text, fontSize: 12, fontFamily: "monospace",
          }}
          placeholder="data/raw/taifex"
        />
        <span style={{ color: COLORS.textMuted, fontSize: 10 }}>（放置期交所手動下載的 .csv 檔案）</span>
      </div>

      {/* ─── 資料庫統計 ─── */}
      {summary.length > 0 && (
        <div style={{
          marginBottom: 16, background: COLORS.bgCard,
          border: `1px solid ${COLORS.border}`, borderRadius: 8, overflow: "hidden",
        }}>
          <div style={{
            padding: "8px 14px", borderBottom: `1px solid ${COLORS.border}`,
            fontSize: 11, color: COLORS.textDim, fontWeight: 600,
          }}>資料庫統計</div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
            <thead>
              <tr style={{ background: "rgba(255,255,255,0.02)" }}>
                {["商品", "週期", "筆數", "最早", "最新"].map(h => (
                  <th key={h} style={{
                    padding: "6px 14px", textAlign: "left",
                    color: COLORS.textDim, fontWeight: 600,
                    borderBottom: `1px solid ${COLORS.border}`,
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {summary.map((d, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                  <td style={{ padding: "6px 14px", color: COLORS.warn, fontWeight: 600 }}>{d.symbol}</td>
                  <td style={{ padding: "6px 14px", color: COLORS.textDim }}>{d.timeframe}</td>
                  <td style={{ padding: "6px 14px", color: COLORS.text }}>{d.count.toLocaleString()}</td>
                  <td style={{ padding: "6px 14px", color: COLORS.textMuted, fontFamily: "monospace" }}>{d.start?.slice(0, 10)}</td>
                  <td style={{ padding: "6px 14px", color: COLORS.textMuted, fontFamily: "monospace" }}>{d.end?.slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ─── 操作日誌 ─── */}
      <div style={{
        background: "#0d1117", border: `1px solid ${COLORS.border}`, borderRadius: 8,
        padding: 12, maxHeight: 260, overflowY: "auto", fontFamily: "monospace", fontSize: 11,
      }}>
        {logs.length === 0 && (
          <span style={{ color: COLORS.textMuted }}>連線中，等待後端回應...</span>
        )}
        {logs.map((l, i) => (
          <div key={i} style={{ padding: "2px 0", display: "flex", gap: 10 }}>
            <span style={{ color: COLORS.textMuted, flexShrink: 0 }}>{l.time}</span>
            <span style={{
              color: l.type === "success" ? COLORS.success
                : l.type === "error" ? COLORS.danger
                  : COLORS.textDim,
            }}>{l.msg}</span>
          </div>
        ))}
        <div ref={logsEndRef} />
      </div>
    </div>
  );
}

// ─── Backtest Page ───────────────────────────────────────────────────
function BacktestPage({ scripts }) {
  const [selectedStrategy, setSelectedStrategy] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);

  const strategies = scripts.filter(s => s.type === "strategy");

  const runBacktest = () => {
    setRunning(true);
    setTimeout(() => {
      setResult({
        totalReturn: 18.7, maxDrawdown: -6.2, sharpe: 1.42, winRate: 62.5,
        totalTrades: 148, profitFactor: 1.85,
        equity: Array.from({ length: 100 }, (_, i) => ({
          x: i, y: 1000000 + (Math.random() - 0.42) * 30000 * Math.sqrt(i + 1) + i * 2000
        }))
      });
      setRunning(false);
    }, 2000);
  };

  return (
    <div style={{ padding: 20, maxWidth: 1000, margin: "0 auto" }}>
      <h2 style={{ color: COLORS.text, fontSize: 18, fontWeight: 700, marginBottom: 16 }}>🔬 交易回測中心</h2>

      <div style={{
        display: "flex", gap: 12, marginBottom: 20, padding: 16, background: COLORS.bgCard,
        borderRadius: 8, border: `1px solid ${COLORS.border}`, alignItems: "flex-end"
      }}>
        <div style={{ flex: 1 }}>
          <label style={{ fontSize: 11, color: COLORS.textDim, display: "block", marginBottom: 4 }}>策略 Script</label>
          <select value={selectedStrategy} onChange={e => setSelectedStrategy(e.target.value)} style={{
            width: "100%", padding: "8px 10px", background: COLORS.bg, border: `1px solid ${COLORS.border}`,
            borderRadius: 4, color: COLORS.text, fontSize: 12, outline: "none"
          }}>
            <option value="">選擇策略...</option>
            {strategies.map(s => <option key={s.id} value={s.name}>{s.name} — {s.desc}</option>)}
          </select>
        </div>
        <div>
          <label style={{ fontSize: 11, color: COLORS.textDim, display: "block", marginBottom: 4 }}>商品</label>
          <select style={{
            padding: "8px 10px", background: COLORS.bg, border: `1px solid ${COLORS.border}`,
            borderRadius: 4, color: COLORS.text, fontSize: 12, outline: "none"
          }}>
            <option>TX 台指期</option>
            <option>MTX 小台指</option>
          </select>
        </div>
        <div>
          <label style={{ fontSize: 11, color: COLORS.textDim, display: "block", marginBottom: 4 }}>區間</label>
          <select style={{
            padding: "8px 10px", background: COLORS.bg, border: `1px solid ${COLORS.border}`,
            borderRadius: 4, color: COLORS.text, fontSize: 12, outline: "none"
          }}>
            <option>近一年</option>
            <option>近三年</option>
            <option>全部資料</option>
          </select>
        </div>
        <button onClick={runBacktest} disabled={!selectedStrategy || running} style={{
          padding: "8px 24px", background: running ? COLORS.textMuted : `linear-gradient(135deg, ${COLORS.accent}, #6366f1)`,
          border: "none", borderRadius: 6, color: "#fff", fontSize: 13, fontWeight: 700,
          cursor: !selectedStrategy || running ? "not-allowed" : "pointer", whiteSpace: "nowrap"
        }}>{running ? "⏳ 執行中..." : "▶ 開始回測"}</button>
      </div>

      {result && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 10, marginBottom: 20 }}>
            {[
              { label: "總報酬", value: `${result.totalReturn}%`, color: COLORS.up },
              { label: "最大回撤", value: `${result.maxDrawdown}%`, color: COLORS.down },
              { label: "Sharpe", value: result.sharpe.toFixed(2), color: COLORS.accent },
              { label: "勝率", value: `${result.winRate}%`, color: COLORS.warn },
              { label: "總交易", value: result.totalTrades, color: COLORS.text },
              { label: "盈虧比", value: result.profitFactor.toFixed(2), color: COLORS.up },
            ].map((m, i) => (
              <div key={i} style={{
                padding: 12, background: COLORS.bgCard, borderRadius: 8,
                border: `1px solid ${COLORS.border}`, textAlign: "center"
              }}>
                <div style={{ fontSize: 10, color: COLORS.textDim, marginBottom: 4 }}>{m.label}</div>
                <div style={{ fontSize: 18, fontWeight: 700, color: m.color, fontFamily: "monospace" }}>{m.value}</div>
              </div>
            ))}
          </div>

          {/* Equity curve */}
          <div style={{
            padding: 16, background: COLORS.bgCard, borderRadius: 8,
            border: `1px solid ${COLORS.border}`, height: 280
          }}>
            <div style={{ fontSize: 12, color: COLORS.textDim, fontWeight: 600, marginBottom: 8 }}>權益曲線</div>
            <svg viewBox="0 0 800 220" style={{ width: "100%", height: "calc(100% - 24px)" }}>
              <defs>
                <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={COLORS.up} stopOpacity="0.3" />
                  <stop offset="100%" stopColor={COLORS.up} stopOpacity="0" />
                </linearGradient>
              </defs>
              {[0, 1, 2, 3, 4].map(i => (
                <line key={i} x1="0" y1={i * 55} x2="800" y2={i * 55} stroke={COLORS.border} strokeWidth="0.5" />
              ))}
              <path d={
                result.equity.map((p, i) => {
                  const x = (i / (result.equity.length - 1)) * 800;
                  const minY = Math.min(...result.equity.map(e => e.y));
                  const maxY = Math.max(...result.equity.map(e => e.y));
                  const y = 210 - ((p.y - minY) / (maxY - minY)) * 200;
                  return `${i === 0 ? "M" : "L"} ${x} ${y}`;
                }).join(" ")
              } fill="none" stroke={COLORS.up} strokeWidth="2" />
              <path d={
                result.equity.map((p, i) => {
                  const x = (i / (result.equity.length - 1)) * 800;
                  const minY = Math.min(...result.equity.map(e => e.y));
                  const maxY = Math.max(...result.equity.map(e => e.y));
                  const y = 210 - ((p.y - minY) / (maxY - minY)) * 200;
                  return `${i === 0 ? "M" : "L"} ${x} ${y}`;
                }).join(" ") + " L 800 220 L 0 220 Z"
              } fill="url(#eqGrad)" />
            </svg>
          </div>
        </>
      )}
    </div>
  );
}

// ─── Options T-Quote Component ───────────────────────────────────────
const MOCK_OPTIONS_DATA = [
  { strike: 46200, callPrice: 710, callChange: 25, putPrice: 185, putChange: -55 },
  { strike: 46250, callPrice: 670, callChange: 20, putPrice: 205, putChange: -52 },
  { strike: 46300, callPrice: 630, callChange: 15, putPrice: 230, putChange: -50 },
  { strike: 46350, callPrice: 590, callChange: 10, putPrice: 260, putChange: -48 },
  { strike: 46400, callPrice: 550, callChange: 5, putPrice: 290, putChange: -45 },
  { strike: 46450, callPrice: 510, callChange: 0, putPrice: 327, putChange: -44 },
  { strike: 46500, callPrice: 470, callChange: -5, putPrice: 347, putChange: -41 },
  { strike: 46550, callPrice: 435, callChange: -9, putPrice: 364, putChange: -46 },
  { strike: 46600, callPrice: 400, callChange: -12, putPrice: 383, putChange: -47 },
  { strike: 46650, callPrice: 365, callChange: -15, putPrice: 404, putChange: -50 },
  { strike: 46700, callPrice: 330, callChange: -18, putPrice: 427, putChange: -48 },
  { strike: 46750, callPrice: 300, callChange: -20, putPrice: 451, putChange: -49 },
  { strike: 46800, callPrice: 270, callChange: -22, putPrice: 479, putChange: -46 },
  { strike: 46850, callPrice: 245, callChange: -25, putPrice: 500, putChange: -50 },
  { strike: 46900, callPrice: 220, callChange: -28, putPrice: 565, putChange: -20 },
  { strike: 46950, callPrice: 200, callChange: -30, putPrice: 595, putChange: -10 },
  { strike: 47000, callPrice: 180, callChange: -32, putPrice: 590, putChange: -45 },
];

function OptionsTQuote({ currentPrice = 46465, onClose }) {
  const scrollRef = useRef(null);
  const [selectedContract, setSelectedContract] = useState("2026/06F3");

  // Auto-scroll to ATM
  useEffect(() => {
    if (scrollRef.current && MOCK_OPTIONS_DATA.length > 0) {
      const atmIndex = MOCK_OPTIONS_DATA.reduce((closestIdx, row, idx) => {
        const closestDiff = Math.abs(MOCK_OPTIONS_DATA[closestIdx].strike - currentPrice);
        const currentDiff = Math.abs(row.strike - currentPrice);
        return currentDiff < closestDiff ? idx : closestIdx;
      }, 0);

      const rowHeight = 26;
      const headerHeight = 28;
      const containerHeight = scrollRef.current.clientHeight;
      const scrollTop = (atmIndex * rowHeight) - (containerHeight / 2) + (rowHeight / 2) + headerHeight;
      scrollRef.current.scrollTop = Math.max(0, scrollTop);
    }
  }, [currentPrice]);

  const T_GRID = "45px 55px 60px 55px 45px";

  const renderValue = (val) => {
    if (val === undefined || val === null) return "--";
    return val.toFixed(1);
  };

  const renderChange = (change) => {
    if (change === undefined || change === null) return "--";
    const color = change > 0 ? COLORS.up : change < 0 ? COLORS.down : COLORS.text;
    return <span style={{ color }}>{change > 0 ? `+${change}` : change}</span>;
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: COLORS.bgCard, borderRadius: 8, border: `1px solid ${COLORS.border}` }}>
      {/* Header Info */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "6px 12px", borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: COLORS.text }}>台指選擇權</span>
          <select 
            value={selectedContract}
            onChange={(e) => setSelectedContract(e.target.value)}
            style={{
              background: "transparent", border: `1px solid rgba(255,255,255,0.1)`, color: COLORS.text,
              fontSize: 12, fontWeight: 600, padding: "2px 4px", borderRadius: 4, outline: "none", cursor: "pointer"
            }}
          >
            <option value="2026/06F3">2026/06F3</option>
            <option value="2026/06W4">2026/06W4</option>
            <option value="2026/06F4">2026/06F4</option>
            <option value="2026/07W1">2026/07W1</option>
            <option value="2026/07">2026/07</option>
            <option value="2026/08">2026/08</option>
          </select>
          <span style={{ fontSize: 10, color: COLORS.textDim, background: "rgba(255,255,255,0.05)", padding: "2px 6px", borderRadius: 4 }}>剩 5 天</span>
        </div>
        {onClose && (
          <button onClick={onClose} style={{ background: "none", border: "none", color: COLORS.textDim, cursor: "pointer", fontSize: 14 }}>✕</button>
        )}
      </div>
      
      {/* Underlying Info */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 12, padding: "6px", background: COLORS.bgPanel, borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0, fontSize: 11 }}>
        <span style={{ color: COLORS.textMuted }}>加權指數</span>
        <span style={{ color: COLORS.up, fontWeight: 700 }}>46465.20</span>
        <span style={{ color: COLORS.up }}>587.81 ▲</span>
        <span style={{ color: COLORS.up }}>1.28%</span>
      </div>

      {/* Main Headers */}
      <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0, background: COLORS.bgPanel }}>
        <div style={{ width: "40%", textAlign: "center", color: COLORS.up, fontWeight: 700, fontSize: 12 }}>買權 Call</div>
        <div style={{ width: "20%", textAlign: "center", color: "#facc15", fontWeight: 700, fontSize: 11, border: `1px solid rgba(250,204,21,0.5)`, borderRadius: 4, background: "rgba(250,204,21,0.1)" }}>
          {selectedContract.split('/')[1] || "06F3"}
        </div>
        <div style={{ width: "40%", textAlign: "center", color: COLORS.down, fontWeight: 700, fontSize: 12 }}>賣權 Put</div>
      </div>

      {/* Column Headers */}
      <div style={{ display: "grid", gridTemplateColumns: T_GRID, borderBottom: `1px solid ${COLORS.border}`, padding: "4px 0", flexShrink: 0, background: COLORS.bgPanel, fontSize: 10, color: COLORS.textMuted, fontWeight: 600 }}>
        <div style={{ textAlign: "center" }}>漲跌</div>
        <div style={{ textAlign: "center" }}>成交價</div>
        <div style={{ textAlign: "center" }}>履約價</div>
        <div style={{ textAlign: "center" }}>成交價</div>
        <div style={{ textAlign: "center" }}>漲跌</div>
      </div>

      {/* Rows */}
      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", overflowX: "hidden" }}>
        {MOCK_OPTIONS_DATA.map((row) => {
          const diffToAtm = Math.abs(row.strike - currentPrice);
          const isAtm = diffToAtm < 25;

          return (
            <div key={row.strike} style={{ display: "grid", gridTemplateColumns: T_GRID, alignItems: "center", height: 26, borderBottom: `1px solid ${COLORS.border}15`, fontSize: 11, fontFamily: "monospace", fontWeight: 600 }}>
              <div style={{ textAlign: "center", paddingRight: 4 }}>{renderChange(row.callChange)}</div>
              <div style={{ textAlign: "center", color: COLORS.up }}>{renderValue(row.callPrice)}</div>
              <div style={{ textAlign: "center", background: isAtm ? "rgba(250,204,21,0.15)" : "rgba(255,255,255,0.03)", color: isAtm ? "#facc15" : COLORS.text, borderLeft: `1px solid ${COLORS.border}`, borderRight: `1px solid ${COLORS.border}` }}>{row.strike}</div>
              <div style={{ textAlign: "center", color: COLORS.down }}>{renderValue(row.putPrice)}</div>
              <div style={{ textAlign: "center", paddingLeft: 4 }}>{renderChange(row.putChange)}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Main App ────────────────────────────────────────────────────────
export default function TradingPlatform() {
  const [page, setPage] = useState("trading");
  const [klineData, setKlineData] = useState([]);
  const [chartSymbol, setChartSymbol] = useState("TX");
  const [orderSymbol, setOrderSymbol] = useState("TX");
  const [latestPrices, setLatestPrices] = useState({});
  const [orderbooks, setOrderbooks] = useState({});
  const [scripts, setScripts] = useState([]);
  const wsUrl = `ws://${window.location.host}/ws`;
  const { send, addHandler, connected } = useWebSocket(wsUrl);
  const [showBrokerConfig, setShowBrokerConfig] = useState(false);
  const [showTQuote, setShowTQuote] = useState(true);
  const [brokerConfig, setBrokerConfig] = useState({
    quote: { name: "未連線", connected: false },
    trade: { name: "未連線", connected: false }
  });
  // 各 Script 即時運算結果（由後端 ScriptEngine 算完透過 ws "indicator_output" 廣播），用 name 當 key
  const [indicatorOutputs, setIndicatorOutputs] = useState({});
  const [candleColorScheme, setCandleColorScheme] = useState("green-up");

  // 成交量水平警示線：來自 Volume_Alert Script 的運算結果（取代舊版 /api/config 的寫死設定）
  // 後端尚未回傳前先用預設值，避免圖表開頭沒有參考線
  const volumeRefLines = useMemo(() => {
    const output = indicatorOutputs["Volume_Alert"];
    if (!output) {
      return [{ level: 1500, label: "爆大量" }, { level: 400, label: "大量" }];
    }
    return Object.entries(output.series)
      .map(([label, values]) => ({ level: values[values.length - 1], label }))
      .sort((a, b) => b.level - a.level);
  }, [indicatorOutputs]);

  useEffect(() => {
    fetch("/api/scripts")
      .then(r => r.json())
      .then(cfg => {
        if (Array.isArray(cfg.scripts)) setScripts(cfg.scripts);
      })
      .catch(() => { }); // 取不到就維持空清單
  }, []);

  useEffect(() => {
    fetch("/api/config")
      .then(r => r.json())
      .then(cfg => {
        if (cfg.candle_color_scheme) {
          setCandleColorScheme(cfg.candle_color_scheme);
        }
      })
      .catch(() => { }); // 取不到設定就沿用預設值
  }, []);

  // 在每次渲染前套用漲跌顏色慣例，讓所有子元件（K棒、損益、多空標籤…）讀到一致的顏色
  applyCandleColorScheme(candleColorScheme);

  useEffect(() => {
    const cleanup = addHandler("script_toggled", (msg) => {
      setScripts(ss => ss.map(s => s.id === msg.id ? { ...s, enabled: msg.enabled } : s));
    });
    return cleanup;
  }, [addHandler]);

  useEffect(() => {
    const cleanup = addHandler("indicator_output", (msg) => {
      setIndicatorOutputs(prev => ({ ...prev, [msg.name]: msg }));
    });
    return cleanup;
  }, [addHandler]);

  useEffect(() => {
    if (!connected) return;
    send("broker_status", {});
  }, [connected, send]);

  useEffect(() => {
    const handle1 = addHandler("broker_status", (msg) => {
      setBrokerConfig({
        quote: { name: msg.quote?.name || "未連線", connected: msg.quote?.connected || false },
        trade: { name: msg.trade?.name || "未連線", connected: msg.trade?.connected || false },
      });
    });
    const handle2 = addHandler("broker_status_update", (msg) => {
      if (msg.kind === "quote" || msg.kind === "trade") {
        setBrokerConfig(prev => ({
          ...prev,
          [msg.kind]: { name: msg.name, connected: msg.connected }
        }));
      }
    });
    return () => { handle1(); handle2(); };
  }, [addHandler]);
  const [clock, setClock] = useState("");

  // Lifted order state (shared between OrderPanel and PositionOrdersPanel)
  const [myBuyOrders, setMyBuyOrders] = useState({});
  const [mySellOrders, setMySellOrders] = useState({});
  const [stopBuys, setStopBuys] = useState({});
  const [stopSells, setStopSells] = useState({});
  const [timeframe, setTimeframe] = useState("15"); // K棒時間週期
  const [visibleCount, setVisibleCount] = useState(60);
  const [offset, setOffset] = useState(0);
  const [globalTooltip, setGlobalTooltip] = useState(null);

  const enabledIndicators = scripts.filter(s => s.type === "indicator" && s.enabled).map(s => s.name);

  useEffect(() => {
    const tick = () => setClock(new Date().toLocaleTimeString("zh-TW"));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  // 連線成功、切換商品或切換週期時拉資料 + 訂閱即時報價
  const [rawM1, setRawM1] = useState([]);
  const [liveM1Bar, setLiveM1Bar] = useState(null);
  const liveM1BarRef = useRef(null);
  useEffect(() => { liveM1BarRef.current = liveM1Bar; }, [liveM1Bar]);
  // 切換商品時重置即時棒
  useEffect(() => { setLiveM1Bar(null); }, [chartSymbol]);

  // 成交量語音提示：當前棒的量跨越設定的水平線時播報一次
  // （同一根棒內每個門檻只播一次，新的一根棒開始後重新計算）
  // 限定 liveM1Bar 有值（代表正在收即時報價、追蹤的是目前進行中的這根K棒）才檢查，
  // 避免剛切換商品/週期、或頁面剛拉到歷史資料時，對著早已收完的舊K棒誤報「爆量」。
  const volumeAlertRef = useRef({ time: null, levels: new Set() });

  useEffect(() => {
    if (!liveM1Bar) return;
    if (!klineData.length || !volumeRefLines.length) return;
    const last = klineData[klineData.length - 1];
    if (!last) return;

    if (volumeAlertRef.current.time !== last.time) {
      volumeAlertRef.current = { time: last.time, levels: new Set() };
    }

    // 由小到大檢查，量持續攀升時依序播報「大量」→「爆大量」
    [...volumeRefLines].sort((a, b) => a.level - b.level).forEach(({ level, label }) => {
      if (last.volume >= level && !volumeAlertRef.current.levels.has(level)) {
        volumeAlertRef.current.levels.add(level);
        speakVolumeAlert(label);
      }
    });
  }, [klineData, volumeRefLines, liveM1Bar]);

  useEffect(() => {
    if (!connected) return;
    // 訂閱即時報價（後端 QuoteModule 已連線才有效，未連線也不影響）
    send("subscribe", { symbol: chartSymbol });

    const isLarge = ["日", "周", "月"].includes(timeframe);
    let reqCount = 2000;
    if (!isLarge) {
      const minutes = { "1": 1, "3": 3, "15": 15, "60": 60 }[timeframe] || 1;
      reqCount = 1500 * minutes;
    } else {
      reqCount = 500;
    }
    send("get_history", {
      symbol: chartSymbol,
      timeframe,
      count: reqCount,
    });
  }, [connected, chartSymbol, timeframe]);

  useEffect(() => {
    if (!connected) return;
    send("subscribe", { symbol: orderSymbol });
    send("get_history", { symbol: orderSymbol, timeframe: "1", count: 1 });
  }, [connected, orderSymbol]);

  // 後端回傳歷史資料
  useEffect(() => {
    return addHandler("history_bars", (msg) => {
      if (msg.bars && msg.bars.length > 0) {
        const lastClose = msg.bars[msg.bars.length - 1].close;
        setLatestPrices(prev => ({ ...prev, [msg.symbol]: lastClose }));

        if (msg.symbol === chartSymbol) {
          if (["日", "周", "月"].includes(msg.timeframe)) {
            setKlineData(msg.bars);
            setRawM1([]);
          } else {
            setRawM1(msg.bars);
          }
        }
      }
    });
  }, [addHandler, chartSymbol]);

  // 即時 tick 事件（券商原始即時報價，更新最新價格）
  useEffect(() => {
    return addHandler("tick", (msg) => {
      setLatestPrices(prev => ({ ...prev, [msg.symbol]: msg.price }));
    });
  }, [addHandler]);

  // 五檔報價
  useEffect(() => {
    return addHandler("orderbook", (msg) => {
      console.log("orderbook received", msg);
      setOrderbooks(prev => ({ ...prev, [msg.symbol]: msg }));
    });
  }, [addHandler]);

  // 即時 bar 事件（BarBuilder 推送 K 棒）
  // is_closed=true  → 已收完的 K 棒，更新歷史資料
  // is_closed=false → 正在成形的 M1 即時棒，更新 liveM1Bar
  useEffect(() => {
    return addHandler("bar", (msg) => {
      setLatestPrices(prev => ({ ...prev, [msg.symbol]: msg.close }));
      if (msg.symbol !== chartSymbol) return;

      const barTimeMs = new Date(msg.timestamp).getTime();

      if (["日", "周", "月"].includes(timeframe)) {
        // 日/周/月K 只接收已收完的大週期棒
        if (!["1d", "1w", "1M"].includes(msg.timeframe)) return;
        if (!msg.is_closed) return;
        const newBar = {
          time: barTimeMs,
          open: msg.open, high: msg.high, low: msg.low, close: msg.close,
          volume: msg.volume, delivery: msg.delivery ?? "",
        };
        setKlineData(prev => {
          if (!prev.length) return [newBar];
          const last = prev[prev.length - 1];
          if (last.time === barTimeMs) {
            const updated = [...prev];
            updated[updated.length - 1] = {
              ...last,
              high: Math.max(last.high, newBar.high),
              low: Math.min(last.low, newBar.low),
              close: newBar.close,
              volume: Math.max(last.volume, newBar.volume),
            };
            return updated;
          }
          return [...prev, newBar].slice(-1500);
        });
      } else {
        // 分鐘K 只處理 M1 棒
        if (msg.timeframe !== "1m") return;

        if (!msg.is_closed) {
          // 即時棒：更新 liveM1Bar，供 liveM1Bar effect 更新 klineData
          setLiveM1Bar({
            time: barTimeMs,
            open: msg.open, high: msg.high, low: msg.low,
            close: msg.close, volume: msg.volume,
          });
          return;
        }

        // 已收完的 M1 棒：加入 rawM1，觸發聚合 useEffect 重算
        setRawM1(prev => {
          const lastMs = prev.length ? prev[prev.length - 1].time : 0;
          if (barTimeMs <= lastMs) return prev;
          return [...prev, {
            time: barTimeMs,
            open: msg.open, high: msg.high, low: msg.low, close: msg.close,
            volume: msg.volume, delivery: msg.delivery ?? "",
          }].slice(-5000);
        });
      }
    });
  }, [addHandler, chartSymbol, timeframe]);

  // liveM1Bar 變化時更新 klineData 當前棒（取代舊的 tick handler）
  // liveM1Bar 來自 BarBuilder 推送的 is_closed=false M1 棒，OHLCV 完全正確
  useEffect(() => {
    if (!liveM1Bar) return;

    if (["日", "周", "月"].includes(timeframe)) {
      // 日週月：用最新 M1 棒的收盤更新當前棒
      setKlineData(prev => {
        if (!prev.length) return prev;
        const updated = [...prev];
        const last = updated[updated.length - 1];
        updated[updated.length - 1] = {
          ...last,
          high: Math.max(last.high, liveM1Bar.high),
          low: Math.min(last.low, liveM1Bar.low),
          close: liveM1Bar.close,
        };
        return updated;
      });
      return;
    }

    const minutes = { "1": 1, "3": 3, "15": 15, "60": 60 }[timeframe];
    if (!minutes) return;
    const periodMs = minutes * 60 * 1000;
    const bucketTime = Math.floor(liveM1Bar.time / periodMs) * periodMs;

    setKlineData(prev => {
      if (!prev.length) return prev;
      const last = prev[prev.length - 1];

      if (bucketTime === last.time) {
        const updated = [...prev];
        updated[updated.length - 1] = {
          ...last,
          high: Math.max(last.high, liveM1Bar.high),
          low: Math.min(last.low, liveM1Bar.low),
          close: liveM1Bar.close,
          volume: Math.max(last.volume, liveM1Bar.volume),
        };
        return updated;
      } else if (bucketTime > last.time) {
        // 新週期開始
        return [...prev, {
          time: bucketTime,
          open: liveM1Bar.open,
          high: liveM1Bar.high,
          low: liveM1Bar.low,
          close: liveM1Bar.close,
          volume: liveM1Bar.volume,
          delivery: last.delivery,
        }].slice(-1500);
      }
      return prev;
    });
  }, [liveM1Bar, timeframe]);

  // M1 聚合為分鐘週期（rawM1 或 timeframe 變化時才重算，避免每個 tick 都觸發）
  useEffect(() => {
    if (!rawM1.length) return;
    const minutes = { "1": 1, "3": 3, "15": 15, "60": 60 }[timeframe];
    if (!minutes) return;
    const periodMs = minutes * 60 * 1000;
    const buckets = new Map();
    for (const b of rawM1) {
      const key = Math.floor(b.time / periodMs) * periodMs;
      if (!buckets.has(key)) {
        buckets.set(key, { time: key, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume, delivery: b.delivery });
      } else {
        const c = buckets.get(key);
        c.high = Math.max(c.high, b.high);
        c.low = Math.min(c.low, b.low);
        c.close = b.close;
        c.volume += b.volume;
      }
    }

    const aggregated = [...buckets.values()].sort((a, b) => a.time - b.time).slice(-1500);

    // 透過 ref 讀取最新的 liveM1Bar，不將其加入 deps（避免每個 tick 重新跑完整聚合）
    const live = liveM1BarRef.current;
    if (live) {
      const liveBucket = Math.floor(live.time / periodMs) * periodMs;
      const lastAgg = aggregated[aggregated.length - 1];
      if (lastAgg && liveBucket === lastAgg.time) {
        aggregated[aggregated.length - 1] = {
          ...lastAgg,
          high: Math.max(lastAgg.high, live.high),
          low: Math.min(lastAgg.low, live.low),
          close: live.close,
          volume: Math.max(lastAgg.volume, live.volume),
        };
      } else if (!lastAgg || liveBucket > lastAgg.time) {
        aggregated.push({
          time: liveBucket, open: live.open, high: live.high,
          low: live.low, close: live.close, volume: live.volume, delivery: "",
        });
      }
    }

    setKlineData(aggregated);
  }, [rawM1, timeframe]);

  const panelStyle = {
    background: COLORS.bgPanel,
    border: `1px solid ${COLORS.border}`,
    borderRadius: 8,
    overflow: "hidden",
  };

  return (
    <div style={{
      width: "100%", height: "100vh", display: "flex", flexDirection: "column",
      background: COLORS.bg, color: COLORS.text,
      fontFamily: "'Noto Sans TC', 'SF Pro Display', -apple-system, sans-serif", overflow: "hidden"
    }}>
      {/* ─── Header Bar ─────────────────────────────── */}
      <div style={{
        height: 44, display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "0 16px", background: "linear-gradient(180deg, #151c2c 0%, #111827 100%)",
        borderBottom: `1px solid ${COLORS.border}`, flexShrink: 0
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span style={{
            fontSize: 15, fontWeight: 800, letterSpacing: 1,
            background: "linear-gradient(135deg, #3b82f6, #22c55e)", WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent"
          }}>FUTURES PRO</span>
          <div style={{ display: "flex", gap: 0, background: COLORS.bgCard, borderRadius: 6, overflow: "hidden", border: `1px solid ${COLORS.border}` }}>
            {[
              ["trading", "交易"],
              ["scripts", "Scripts"],
              ["database", "資料庫"],
              ["backtest", "回測中心"],
            ].map(([id, label]) => (
              <button key={id} onClick={() => setPage(id)} style={{
                padding: "5px 16px", fontSize: 11, fontWeight: page === id ? 700 : 400,
                background: page === id ? "rgba(59,130,246,0.15)" : "transparent",
                color: page === id ? COLORS.accent : COLORS.textDim,
                border: "none", cursor: "pointer", transition: "all .15s"
              }}>{label}</button>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11 }}>
            <span style={{ color: COLORS.textDim }}>問價:</span>
            <div style={{ width: 8, height: 8, borderRadius: "50%", background: brokerConfig.quote.connected ? COLORS.success : COLORS.danger }} title={brokerConfig.quote.connected ? "已連線" : "未連線"} />
            <span style={{ color: brokerConfig.quote.connected ? COLORS.text : COLORS.warn, fontWeight: 600 }}>{brokerConfig.quote.name}</span>
            <span style={{ color: COLORS.textMuted, margin: "0 2px" }}>|</span>
            <span style={{ color: COLORS.textDim }}>交易:</span>
            <div style={{ width: 8, height: 8, borderRadius: "50%", background: brokerConfig.trade.connected ? COLORS.success : COLORS.danger }} title={brokerConfig.trade.connected ? "已連線" : "未連線"} />
            <span style={{ color: brokerConfig.trade.connected ? COLORS.text : COLORS.warn, fontWeight: 600 }}>{brokerConfig.trade.name}</span>
          </div>
          <button onClick={() => setShowBrokerConfig(true)} style={{
            padding: "4px 12px", background: "rgba(59,130,246,0.1)", border: `1px solid ${COLORS.accentDim}`,
            color: COLORS.accent, borderRadius: 4, fontSize: 11, cursor: "pointer"
          }}>⚙ 券商設定</button>
          <span style={{ fontFamily: "monospace", fontSize: 12, color: COLORS.textDim }}>{clock}</span>
        </div>
      </div>

      {/* ─── Content ──────────────────────────────── */}
      <div style={{ flex: 1, overflow: "hidden" }}>
        {page === "database" && <DatabasePage send={send} addHandler={addHandler} />}
        {page === "backtest" && <BacktestPage scripts={scripts} />}
        {page === "scripts" && (
          <div style={{ height: "100%", ...panelStyle, margin: 8, borderRadius: 8 }}>
            <ScriptsPanel scripts={scripts} send={send} activeView="scripts" />
          </div>
        )}
        {page === "trading" && (
          <div style={{ display: "flex", height: "100%", padding: 8, gap: 8 }}>
            {/* 選擇權 T字報價表 - 放在左側 */}
            {showTQuote && (
              <div style={{ width: 300, flexShrink: 0, display: "flex", flexDirection: "column" }}>
                <OptionsTQuote currentPrice={latestPrices[chartSymbol] ?? klineData[klineData.length - 1]?.close ?? 46465} onClose={() => setShowTQuote(false)} />
              </div>
            )}

            {/* Left: Technical Analysis — stacked vertically */}
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 4 }}>
              {/* Header bar with price info and timeframe selector */}
              <div style={{
                ...panelStyle, display: "flex", justifyContent: "space-between", alignItems: "center",
                padding: "0 12px", height: 34, flexShrink: 0
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  {/* T字報價切換按鈕 */}
                  <button onClick={() => setShowTQuote(!showTQuote)} style={{
                    display: "flex", alignItems: "center", gap: 4,
                    padding: "4px 8px", fontSize: 11, fontWeight: showTQuote ? 700 : 500,
                    background: showTQuote ? "rgba(59,130,246,0.15)" : "transparent",
                    border: `1px solid ${showTQuote ? COLORS.accent : COLORS.border}`,
                    color: showTQuote ? COLORS.accent : COLORS.textDim, 
                    borderRadius: 4, cursor: "pointer", transition: "all 0.15s",
                    marginRight: 4 // 縮小間距
                  }}>
                    <span style={{ fontSize: 12 }}>📊</span> 期權
                  </button>

                  {/* Timeframe selector */}
                  <div style={{ display: "flex", gap: 2, background: COLORS.bgCard, borderRadius: 4, padding: 2, border: `1px solid ${COLORS.border}` }}>
                    {["1", "3", "15", "60", "日", "周", "月"].map(tf => (
                      <button key={tf} onClick={() => setTimeframe(tf)} style={{
                        padding: "3px 8px", fontSize: 10, fontWeight: timeframe === tf ? 700 : 400,
                        background: timeframe === tf ? "rgba(59,130,246,0.15)" : "transparent",
                        border: timeframe === tf ? `1px solid ${COLORS.accent}` : "1px solid transparent",
                        color: timeframe === tf ? COLORS.accent : COLORS.textDim,
                        borderRadius: 3, cursor: "pointer", transition: "all 0.15s"
                      }}>{tf}{["1", "3", "15", "60"].includes(tf) ? "分" : ""}</button>
                    ))}
                  </div>
                  {enabledIndicators.length > 0 && (
                    <>
                      <span style={{ color: COLORS.border }}>|</span>
                      <div style={{ display: "flex", gap: 3 }}>
                        {enabledIndicators.map(n => (
                          <span key={n} style={{
                            padding: "2px 6px", background: "rgba(59,130,246,0.1)",
                            borderRadius: 3, color: COLORS.accent, fontSize: 9, fontWeight: 600
                          }}>{n}</span>
                        ))}
                      </div>
                    </>
                  )}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <select value={chartSymbol} onChange={(e) => setChartSymbol(e.target.value)} style={{
                    background: COLORS.bgCard, border: `1px solid ${COLORS.border}`, borderRadius: 4,
                    color: COLORS.text, fontSize: 14, fontWeight: 700, padding: "2px 4px", outline: "none", cursor: "pointer"
                  }}>
                    <option value="TX">TX</option>
                    <option value="MTX">MTX</option>
                    <option value="TMF">TMF</option>
                  </select>
                  <span style={{ color: COLORS.text, fontWeight: 700, fontFamily: "monospace", fontSize: 16 }}>
                    {latestPrices[chartSymbol] ?? klineData[klineData.length - 1]?.close ?? "--"}
                  </span>
                  <span style={{
                    color: klineData[klineData.length - 1]?.close >= klineData[klineData.length - 2]?.close ? COLORS.up : COLORS.down,
                    fontSize: 11, fontWeight: 600
                  }}>
                    {klineData[klineData.length - 1]?.close >= klineData[klineData.length - 2]?.close ? "▲" : "▼"}
                    {Math.abs(klineData[klineData.length - 1]?.close - klineData[klineData.length - 2]?.close).toFixed(0)}
                  </span>
                </div>
              </div>

              {/* K-line chart — 60% */}
              <div style={{ ...panelStyle, flex: 6, position: "relative", minHeight: 0 }}>
                <CandlestickChart data={klineData} indicators={enabledIndicators} timeframe={timeframe} visibleCount={visibleCount} setVisibleCount={setVisibleCount} offset={offset} setOffset={setOffset} setTooltip={setGlobalTooltip} />
              </div>

              {/* Volume — 30% */}
              <div style={{ ...panelStyle, flex: 3, position: "relative", minHeight: 0 }}>
                <VolumeChart data={klineData} visibleCount={visibleCount} offset={offset} setTooltip={setGlobalTooltip} refLines={volumeRefLines} />
              </div>

              {/* Timeline Navigator */}
              {klineData.length > 0 && (
                <TimelineNavigator
                  data={klineData}
                  visibleCount={visibleCount}
                  setVisibleCount={setVisibleCount}
                  offset={offset}
                  setOffset={setOffset}
                />
              )}

              {/* Positions — 10% */}
              <div style={{ ...panelStyle, flex: 1, minHeight: 0, overflowY: "auto" }}>
                <div style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                  padding: "4px 10px", borderBottom: `1px solid ${COLORS.border}`
                }}>
                  <span style={{ fontSize: 9, color: COLORS.textMuted, letterSpacing: 1, fontWeight: 600, textTransform: "uppercase" }}>庫存倉位</span>
                  <span style={{
                    fontSize: 11, fontFamily: "monospace", fontWeight: 700,
                    color: MOCK_POSITIONS.reduce((s, p) => s + p.pnl, 0) >= 0 ? COLORS.up : COLORS.down
                  }}>
                    {MOCK_POSITIONS.reduce((s, p) => s + p.pnl, 0) >= 0 ? "+" : ""}
                    {MOCK_POSITIONS.reduce((s, p) => s + p.pnl, 0).toLocaleString()}
                  </span>
                </div>
                <div style={{ display: "flex", gap: 6, padding: "3px 8px", fontSize: 10, overflowX: "auto" }}>
                  {MOCK_POSITIONS.map(p => (
                    <div key={p.id} style={{
                      display: "flex", alignItems: "center", gap: 6, padding: "2px 8px",
                      background: COLORS.bgCard, borderRadius: 4, whiteSpace: "nowrap",
                      border: `1px solid ${p.pnl >= 0 ? "rgba(34,197,94,0.2)" : "rgba(239,68,68,0.2)"}`
                    }}>
                      <span style={{ color: COLORS.text, fontWeight: 600 }}>{p.symbol}</span>
                      <span style={{ color: p.direction === "多" ? COLORS.up : COLORS.down, fontWeight: 600 }}>{p.direction}{p.qty}</span>
                      <span style={{ color: p.pnl >= 0 ? COLORS.up : COLORS.down, fontFamily: "monospace", fontWeight: 600 }}>
                        {p.pnl >= 0 ? "+" : ""}{p.pnl.toLocaleString()}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
            {/* Right: 閃電下單 (10/5) + 倉位/委託 (10/2) + 成交明細 (10/3) */}
            <div style={{ width: 330, display: "flex", flexDirection: "column", gap: 6, flexShrink: 0 }}>
              {/* 閃電下單 - 50% */}
              <div style={{ ...panelStyle, flex: 5, display: "flex", flexDirection: "column", minHeight: 0 }}>
                <OrderPanel brokerConfig={brokerConfig}
                  currentPrice={latestPrices[orderSymbol] ?? 17535}
                  orderbook={orderbooks[orderSymbol]}
                  activeSymbol={orderSymbol} setActiveSymbol={setOrderSymbol}
                  myBuyOrders={myBuyOrders} setMyBuyOrders={setMyBuyOrders}
                  mySellOrders={mySellOrders} setMySellOrders={setMySellOrders}
                  stopBuys={stopBuys} setStopBuys={setStopBuys}
                  stopSells={stopSells} setStopSells={setStopSells}
                />
              </div>
              {/* 倉位/委託 - 20% */}
              <div style={{ ...panelStyle, flex: 2, minHeight: 0, display: "flex", flexDirection: "column" }}>
                <PositionOrdersPanel
                  myBuyOrders={myBuyOrders} setMyBuyOrders={setMyBuyOrders}
                  mySellOrders={mySellOrders} setMySellOrders={setMySellOrders}
                  stopBuys={stopBuys} setStopBuys={setStopBuys}
                  stopSells={stopSells} setStopSells={setStopSells}
                />
              </div>
              {/* 成交明細 - 30% */}
              <div style={{ ...panelStyle, flex: 3, minHeight: 0, display: "flex", flexDirection: "column" }}>
                <TradeHistoryPanel />
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ─── Status Bar ────────────────────────────── */}
      <div style={{
        height: 26, display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "0 12px", background: COLORS.bgPanel, borderTop: `1px solid ${COLORS.border}`,
        fontSize: 10, color: COLORS.textDim, flexShrink: 0
      }}>
        <div style={{ display: "flex", gap: 16 }}>
          <span>
            <span style={{ display: "inline-block", width: 6, height: 6, borderRadius: "50%", background: connected ? COLORS.success : COLORS.danger, marginRight: 4, boxShadow: `0 0 6px ${connected ? COLORS.success : COLORS.danger}` }} />
            {connected ? "後端連線正常" : "後端未連線"}
          </span>
          <span>延遲: 3ms</span>
        </div>
        <div style={{ display: "flex", gap: 16 }}>
          <span>Scripts: {scripts.filter(s => s.enabled).length} 啟用</span>
          <span>DB: 期交所 + 券商API</span>
          <span>v0.1.0-alpha</span>
        </div>
      </div>

      {/* Global Tooltip */}
      {globalTooltip && (
        <div style={{
          position: "fixed", left: globalTooltip.x + 12, top: globalTooltip.y - 80,
          background: "rgba(17,24,39,0.95)", border: `1px solid ${COLORS.borderLight}`,
          borderRadius: 6, padding: "8px 12px", fontSize: 11, fontFamily: "monospace",
          color: COLORS.text, pointerEvents: "none", zIndex: 1000, backdropFilter: "blur(8px)",
          boxShadow: "0 4px 20px rgba(0,0,0,0.5)"
        }}>
          <div style={{ color: COLORS.textDim, marginBottom: 4 }}>
            {["日", "周", "月"].includes(timeframe)
              ? new Date(globalTooltip.time).toLocaleDateString("zh-TW", { month: "2-digit", day: "2-digit" })
              : new Date(globalTooltip.time).toLocaleString("zh-TW", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}
          </div>
          <div>開 <span style={{ color: COLORS.text, fontWeight: 600 }}>{globalTooltip.open}</span></div>
          <div>高 <span style={{ color: COLORS.up }}>{globalTooltip.high}</span></div>
          <div>低 <span style={{ color: COLORS.down }}>{globalTooltip.low}</span></div>
          <div>收 <span style={{ color: globalTooltip.close >= globalTooltip.open ? COLORS.up : COLORS.down, fontWeight: 600 }}>{globalTooltip.close}</span></div>
          <div>量 <span style={{ color: COLORS.accent }}>{globalTooltip.volume.toLocaleString()}</span></div>
          {globalTooltip.indVals && Object.keys(globalTooltip.indVals).length > 0 && (
            <>
              <div style={{ borderTop: `1px solid ${COLORS.border}`, margin: "5px 0" }} />
              {Object.entries(globalTooltip.indVals).map(([name, val]) => (
                <div key={name}>
                  <span style={{ color: COLORS.textDim }}>{name} </span>
                  <span style={{ color: COLORS.warn, fontWeight: 600 }}>{val}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}

      {/* Broker Config Modal */}
      {showBrokerConfig && (
        <BrokerConfigPanel
          brokerConfig={brokerConfig}
          setBrokerConfig={setBrokerConfig}
          onClose={() => setShowBrokerConfig(false)}
          send={send}
          addHandler={addHandler}
        />
      )}
    </div>
  );
}
