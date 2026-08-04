import styles from "./HitlActionBar.module.css";

/** FR-GAT-3 / FR-PRS-2: the human-in-the-loop gate. Nothing books without this. */
export function HitlActionBar({
  status,
  actionState,
  requiresApproval,
  onConfirm,
  onReject,
}: {
  status: string;
  actionState: "idle" | "confirming" | "rejecting";
  requiresApproval: boolean;
  onConfirm: () => void;
  onReject: () => void;
}) {
  const isDecided = status === "confirmed" || status === "rejected";

  return (
    <div className={styles.bar}>
      <div className={styles.statusText}>
        {isDecided ? (
          <span data-tone={status === "confirmed" ? "green" : "red"}>
            {status === "confirmed" ? "Confirmed — routed for handoff" : "Rejected"}
          </span>
        ) : requiresApproval ? (
          <span data-tone="amber">Awaiting approver sign-off before confirmation</span>
        ) : (
          <span>Review the itinerary, then confirm or reject</span>
        )}
      </div>
      {!isDecided && (
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.reject}
            onClick={onReject}
            disabled={actionState !== "idle"}
          >
            Reject
          </button>
          <button
            type="button"
            className={styles.confirm}
            onClick={onConfirm}
            disabled={actionState !== "idle" || requiresApproval}
          >
            {actionState === "confirming" ? "Confirming…" : "Confirm itinerary"}
          </button>
        </div>
      )}
    </div>
  );
}
