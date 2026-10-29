export type DynamicBoxCongestionSeverity = "normal" | "caution" | "warning" | "alert";

export interface DynamicBoxCongestionAssessment {
  severity: DynamicBoxCongestionSeverity;
  label: string;
  message: string;
}

/**
 * Turn the number of currently detected boxes into the operational
 * congestion severity shown in the Viewer Inspector.  Zero boxes is normal:
 * it means the monitored area is clear, not that the tracking system failed.
 */
export function assessDynamicBoxCongestion(detectedBoxCount: number): DynamicBoxCongestionAssessment {
  const count = Math.max(0, Math.floor(Number.isFinite(detectedBoxCount) ? detectedBoxCount : 0));
  if (count <= 1) {
    return {
      severity: "normal",
      label: "通常",
      message: count === 0 ? "検出中の箱はありません。" : "箱を1個検出しています。",
    };
  }
  if (count === 2) {
    return { severity: "caution", label: "注意", message: "箱を2個検出しています。滞留に注意してください。" };
  }
  if (count === 3) {
    return { severity: "warning", label: "警告", message: "箱を3個検出しています。現場を確認してください。" };
  }
  return { severity: "alert", label: "アラート", message: `箱を${count}個検出しています。至急確認してください。` };
}
