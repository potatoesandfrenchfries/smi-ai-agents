import type { PolicyDecision } from "../../api/types";
import styles from "./PolicyBadge.module.css";

const LABEL: Record<PolicyDecision, string> = {
  compliant: "Compliant",
  breach: "Policy breach",
  waived: "Waived",
  not_applicable: "Not applicable",
  pending: "Pending review",
};

const TONE: Record<PolicyDecision, string> = {
  compliant: "green",
  breach: "red",
  waived: "amber",
  not_applicable: "neutral",
  pending: "amber",
};

export function PolicyBadge({ decision }: { decision: PolicyDecision }) {
  const tone = TONE[decision];
  return (
    <span className={styles.badge} data-tone={tone}>
      <span className={styles.dot} aria-hidden="true" />
      {LABEL[decision]}
    </span>
  );
}
