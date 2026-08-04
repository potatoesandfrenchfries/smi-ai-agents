import type { StepStatus } from "../../api/types";
import styles from "./StatusDot.module.css";

const GLYPH: Record<StepStatus, string> = {
  pending: "○",
  in_progress: "◐",
  completed: "●",
  failed: "✕",
};

export function StatusDot({ status }: { status: StepStatus }) {
  return (
    <span className={styles.dot} data-status={status} aria-hidden="true">
      {GLYPH[status]}
    </span>
  );
}
