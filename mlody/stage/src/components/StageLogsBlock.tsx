import type { StageCommandLogEvent } from "../types.js";
import { JsonSyntaxBlock } from "./JsonSyntaxBlock.js";

interface StageLogsBlockProps {
  title?: string;
  requestId: string;
  status: "loading" | "loaded" | "error";
  events?: StageCommandLogEvent[];
  error?: string;
}

const HIDDEN_LOG_KEYS = new Set(["event", "requestId", "timestamp"]);

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

export function StageLogsBlock({
  title,
  requestId,
  status,
  events = [],
  error,
}: StageLogsBlockProps) {
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
            {statusLabel(status, events.length)}
          </span>
        </div>
      </div>
      {status === "loading" ? (
        <div className="StageLogsBlock-empty">Loading logs…</div>
      ) : status === "error" ? (
        <div className="StageLogsBlock-empty StageLogsBlock-empty--error">
          {error ?? "Failed to load logs."}
        </div>
      ) : events.length === 0 ? (
        <div className="StageLogsBlock-empty">No logs were recorded for this request.</div>
      ) : (
        <div className="StageLogsBlock-list">
          {events.map((event, index) => {
            const detailEntries = Object.entries(event).filter(
              ([key]) => !HIDDEN_LOG_KEYS.has(key),
            );
            return (
              <article
                key={`${event.event}-${event.timestamp ?? "na"}-${index}`}
                className="StageLogsBlock-event"
              >
                <div className="StageLogsBlock-eventHeader">
                  <span className="StageLogsBlock-eventName">{event.event}</span>
                  {typeof event.timestamp === "string" ? (
                    <time
                      className="StageLogsBlock-eventTimestamp"
                      dateTime={event.timestamp}
                    >
                      {event.timestamp}
                    </time>
                  ) : null}
                </div>
                {detailEntries.length > 0 ? (
                  <dl className="StageLogsBlock-fields">
                    {detailEntries.map(([key, value]) => (
                      <div
                        key={key}
                        className={
                          value !== null && typeof value === "object"
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
                ) : null}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
