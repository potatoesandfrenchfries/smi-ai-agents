import { useState } from "react";
import type { ItinerarySegment } from "../../api/types";
import { PolicyBadge } from "../common/PolicyBadge";
import styles from "./SegmentTicket.module.css";

const KIND_LABEL: Record<string, string> = {
  flight: "Flight",
  hotel: "Hotel",
  attraction: "Attraction",
  dining: "Dining",
  rail: "Rail",
  car: "Car Rental",
  activity: "Activity",
  transfer: "Transfer",
};

/** 1-5 star control — feeds providers/ranking/bandit.py::reward_from_rating
 * via POST /:planId/rate, distinct from "Request a change": rating doesn't
 * alter the itinerary, it's purely a preference signal for future ranking.
 * Only rendered when rankArm is set — an un-ranked segment (predates
 * providers/ranking/, or personalization is off) has nothing to attribute
 * a rating to. */
function RatingStars({ onRate }: { onRate: (rating: number) => void }) {
  const [selected, setSelected] = useState<number | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);

  return (
    <div className={styles.rating} role="radiogroup" aria-label="Rate this option">
      {[1, 2, 3, 4, 5].map((n) => {
        const filled = (hovered ?? selected ?? 0) >= n;
        return (
          <button
            key={n}
            type="button"
            className={styles.star}
            data-filled={filled}
            aria-label={`${n} star${n === 1 ? "" : "s"}`}
            aria-checked={selected === n}
            role="radio"
            onMouseEnter={() => setHovered(n)}
            onMouseLeave={() => setHovered(null)}
            onClick={() => {
              setSelected(n);
              onRate(n);
            }}
          >
            {filled ? "★" : "☆"}
          </button>
        );
      })}
    </div>
  );
}

/**
 * The itinerary's signature element: every segment is drawn as a literal
 * ticket stub, split by a perforated edge into the segment itself and a
 * provenance stub. This is not decoration — it makes FR-SPC-4 ("every
 * candidate traces to a PriceSnapshot") part of what the reviewer sees on
 * first glance, not a fact buried in a tooltip.
 *
 * `reason` is the specialist's own "why this ranks where it does" text
 * (providers/explain.py::annotate_reasons / annotate_reasons_personalized)
 * — real backend output, not placeholder copy. There's no structured
 * start/end time or numeric confidence score on the wire (see
 * api/types.ts's comment on ItinerarySegment) — `subtitle` is the only
 * schedule text, in the specialist's own wording.
 */
export function SegmentTicket({
  segment,
  sequence,
  onRequestChange,
  onRate,
}: {
  segment: ItinerarySegment;
  sequence: number;
  onRequestChange?: (segment: ItinerarySegment) => void;
  onRate?: (segment: ItinerarySegment, rating: number) => void;
}) {
  const canRate = Boolean(onRate && segment.rankArm && segment.candidateId);

  return (
    <article className={styles.ticket}>
      <div className={styles.main}>
        <div className={styles.kindRow}>
          <span className={styles.kindLabel}>
            {String(sequence).padStart(2, "0")} · {KIND_LABEL[segment.kind] ?? segment.kind}
          </span>
          <PolicyBadge decision={segment.policyDecision} />
        </div>
        <h3 className={styles.title}>{segment.title}</h3>
        {segment.subtitle && <p className={styles.subtitle}>{segment.subtitle}</p>}
        {segment.reason && <p className={styles.reason}>{segment.reason}</p>}
        <div className={styles.footer}>
          {canRate ? (
            <RatingStars onRate={(rating) => onRate?.(segment, rating)} />
          ) : (
            <span />
          )}
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
          {segment.amount === null
            ? "—"
            : `${segment.currency === "GBP" ? "£" : `${segment.currency} `}${segment.amount.toFixed(2)}`}
        </div>
        {segment.snapshotId && (
          <div className={styles.stubMeta}>
            <span className={styles.stubLabel}>Snapshot</span>
            <span className={styles.snapshotId}>{segment.snapshotId}</span>
          </div>
        )}
        <div className={styles.barcode} aria-hidden="true" />
      </div>
    </article>
  );
}
