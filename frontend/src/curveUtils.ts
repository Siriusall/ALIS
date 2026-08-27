export const CURVE_PLOT_START_PERCENT = 5;
export const CURVE_PLOT_END_PERCENT = 95;

export function curveHoverIndex(pointerX: number, plotWidth: number, rowCount: number): number {
  if (rowCount <= 1 || !Number.isFinite(plotWidth) || plotWidth <= 0) return 0;
  const pointerPercent = pointerX / plotWidth * 100;
  const dataPercent = Math.max(
    0,
    Math.min(
      1,
      (pointerPercent - CURVE_PLOT_START_PERCENT)
        / (CURVE_PLOT_END_PERCENT - CURVE_PLOT_START_PERCENT),
    ),
  );
  return Math.round(dataPercent * (rowCount - 1));
}
