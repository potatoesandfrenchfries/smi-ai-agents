import { useState } from "react";
import styles from "./TripStarter.module.css";

/**
 * Starts a new plan run (PRD §5 stages 1-4) via the gateway's Temporal-backed
 * trips surface. Kept separate from the conversation composer because, as
 * built today, the FastAPI conversation graph and the itinerary Temporal
 * workflow are two independent backends (see gateway/src/temporal/client.ts)
 * — a future iteration could have the conversational agent trigger this
 * itself, but that hand-off doesn't exist in the Python code yet.
 */
export function TripStarter({
  onStart,
  submitting,
}: {
  onStart: (rawGoal: string) => void;
  submitting: boolean;
}) {
  const [goal, setGoal] = useState("");

  return (
    <div className={styles.starter}>
      <p className={styles.lead}>No itinerary yet.</p>
      <p className={styles.hint}>
        Describe the trip and the planner will dispatch flight, hotel, and dining
        specialists in parallel, then present a versioned itinerary here for review.
      </p>
      <textarea
        className={styles.textarea}
        placeholder="e.g. Two days in Edinburgh next month for a client meeting, budget £900"
        value={goal}
        onChange={(e) => setGoal(e.target.value)}
        rows={3}
      />
      <button
        type="button"
        className={styles.button}
        disabled={submitting || goal.trim().length === 0}
        onClick={() => onStart(goal.trim())}
      >
        {submitting ? "Starting plan…" : "Start planning"}
      </button>
    </div>
  );
}
