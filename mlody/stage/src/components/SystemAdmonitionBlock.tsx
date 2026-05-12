import type { SystemAdmonition } from "../types.js";

interface SystemAdmonitionBlockProps {
  admonition: SystemAdmonition;
}

export function SystemAdmonitionBlock({
  admonition,
}: SystemAdmonitionBlockProps) {
  return (
    <div
      className={`SystemAdmonition SystemAdmonition--${admonition.tone}`}
      role="status"
      aria-live="polite"
    >
      <div className="SystemAdmonition-signal" aria-hidden="true" />
      <div className="SystemAdmonition-content">
        <span className="SystemAdmonition-label">System</span>
        <span className="SystemAdmonition-title">{admonition.title}</span>
        <p className="SystemAdmonition-message">{admonition.message}</p>
      </div>
    </div>
  );
}
