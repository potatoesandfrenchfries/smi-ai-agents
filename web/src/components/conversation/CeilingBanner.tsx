import type { ConversationCeiling } from "../../api/types";
import styles from "./CeilingBanner.module.css";

/** Design Reference §6.2 usage ceiling: warn at 80%, lock at 100%. */
export function CeilingBanner({ ceiling }: { ceiling: ConversationCeiling | null }) {
  if (!ceiling || ceiling.status === "ok") return null;

  return (
    <div className={styles.banner} data-status={ceiling.status}>
      <span>
        {ceiling.status === "exceeded"
          ? "Conversation limit reached — start a new conversation to continue."
          : ceiling.statusMessage ?? "This conversation is approaching its limit."}
      </span>
      <span className={styles.usage}>
        {ceiling.messagesUsed}/{ceiling.maxMessages} messages · {ceiling.percentUsed.toFixed(0)}% used
      </span>
    </div>
  );
}
