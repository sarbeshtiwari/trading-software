import { useEffect, useRef, useState } from 'react';
import { CandlestickSeries, LineSeries, ColorType, createChart, type UTCTimestamp, type Time, type IChartApi } from 'lightweight-charts';
import type { components } from './api-schema';

type Data = components['schemas']['CandleChart'];

export function CandleChart({ data }: { data: Data }) {
  const container = useRef<HTMLDivElement>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!container.current || !data.bars?.length) return;
    let chart: IChartApi | undefined;
    try {
      setError('');
      chart = createChart(container.current, {
      autoSize: true, height: 350,
      layout: { background: { type: ColorType.Solid, color: '#111827' }, textColor: '#e5e7eb' },
      timeScale: { timeVisible: true, secondsVisible: false },
      localization: { timeFormatter: (time: Time) => typeof time === 'number' ? new Date(time * 1000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) : String(time) },
    });
      const candles = chart.addSeries(CandlestickSeries);
      candles.setData(data.bars.map(bar => ({
        time: (Date.parse(bar.ts) / 1000) as UTCTimestamp,
        open: Number(bar.open), high: Number(bar.high), low: Number(bar.low), close: Number(bar.close),
      })));
      const average = chart.addSeries(LineSeries, { title: 'Backend SMA20', color: '#fbbf24', lineWidth: 2 });
      average.setData(data.bars.filter(bar => bar.sma20 !== null).map(bar => ({ time: (Date.parse(bar.ts) / 1000) as UTCTimestamp, value: Number(bar.sma20) })));
      chart.timeScale().fitContent();
    } catch (failure) { setError(String(failure)); }
    return () => chart?.remove();
  }, [data]);
  return <><div ref={container} aria-label="Stored candlestick chart" style={{ height: 350 }} />
    {error && <p role="alert">CHART UNAVAILABLE: {error}</p>}
    <p>Crosshair timestamps: IST. Axis timestamps: UTC. SMA20 is calculated by the backend over returned observations; warm-up values remain unavailable.</p>
    <p>TradingView Lightweight Charts™ — Copyright (с) 2025 <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView, Inc.</a></p>
  </>;
}
