import { useEffect, useRef } from "react";
import type { ConversationMessageItem, StructuredResponse } from "../../api/types";
import { parseStructuredContent } from "../../api/structuredContent";
import { BlockRenderer } from "./BlockRenderer";
import styles from "./MessageList.module.css";

export function MessageList({
  messages,
  streamingText,
  structuredResponse,
  isStreaming,
}: {
  messages: ConversationMessageItem[];
  streamingText: string;
  structuredResponse: StructuredResponse | null;
  isStreaming: boolean;
}) {
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, streamingText, structuredResponse]);

  return (
    <div className={`${styles.list} scroll-thin`}>
      {messages.length === 0 && !isStreaming && (
        <div className={styles.empty}>
          <p>Describe the trip you need — dates, purpose, budget.</p>
          <p className={styles.emptyHint}>
            The planner will ask if anything required is missing, then dispatch flight,
            hotel, and dining specialists in parallel.
          </p>
        </div>
      )}

      {messages.map((m) => {
        const blocks = m.role === "assistant" ? parseStructuredContent(m.content) : null;
        return (
          <div key={m.id} className={styles.row} data-role={m.role}>
            <div className={styles.roleLabel}>{m.role}</div>
            <div className={styles.bubble}>
              {blocks ? (
                <div className={styles.blocks}>
                  {blocks.map((b, i) => (
                    <BlockRenderer key={i} block={b} />
                  ))}
                </div>
              ) : (
                m.content
              )}
            </div>
          </div>
        );
      })}

      {isStreaming && (structuredResponse || streamingText) && (
        <div className={styles.row} data-role="assistant">
          <div className={styles.roleLabel}>assistant</div>
          <div className={styles.bubble}>
            {structuredResponse ? (
              <div className={styles.blocks}>
                {structuredResponse.blocks.map((b, i) => (
                  <BlockRenderer key={i} block={b} />
                ))}
              </div>
            ) : (
              <>
                {streamingText}
                <span className={styles.cursor} aria-hidden="true" />
              </>
            )}
          </div>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
