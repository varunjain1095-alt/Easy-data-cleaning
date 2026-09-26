declare module "plotly.js-dist-min" {
  const Plotly: any;
  export default Plotly;
  export function react(el: HTMLElement, data: any[], layout?: any, config?: any): Promise<void>;
  export function newPlot(el: HTMLElement, data: any[], layout?: any, config?: any): Promise<void>;
}
