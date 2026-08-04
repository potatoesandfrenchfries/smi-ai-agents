import type { ItinerarySegment } from "../../api/types";
import { PolicyBadge } from "../common/PolicyBadge";
import styles from "./SegmentTicket.module.css";

const KIND_LABEL: Record<ItinerarySegment["kind"], string> = {
  flight: "Flight",
  hotel: "Hotel",
  rail: "Rail",
  car: "Car Rental",
  dining: "Dining",
  activity: "Activity",
  transfer: "Transfer",
};

/**
 * The itinerary's signature element: every segment is drawn as a literal
 * ticket stub, split by a perforated edge into the segment itself and a
 * provenance stub. This is not decoration — it makes FR-SPC-4 ("every
 * candidate traces to a PriceSnapshot") and the specialist's self-reported
 * confidence part of what the reviewer sees on first glance, not a fact
 * buried in a tooltip.
 *
 * Schedule text comes only from `subtitle` (the specialist's own wording,
 * e.g. "Thu 14 Aug · 07:05 – 08:25"). `startsAt`/`endsAt` exist for ordering
 * and are deliberately not re-formatted into a second, independently
 * computed local-time string here — a browser-local conversion of a UTC
 * instant can silently disagree with the destination-local time the
 * specialist already reported, which is the number the traveler actually
 * needs at the gate.
 */
export function SegmentTicket({
  segment,
  sequence,
  onRequestChange,
}: {
  segment: ItinerarySegment;
  sequence: number;
  onRequestChange?: (segment: ItinerarySegment) => void;
}) {
  return (
    <article className={styles.ticket}>
      <div className={styles.main}>
        <div className={styles.kindRow}>
          <span className={styles.kindLabel}>
            {String(sequence).padStart(2, "0")} · {KIND_LABEL[segment.kind]}
          </span>
          <PolicyBadge decision={segment.policyDecision} />
        </div>
        <h3 className={styles.title}>{segment.title}</h3>
        {segment.subtitle && <p className={styles.subtitle}>{segment.subtitle}</p>}
        <div className={styles.footer}>
          <span className={styles.provider}>{segment.providerName}</span>
          {onRequestChange && (
            <button
              type="button"
              className={styles.swapButton}
              onClick={() => onRequestChange(segment)}
            >
              Request a change
            </button>
          )}
        </div>
      </div>

      <div className={styles.perforation} aria-hidden="true">
        <span className={styles.notchTop} />
        <span className={styles.notchBottom} />
      </div>

      <div className={styles.stub}>
        <div className={styles.amount}>
          {segment.currency === "GBP" ? "£" : `${segment.currency} `}
          {segment.amount.toFixed(2)}
        </div>
        <div className={styles.stubMeta}>
          <span className={styles.stubLabel}>Confidence</span>
          <span>{Math.round(segment.confidence * 100)}%</span>
        </div>
        <div className={styles.stubMeta}>
          <span className={styles.stubLabel}>Snapshot</span>
          <span className={styles.snapshotId}>{segment.snapshotId}</span>
        </div>
        <div className={styles.barcode} aria-hidden="true" />
      </div>
    </article>
  );
}
