import type { StageCommandLogEvent } from "../types.js";
import { JsonSyntaxBlock } from "./JsonSyntaxBlock.js";

interface StageLogsBlockProps {
  title?: string;
  requestId: string;
  status: "loading" | "loaded" | "error";
  events?: StageCommandLogEvent[];
  error?: string;
}

type StageLogTone =
  | "debug"
  | "info"
  | "warning"
  | "error"
  | "critical"
  | "neutral";

const HIDDEN_LOG_KEYS = new Set([
  "event",
  "kind",
  "requestId",
  "timestamp",
  "message",
  "sequence",
]);

function eventType(event: StageCommandLogEvent): string | null {
  if (typeof event.event === "string" && event.event.trim() !== "") {
    return event.event;
  }
  if (typeof event.kind === "string" && event.kind.trim() !== "") {
    return event.kind;
  }
  return null;
}

function formatLogValue(value: string | number | boolean | null): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  return String(value);
}

function statusLabel(
  status: StageLogsBlockProps["status"],
  eventCount: number,
): string {
  if (status === "loading") {
    return "Loading…";
  }
  if (status === "error") {
    return "Load failed";
  }
  return `${eventCount} event${eventCount === 1 ? "" : "s"}`;
}

function eventBadgeLabel(event: StageCommandLogEvent): string {
  const type = eventType(event);
  if (type === "log" && typeof event.level === "string") {
    return event.level;
  }
  if (type === "chunk" && typeof event.channel === "string") {
    return event.channel;
  }
  return type ?? "event";
}

function eventTone(event: StageCommandLogEvent): StageLogTone {
  const type = eventType(event);
  if (type === "error") {
    return "error";
  }
  if (type !== "log" || typeof event.level !== "string") {
    return "neutral";
  }

  switch (event.level.toLowerCase()) {
    case "debug":
      return "debug";
    case "info":
      return "info";
    case "warn":
    case "warning":
      return "warning";
    case "error":
      return "error";
    case "critical":
    case "fatal":
      return "critical";
    default:
      return "neutral";
  }
}

function eventSummary(event: StageCommandLogEvent): string {
  const type = eventType(event);
  if (typeof event.message === "string" && event.message.trim() !== "") {
    return event.message;
  }
  if (type === "chunk" && typeof event.text === "string") {
    return event.text;
  }
  if (type === "result") {
    if (typeof event.target === "string") {
      return event.target;
    }
    if (typeof event.label === "string") {
      return event.label;
    }
    if (typeof event.command === "string") {
      return `${event.command} result`;
    }
  }
  if (type === "completed" && typeof event.status === "string") {
    return `status ${event.status}`;
  }
  if (type === "started") {
    const command = typeof event.command === "string" ? event.command : "command";
    const argumentsText = Array.isArray(event.arguments)
      ? event.arguments.join(" ")
      : "";
    return `${command}${argumentsText ? ` ${argumentsText}` : ""}`;
  }
  if (typeof event.text === "string" && event.text.trim() !== "") {
    return event.text;
  }
  if (typeof event.target === "string") {
    return event.target;
  }
  if (typeof event.label === "string") {
    return event.label;
  }
  if (typeof event.command === "string") {
    return event.command;
  }
  return type ?? "";
}

function hasJsonDetails(value: unknown): boolean {
  return value !== null && typeof value === "object";
}

function isVisibleLogEvent(event: StageCommandLogEvent): boolean {
  return eventType(event) !== null;
}

export function StageLogsBlock({
  title,
  requestId,
  status,
  events = [],
  error,
}: StageLogsBlockProps) {
  const visibleEvents = events.filter(isVisibleLogEvent);
  return (
    <section className="StageLogsBlock">
      <div className="StageLogsBlock-header">
        <div className="StageLogsBlock-headingGroup">
          <span className="StageLogsBlock-label">Logs</span>
          <h3 className="StageLogsBlock-title">{title ?? requestId}</h3>
        </div>
        <div className="StageLogsBlock-meta">
          <span className="StageLogsBlock-requestId" title={requestId}>
            {requestId}
          </span>
          <span className="StageLogsBlock-count">
            {statusLabel(status, visibleEvents.length)}
          </span>
        </div>
      </div>
      {status === "loading" ? (
        <div className="StageLogsBlock-empty">Loading logs…</div>
      ) : status === "error" ? (
        <div className="StageLogsBlock-empty StageLogsBlock-empty--error">
          {error ?? "Failed to load logs."}
        </div>
      ) : visibleEvents.length === 0 ? (
        <div className="StageLogsBlock-empty">
          No stage events or logs were recorded for this request.
        </div>
      ) : (
        <div className="StageLogsBlock-list">
          {visibleEvents.map((event, index) => {
            const type = eventType(event) ?? "event";
            const detailEntries = Object.entries(event).filter(
              ([key]) => !HIDDEN_LOG_KEYS.has(key),
            );
            const summary = eventSummary(event);
            const badge = eventBadgeLabel(event);
            const tone = eventTone(event);
            const hasDetails = detailEntries.length > 0;
            const content = (
              <>
                <span
                  className={`StageLogsBlock-eventBadge StageLogsBlock-eventBadge--${tone}`}
                >
                  {badge}
                </span>
                <span className="StageLogsBlock-eventSummary">{summary}</span>
                {typeof event.timestamp === "string" ? (
                  <time
                    className="StageLogsBlock-eventTimestamp"
                    dateTime={event.timestamp}
                  >
                    {event.timestamp}
                  </time>
                ) : null}
              </>
            );

            return (
              <article
                key={`${type}-${event.timestamp ?? "na"}-${index}`}
                className={`StageLogsBlock-event StageLogsBlock-event--${tone}`}
              >
                {hasDetails ? (
                  <details className="StageLogsBlock-details">
                    <summary className="StageLogsBlock-summary">{content}</summary>
                    <dl className="StageLogsBlock-fields">
                      {detailEntries.map(([key, value]) => (
                        <div
                          key={key}
                          className={
                            hasJsonDetails(value)
                              ? "StageLogsBlock-field StageLogsBlock-field--json"
                              : "StageLogsBlock-field"
                          }
                        >
                          <dt className="StageLogsBlock-fieldKey">{key}</dt>
                          <dd className="StageLogsBlock-fieldValue">
                            {value === null ||
                            typeof value === "string" ||
                            typeof value === "number" ||
                            typeof value === "boolean" ? (
                              <span className="StageLogsBlock-fieldText">
                                {formatLogValue(value)}
                              </span>
                            ) : (
                              <JsonSyntaxBlock value={value} />
                            )}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                ) : (
                  <div className="StageLogsBlock-summary StageLogsBlock-summary--static">
                    {content}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
