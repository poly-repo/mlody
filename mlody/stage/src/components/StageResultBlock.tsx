import { Component, type ReactNode } from "react";
import type { StageDagData, StageLineageRow, StageResultPayload } from "../types.js";
import { JsonSyntaxBlock } from "./JsonSyntaxBlock.js";
import { StageDagBlock } from "./StageDagBlock.js";
import { StageLineageBlock } from "./StageLineageBlock.js";
import { StageTableBlock } from "./StageTableBlock.js";

interface StageResultBlockProps {
  payload: StageResultPayload;
}

type StageDagPayload = StageResultPayload & {
  view: {
    type: "dag";
    title?: string;
    nodeCount?: number;
    edgeCount?: number;
  };
  data: StageDagData;
};

interface DagRenderBoundaryProps {
  payload: StageDagPayload;
  children: ReactNode;
}

interface DagRenderBoundaryState {
  error: Error | null;
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

function isStageDagData(value: unknown): value is StageDagData {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return Array.isArray(record.nodes) && Array.isArray(record.edges);
}

function isDagPayload(
  payload: StageResultPayload,
): payload is StageDagPayload {
  return payload.view.type === "dag" && isStageDagData(payload.data);
}

function isLineageRowArray(value: unknown): value is StageLineageRow[] {
  return (
    Array.isArray(value) &&
    value.every((row) => {
      if (row === null || typeof row !== "object" || Array.isArray(row)) {
        return false;
      }
      const candidate = row as Record<string, unknown>;
      return (
        typeof candidate.source === "string" &&
        typeof candidate.active === "boolean"
      );
    })
  );
}

function isLineagePayload(
  payload: StageResultPayload,
): payload is StageResultPayload & {
  view: {
    type: "lineage";
    title?: string;
    rowCount?: number;
  };
  data: StageLineageRow[];
} {
  return payload.view.type === "lineage" && isLineageRowArray(payload.data);
}

class DagRenderBoundary extends Component<
  DagRenderBoundaryProps,
  DagRenderBoundaryState
> {
  override state: DagRenderBoundaryState = {
    error: null,
  };

  static getDerivedStateFromError(error: Error): DagRenderBoundaryState {
    return { error };
  }

  override componentDidUpdate(prevProps: DagRenderBoundaryProps) {
    if (this.state.error !== null && prevProps.payload !== this.props.payload) {
      this.setState({ error: null });
    }
  }

  override render() {
    if (this.state.error !== null) {
      return (
        <div className="StageResultBlock-errorFallback">
          <div className="StageResultBlock-errorHeader">
            <span className="StageResultBlock-errorLabel">DAG renderer failed</span>
            <span className="StageResultBlock-errorMessage">
              {this.state.error.message}
            </span>
          </div>
          <JsonSyntaxBlock value={this.props.payload} />
        </div>
      );
    }

    return this.props.children;
  }
}

export function StageResultBlock({ payload }: StageResultBlockProps) {
  if (isTablePayload(payload)) {
    return <StageTableBlock payload={payload} />;
  }
  if (isLineagePayload(payload)) {
    return <StageLineageBlock payload={payload} />;
  }
  if (isDagPayload(payload)) {
    return (
      <DagRenderBoundary payload={payload}>
        <StageDagBlock payload={payload} />
      </DagRenderBoundary>
    );
  }

  return <JsonSyntaxBlock value={payload} />;
}
