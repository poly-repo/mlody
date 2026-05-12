import type { StageResultPayload } from "../types.js";
import { JsonSyntaxBlock } from "./JsonSyntaxBlock.js";
import { StageTableBlock } from "./StageTableBlock.js";

interface StageResultBlockProps {
  payload: StageResultPayload;
}

function isTableRowArray(value: unknown): value is Record<string, unknown>[] {
  return (
    Array.isArray(value) &&
    value.every((row) => row !== null && typeof row === "object" && !Array.isArray(row))
  );
}

function isTablePayload(
  payload: StageResultPayload,
): payload is StageResultPayload & {
  view: {
    type: "table";
    title?: string;
    columns: { key: string; label: string; format?: string }[];
    rowCount?: number;
    truncated?: boolean;
  };
  data: Record<string, unknown>[];
} {
  return (
    payload.view.type === "table" &&
    Array.isArray(payload.view.columns) &&
    isTableRowArray(payload.data)
  );
}

export function StageResultBlock({ payload }: StageResultBlockProps) {
  if (isTablePayload(payload)) {
    return <StageTableBlock payload={payload} />;
  }

  return <JsonSyntaxBlock value={payload} />;
}
