import type { StageResultPayload } from "../types.js";

type StageQueryListPayload = StageResultPayload & {
  view: {
    type: "query-list";
    title?: string;
    rowCount?: number;
  };
  data: Record<string, unknown>[];
};

interface StageQueryListBlockProps {
  payload: StageQueryListPayload;
}

function readCellText(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

export function StageQueryListBlock({ payload }: StageQueryListBlockProps) {
  const rowCount =
    typeof payload.view.rowCount === "number"
      ? payload.view.rowCount
      : payload.data.length;

  return (
    <div className="StageQueryListBlock SystemAdmonition SystemAdmonition--gray">
      <div className="SystemAdmonition-signal" aria-hidden="true" />
      <div className="SystemAdmonition-content StageQueryListBlock-content">
        <div className="StageQueryListBlock-header">
          <div className="StageQueryListBlock-headingGroup">
            <span className="SystemAdmonition-label">Query</span>
            <span className="SystemAdmonition-title">
              {payload.view.title ?? "Query results"}
            </span>
          </div>
          <span className="StageTableBlock-count">
            {rowCount} entr{rowCount === 1 ? "y" : "ies"}
          </span>
        </div>
        {payload.data.length === 0 ? (
          <p className="StageQueryListBlock-empty">
            No entries matched this query.
          </p>
        ) : (
          <div className="StageTableBlock-scroll">
            <table className="StageTableBlock-table">
              <thead>
                <tr>
                  <th className="StageTableBlock-th">Name</th>
                  <th className="StageTableBlock-th">Description</th>
                </tr>
              </thead>
              <tbody>
                {payload.data.map((row, index) => {
                  const name = readCellText(row.name) ?? "—";
                  const description = readCellText(row.description);
                  return (
                    <tr
                      key={`${name}-${index}`}
                      className="StageTableBlock-tr"
                    >
                      <td className="StageTableBlock-td">
                        <span className="StageTableBlock-value">{name}</span>
                      </td>
                      <td className="StageTableBlock-td">
                        <span
                          className={`StageTableBlock-value StageQueryListBlock-description${description === null ? " StageTableBlock-value--muted" : ""}`}
                        >
                          {description ?? "—"}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
