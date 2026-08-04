import { useChat } from "../../hooks/useChat";
import { CeilingBanner } from "./CeilingBanner";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { StepTimeline } from "./StepTimeline";
import styles from "./ChatPanel.module.css";

export function ChatPanel({ conversationId }: { conversationId: string | null }) {
  const chat = useChat(conversationId);

  return (
    <div className={styles.panel}>
      <CeilingBanner ceiling={chat.ceiling} />
      <MessageList
        messages={chat.messages}
        streamingText={chat.streamingText}
        structuredResponse={chat.structuredResponse}
        isStreaming={chat.isStreaming}
      />
      {chat.isStreaming && chat.steps.length > 0 && (
        <div className={styles.stepsWrap}>
          <StepTimeline steps={chat.steps} />
        </div>
      )}
      {chat.error && <div className={styles.error}>{chat.error}</div>}
      <Composer
        onSend={chat.send}
        disabled={!conversationId || chat.isStreaming || chat.ceiling?.status === "exceeded"}
      />
    </div>
  );
}
