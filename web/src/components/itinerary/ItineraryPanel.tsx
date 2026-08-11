import type { ItinerarySegment, ItineraryView } from "../../api/types";
import { PolicyBadge } from "../common/PolicyBadge";
import { HitlActionBar } from "./HitlActionBar";
import { SegmentTicket } from "./SegmentTicket";
import { TripStarter } from "./TripStarter";
import styles from "./ItineraryPanel.module.css";

export function ItineraryPanel({
  itinerary,
  loading,
  actionState,
  onConfirm,
  onReject,
  onRequestChange,
  onRate,
  onStartTrip,
  startingTrip,
}: {
  itinerary: ItineraryView | null;
  loading: boolean;
  actionState: "idle" | "confirming" | "rejecting";
  onConfirm: () => void;
  onReject: () => void;
  onRequestChange: (segmentId: string) => void;
  onRate?: (segment: ItinerarySegment, rating: number) => void;
  onStartTrip: (rawGoal: string) => void;
  startingTrip: boolean;
}) {
  if (loading && !itinerary) {
    return (
      <div className={styles.panel}>
        <div className={styles.empty}>Compiling itinerary…</div>
      </div>
    );
  }

  if (!itinerary) {
    return (
      <div className={styles.panel}>
        <TripStarter onStart={onStartTrip} submitting={startingTrip} />
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <header className={styles.header}>
        <div className={styles.headerTop}>
          <span className={styles.version}>Itinerary v{itinerary.version}</span>
          <PolicyBadge decision={itinerary.policyStatus} />
        </div>
        <div className={styles.total}>
          £{itinerary.totalAmount.toFixed(2)}
          <span className={styles.totalLabel}>total, {itinerary.segments.length} segments</span>
        </div>
        {itinerary.assumptions.length > 0 && (
          <ul className={styles.assumptions}>
            {itinerary.assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        )}
      </header>

      <div className={`${styles.segments} scroll-thin`}>
        {itinerary.segments.map((segment, i) => (
          <SegmentTicket
            key={segment.id}
            segment={segment}
            sequence={i + 1}
            onRequestChange={(s) => onRequestChange(s.id)}
            onRate={onRate}
          />
        ))}
      </div>

      <HitlActionBar
        status={itinerary.status}
        actionState={actionState}
        requiresApproval={itinerary.requiresApproval}
        onConfirm={onConfirm}
        onReject={onReject}
      />
    </div>
  );
}
