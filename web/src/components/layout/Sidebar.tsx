import type { ConversationListItem } from "../../api/types";
import styles from "./Sidebar.module.css";

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

export function Sidebar({
  conversations,
  activeId,
  onSelect,
  onCreate,
}: {
  conversations: ConversationListItem[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
}) {
  return (
    <nav className={styles.sidebar} aria-label="Conversations">
      <div className={styles.brand}>
        <span className={styles.brandMark}>SMI</span>
        <span className={styles.brandName}>Smartinerary</span>
      </div>

      <button type="button" className={styles.newButton} onClick={onCreate}>
        + New conversation
      </button>

      <div className={`${styles.list} scroll-thin`}>
        {conversations.map((c) => (
          <button
            key={c.id}
            type="button"
            className={styles.item}
            data-active={c.id === activeId}
            onClick={() => onSelect(c.id)}
          >
            <div className={styles.itemTop}>
              <span className={styles.itemTitle}>{c.title ?? "Untitled conversation"}</span>
              {c.lastMessage && (
                <span className={styles.itemTime}>{relativeTime(c.lastMessage.at)}</span>
              )}
            </div>
            {c.lastMessage && (
              <p className={styles.itemPreview}>{c.lastMessage.preview}</p>
            )}
            <div className={styles.itemMeta}>
              <span
                className={styles.ceilingDot}
                data-status={c.ceiling.status}
                aria-hidden="true"
              />
              <span>{c.displayId}</span>
            </div>
          </button>
        ))}
        {conversations.length === 0 && (
          <p className={styles.emptyList}>No conversations yet.</p>
        )}
      </div>
    </nav>
  );
}
