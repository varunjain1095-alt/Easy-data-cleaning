import { useEffect, useRef } from "react";
import Plotly from "plotly.js-dist-min";

export default function Plot({
  data,
  layout = {},
  height = 320,
}: {
  data: any[];
  layout?: any;
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (ref.current) {
      Plotly.react(ref.current, data, { height, margin: { t: 30, r: 10, b: 40, l: 50 }, ...layout }, { responsive: true, displaylogo: false });
    }
  }, [data, layout, height]);
  return <div ref={ref} />;
}
