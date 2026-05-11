import { useEffect, useRef, useState } from "react";

interface InputBarProps {
  onSubmit: (command: string) => void;
  disabled?: boolean;
}

export function InputBar({ onSubmit, disabled = false }: InputBarProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grow the textarea to match its content, capped by CSS max-height
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [value]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (value.trim()) {
        onSubmit(value.trim());
        setValue("");
      }
    }
  };

  return (
    <div className="InputBar">
      <textarea
        ref={textareaRef}
        className="InputBar-textarea"
        rows={1}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        aria-label="Command input"
        placeholder="Type a command and press Enter..."
        spellCheck={false}
      />
    </div>
  );
}
