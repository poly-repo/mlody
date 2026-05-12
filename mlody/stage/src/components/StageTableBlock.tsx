import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
} from "@tanstack/react-table";
import type {
  StageEncodedImageCell,
  StageResultPayload,
  StageViewColumn,
} from "../types.js";

type TableRow = Record<string, unknown>;

interface StageTableBlockProps {
  payload: StageResultPayload & {
    view: {
      type: "table";
      title?: string;
      columns: StageViewColumn[];
      rowCount?: number;
      truncated?: boolean;
    };
    data: TableRow[];
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isEncodedImageCell(value: unknown): value is StageEncodedImageCell {
  return (
    isRecord(value) &&
    value.kind === "encoded-image" &&
    typeof value.mimeType === "string" &&
    typeof value.base64 === "string"
  );
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function formatCompactJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: Number.isInteger(value) ? 0 : 2,
  }).format(value);
}

function renderTableValue(value: unknown, column: StageViewColumn): React.ReactNode {
  if (column.display === "image" && isEncodedImageCell(value)) {
    const src = `data:${value.mimeType};base64,${value.base64}`;
    const title = value.path ?? `${value.mimeType} · ${value.byteLength ?? "?"} bytes`;
    return (
      <div className="StageTableBlock-imageCell">
        <img
          className="StageTableBlock-image"
          src={src}
          alt={value.path ?? "Stage result image"}
          title={title}
        />
        {value.path ? (
          <span className="StageTableBlock-imageMeta">{value.path}</span>
        ) : null}
      </div>
    );
  }

  if (column.display === "badge-list" && isStringArray(value)) {
    if (value.length === 0) {
      return (
        <span className="StageTableBlock-value StageTableBlock-value--muted">
          none
        </span>
      );
    }
    return (
      <div className="StageTableBlock-badgeList">
        {value.map((item) => (
          <span key={item} className="StageTableBlock-pill">
            {item}
          </span>
        ))}
      </div>
    );
  }

  if (column.format === "currency" && typeof value === "number") {
    return <span className="StageTableBlock-value StageTableBlock-value--numeric">{formatCurrency(value)}</span>;
  }

  if (typeof value === "number") {
    return <span className="StageTableBlock-value StageTableBlock-value--numeric">{value}</span>;
  }

  if (typeof value === "boolean") {
    return (
      <span className={`StageTableBlock-badge StageTableBlock-badge--${value ? "true" : "false"}`}>
        {String(value)}
      </span>
    );
  }

  if (value === null || value === undefined) {
    return <span className="StageTableBlock-value StageTableBlock-value--muted">null</span>;
  }

  if (typeof value === "string") {
    return <span className="StageTableBlock-value">{value}</span>;
  }

  if (Array.isArray(value)) {
    const compact = formatCompactJson(value);
    return (
      <code className="StageTableBlock-code" title={compact}>
        {compact}
      </code>
    );
  }

  if (isRecord(value)) {
    if (typeof value.path === "string") {
      const meta =
        typeof value.bytes === "string"
          ? ` · ${value.bytes}`
          : undefined;
      return (
        <span className="StageTableBlock-media" title={formatCompactJson(value)}>
          <span className="StageTableBlock-value">{value.path}</span>
          {meta ? <span className="StageTableBlock-mediaMeta">{meta}</span> : null}
        </span>
      );
    }

    const compact = formatCompactJson(value);
    return (
      <code className="StageTableBlock-code" title={compact}>
        {compact}
      </code>
    );
  }

  return <span className="StageTableBlock-value">{String(value)}</span>;
}

function buildColumns(columns: StageViewColumn[]): ColumnDef<TableRow>[] {
  return columns.map((column) => ({
    accessorFn: (row) => row[column.key],
    id: column.key,
    header: column.label,
    cell: (info) => renderTableValue(info.getValue(), column),
  }));
}

export function StageTableBlock({ payload }: StageTableBlockProps) {
  const columns = buildColumns(payload.view.columns);
  const table = useReactTable({
    columns,
    data: payload.data,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (_row, index) => `stage-row-${index}`,
  });

  const rowCount =
    typeof payload.view.rowCount === "number"
      ? payload.view.rowCount
      : payload.data.length;

  return (
    <div className="StageTableBlock">
      <div className="StageTableBlock-header">
        <div className="StageTableBlock-headingGroup">
          <span className="StageTableBlock-label">Table</span>
          {payload.view.title ? (
            <span className="StageTableBlock-title">{payload.view.title}</span>
          ) : null}
        </div>
        <span className="StageTableBlock-count">
          {rowCount} row{rowCount === 1 ? "" : "s"}
          {payload.view.truncated ? " shown" : ""}
        </span>
      </div>
      <div className="StageTableBlock-scroll">
        <table className="StageTableBlock-table">
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th key={header.id} className="StageTableBlock-th">
                    {header.isPlaceholder
                      ? null
                      : flexRender(
                          header.column.columnDef.header,
                          header.getContext(),
                        )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="StageTableBlock-tr">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="StageTableBlock-td">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
