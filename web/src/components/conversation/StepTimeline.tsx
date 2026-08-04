import type { StepEventData } from "../../api/types";
import { StatusDot } from "../common/StatusDot";
import styles from "./StepTimeline.module.css";

/**
 * Renders the orchestrator's reasoning steps as a teleprinter feed — the
 * closest visual analogue to what the PRD calls the PlanGraph (FR-ORC-6):
 * a running log of which node ran, in what order, and whether it finished.
 */
export function StepTimeline({ steps }: { steps: StepEventData[] }) {
  if (steps.length === 0) return null;

  return (
    <ol className={styles.timeline} aria-label="Agent reasoning steps">
      {steps.map((step) => (
        <li key={step.step_id} className={styles.row} data-status={step.status}>
          <StatusDot status={step.status} />
          <span className={styles.node}>{step.node}</span>
          <span className={styles.message}>{step.message}</span>
        </li>
      ))}
    </ol>
  );
}
