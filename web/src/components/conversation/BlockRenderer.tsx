import type {
  ActionButtonsContent,
  ContentBlock,
  ErrorContent,
  ListContent,
  MetricRowContent,
  TableContent,
  TextContent,
} from "../../api/types";
import styles from "./BlockRenderer.module.css";

/**
 * Renders the canonical StructuredResponse block union (agents/response.py).
 *
 * `block.content`'s shape is only as trustworthy as the LLM output it came
 * from — agents/response.py's ContentBlock validator rejects a `type`/
 * `content` mismatch going forward, but that doesn't retroactively fix
 * messages already persisted before the validator existed. Each case below
 * checks its required array field is actually an array before mapping over
 * it, so one malformed block renders as nothing instead of throwing and
 * taking the whole page blank (no error boundary above this recovers from
 * that — see MessageList.tsx's use of ErrorBoundary for the last-resort net).
 */
export function BlockRenderer({ block }: { block: ContentBlock }) {
  switch (block.type) {
    case "text": {
      const c = block.content as TextContent;
      return <p className={styles.text}>{c.body}</p>;
    }
    case "separator": {
      const label = (block.content as { label?: string | null }).label;
      return (
        <div className={styles.separator} role="separator">
          {label && <span>{label}</span>}
        </div>
      );
    }
    case "list": {
      const c = block.content as ListContent;
      if (!Array.isArray(c.items)) return null;
      const Tag = c.style === "numbered" ? "ol" : "ul";
      return (
        <div className={styles.blockGroup}>
          {c.title && <div className={styles.blockTitle}>{c.title}</div>}
          <Tag className={styles.list} data-style={c.style ?? "bulleted"}>
            {c.items.map((item, i) => (
              <li key={i} data-severity={item.severity ?? undefined}>
                {item.text}
              </li>
            ))}
          </Tag>
        </div>
      );
    }
    case "table": {
      const c = block.content as TableContent;
      if (!Array.isArray(c.columns) || !Array.isArray(c.rows)) return null;
      return (
        <div className={styles.blockGroup}>
          <div className={styles.blockTitle}>{c.title}</div>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  {c.columns.map((col) => (
                    <th key={col.name}>{col.name}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {c.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j}>{String(cell)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );
    }
    case "metric_row": {
      const c = block.content as MetricRowContent;
      if (!Array.isArray(c.metrics)) return null;
      return (
        <div className={styles.metricRow}>
          {c.metrics.map((m) => (
            <div key={m.label} className={styles.metric} data-color={m.color ?? undefined}>
              <div className={styles.metricValue}>
                {m.format === "currency" ? `£${m.value.toLocaleString()}` : m.value.toLocaleString()}
                {m.format === "percent" ? "%" : ""}
              </div>
              <div className={styles.metricLabel}>{m.label}</div>
            </div>
          ))}
        </div>
      );
    }
    case "action_buttons": {
      const c = block.content as ActionButtonsContent;
      if (!Array.isArray(c.actions)) return null;
      return (
        <div className={styles.actions}>
          {c.actions.map((a) => (
            <button key={a.label} type="button" className={styles.actionButton} data-style={a.style ?? "secondary"}>
              {a.label}
            </button>
          ))}
        </div>
      );
    }
    case "error": {
      const c = block.content as ErrorContent;
      return (
        <div className={styles.error} data-severity={c.severity ?? "warning"}>
          <div className={styles.errorTitle}>{c.title}</div>
          <div>{c.body}</div>
          {c.recoveryHint && <div className={styles.errorHint}>{c.recoveryHint}</div>}
        </div>
      );
    }
    case "code": {
      const c = block.content as { title?: string; code: string };
      return (
        <div className={styles.blockGroup}>
          {c.title && <div className={styles.blockTitle}>{c.title}</div>}
          <pre className={styles.code}>{c.code}</pre>
        </div>
      );
    }
    default:
      return null;
  }
}
