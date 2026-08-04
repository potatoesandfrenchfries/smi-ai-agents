import { useState, type KeyboardEvent } from "react";
import styles from "./Composer.module.css";

export function Composer({
  onSend,
  disabled,
}: {
  onSend: (message: string) => void;
  disabled: boolean;
}) {
  const [value, setValue] = useState("");

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className={styles.composer}>
      <textarea
        className={styles.input}
        placeholder="Plan a trip, or ask about the one in review…"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        rows={1}
        disabled={disabled}
      />
      <button
        type="button"
        className={styles.send}
        onClick={submit}
        disabled={disabled || value.trim().length === 0}
      >
        Send
      </button>
    </div>
  );
}
