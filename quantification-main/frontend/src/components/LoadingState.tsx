import type { CSSProperties } from "react";

type LoadingVariant = "page" | "section" | "inline";

interface LoadingStateProps {
  title?: string;
  detail?: string;
  variant?: LoadingVariant;
  rows?: number;
  className?: string;
}

export default function LoadingState({
  title = "正在加载",
  detail = "正在读取最新数据，请稍候",
  variant = "section",
  rows = 3,
  className = "",
}: LoadingStateProps) {
  const skeletonRows = Math.max(1, Math.min(rows, 5));
  return (
    <div className={`alis-loading alis-loading-${variant} ${className}`.trim()} role="status" aria-live="polite" aria-busy="true">
      <div className="alis-loading-heading">
        <span className="alis-loading-mark" aria-hidden="true"><i /></span>
        <div><strong>{title}</strong><p>{detail}</p></div>
      </div>
      {variant !== "inline" && (
        <div className="alis-loading-skeleton" aria-hidden="true">
          {Array.from({ length: skeletonRows }, (_, index) => (
            <span key={index} style={{ "--loading-row-width": `${92 - index * 11}%` } as CSSProperties} />
          ))}
        </div>
      )}
    </div>
  );
}
