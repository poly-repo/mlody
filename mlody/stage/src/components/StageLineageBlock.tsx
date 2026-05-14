import type { StageLineageRow, StageResultPayload } from "../types.js";

interface StageLineageBlockProps {
  payload: StageResultPayload & {
    view: {
      type: "lineage";
      title?: string;
      rowCount?: number;
    };
    data: StageLineageRow[];
  };
}

function formatLineageValue(value: unknown): {
  text: string;
  title?: string;
  muted?: boolean;
  code?: boolean;
} {
  if (value === null || value === undefined) {
    return { text: "null", muted: true };
  }
  if (typeof value === "string") {
    return { text: value };
  }
  if (
    typeof value === "number" ||
    typeof value === "boolean" ||
    typeof value === "bigint"
  ) {
    return { text: String(value) };
  }

  try {
    const compact = JSON.stringify(value);
    return { text: compact, title: compact, code: true };
  } catch {
    return { text: String(value) };
  }
}

export function StageLineageBlock({ payload }: StageLineageBlockProps) {
  const rowCount =
    typeof payload.view.rowCount === "number"
      ? payload.view.rowCount
      : payload.data.length;

  return (
    <div className="StageLineageBlock">
      <div className="StageLineageBlock-header">
        <div className="StageLineageBlock-headingGroup">
          <span className="StageLineageBlock-label">Lineage</span>
          {payload.view.title ? (
            <span className="StageLineageBlock-title">{payload.view.title}</span>
          ) : null}
        </div>
        <span className="StageLineageBlock-count">
          {rowCount} entr{rowCount === 1 ? "y" : "ies"}
        </span>
      </div>
      <div className="StageLineageBlock-scroll">
        <table className="StageLineageBlock-table">
          <thead>
            <tr>
              <th className="StageLineageBlock-th">source</th>
              <th className="StageLineageBlock-th">value</th>
            </tr>
          </thead>
          <tbody>
            {payload.data.length === 0 ? (
              <tr className="StageLineageBlock-tr StageLineageBlock-tr--empty">
                <td className="StageLineageBlock-td" />
                <td className="StageLineageBlock-td">
                  <span className="StageLineageBlock-value StageLineageBlock-value--muted">
                    (empty)
                  </span>
                </td>
              </tr>
            ) : (
              payload.data.map((row, index) => {
                const formatted = formatLineageValue(row.value);
                const stateClass = row.active
                  ? "StageLineageBlock-tr--active"
                  : "StageLineageBlock-tr--inactive";
                const valueClass = [
                  "StageLineageBlock-value",
                  formatted.code ? "StageLineageBlock-value--code" : "",
                  formatted.muted ? "StageLineageBlock-value--muted" : "",
                ]
                  .filter(Boolean)
                  .join(" ");

                return (
                  <tr
                    key={`${row.source}-${index}`}
                    className={`StageLineageBlock-tr ${stateClass}`}
                  >
                    <td className="StageLineageBlock-td">
                      <span className="StageLineageBlock-source">{row.source}</span>
                    </td>
                    <td className="StageLineageBlock-td">
                      <span className={valueClass} title={formatted.title}>
                        {formatted.text}
                      </span>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
