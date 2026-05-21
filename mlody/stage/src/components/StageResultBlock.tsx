import { Component, type ReactNode } from "react";
import type {
  StageActionGraphData,
  StageDagData,
  StageEntityData,
  StageLineageRow,
  StageResultPayload,
  StageSourceCodeData,
} from "../types.js";
import { StageActionGraphBlock } from "./StageActionGraphBlock.js";
import { JsonSyntaxBlock } from "./JsonSyntaxBlock.js";
import { StageDagBlock } from "./StageDagBlock.js";
import { StageQueryListBlock } from "./StageQueryListBlock.js";
import { StageEntityBlock } from "./StageTaskBlock.js";
import { StageLineageBlock } from "./StageLineageBlock.js";
import { StageScalarBlock } from "./StageScalarBlock.js";
import { StageSourceCodeBlock } from "./StageSourceCodeBlock.js";
import { StageTableBlock } from "./StageTableBlock.js";

interface StageResultBlockProps {
  payload: StageResultPayload;
  mode?: StageResultViewMode;
}

export type StageResultViewMode = "rendered" | "json";

type StageDagPayload = StageResultPayload & {
  view: {
    type: "dag";
    title?: string;
    nodeCount?: number;
    edgeCount?: number;
  };
  data: StageDagData;
};

type StageActionGraphPayload = StageResultPayload & {
  view: {
    type: "action-graph";
    title?: string;
    nodeCount?: number;
    edgeCount?: number;
  };
  data: StageActionGraphData;
};

type StageTablePayload = StageResultPayload & {
  view: {
    type: "table";
    title?: string;
    columns: { key: string; label: string; format?: string }[];
    rowCount?: number;
    truncated?: boolean;
  };
  data: Record<string, unknown>[];
};

type StageLineagePayload = StageResultPayload & {
  view: {
    type: "lineage";
    title?: string;
    rowCount?: number;
  };
  data: StageLineageRow[];
};

type StageSourceCodePayload = StageResultPayload & {
  view: {
    type: "source-code";
    title?: string;
  };
  data: StageSourceCodeData;
};

type StageScalarPayload = StageResultPayload & {
  view: {
    type: "json";
    title?: string;
  };
  data: string | number | boolean | null;
};

type StageEntityPayload = StageResultPayload & {
  view: {
    type: "task" | "action";
    title?: string;
  };
  data: StageEntityData;
};

type StageResultListPayload = StageResultPayload & {
  view: {
    type: "result-list";
    title?: string;
    rowCount?: number;
  };
  data: StageResultPayload[];
};

type StageQueryListPayload = StageResultPayload & {
  view: {
    type: "query-list";
    title?: string;
    rowCount?: number;
  };
  data: Record<string, unknown>[];
};

type StageSpecializedRenderer =
  | {
      kind: "query-list";
      payload: StageQueryListPayload;
    }
  | {
      kind: "table";
      payload: StageTablePayload;
    }
  | {
      kind: "lineage";
      payload: StageLineagePayload;
    }
  | {
      kind: "dag";
      payload: StageDagPayload;
    }
  | {
      kind: "action-graph";
      payload: StageActionGraphPayload;
    }
  | {
      kind: "source-code";
      payload: StageSourceCodePayload;
    }
  | {
      kind: "scalar";
      payload: StageScalarPayload;
    }
  | {
      kind: "task";
      payload: StageEntityPayload;
    }
  | {
      kind: "result-list";
      payload: StageResultListPayload;
    };

interface DagRenderBoundaryProps {
  payload: StageDagPayload | StageActionGraphPayload;
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
): payload is StageTablePayload {
  return (
    payload.view.type === "table" &&
    Array.isArray(payload.view.columns) &&
    isTableRowArray(payload.data)
  );
}

function isQueryListPayload(
  payload: StageResultPayload,
): payload is StageQueryListPayload {
  return payload.view.type === "query-list" && isTableRowArray(payload.data);
}

function isStageDagData(value: unknown): value is StageDagData {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return Array.isArray(record.nodes) && Array.isArray(record.edges);
}

function isStageActionGraphData(value: unknown): value is StageActionGraphData {
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

function isActionGraphPayload(
  payload: StageResultPayload,
): payload is StageActionGraphPayload {
  return (
    payload.view.type === "action-graph" &&
    isStageActionGraphData(payload.data)
  );
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
): payload is StageLineagePayload {
  return payload.view.type === "lineage" && isLineageRowArray(payload.data);
}

function isStageSourceCodeData(value: unknown): value is StageSourceCodeData {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.path === "string" &&
    typeof candidate.language === "string" &&
    typeof candidate.startLine === "number" &&
    typeof candidate.endLine === "number" &&
    typeof candidate.code === "string"
  );
}

function isSourceCodePayload(
  payload: StageResultPayload,
): payload is StageSourceCodePayload {
  return payload.view.type === "source-code" && isStageSourceCodeData(payload.data);
}

function isScalarPayload(payload: StageResultPayload): payload is StageScalarPayload {
  return (
    payload.view.type === "json" &&
    (payload.data === null ||
      typeof payload.data === "string" ||
      typeof payload.data === "number" ||
      typeof payload.data === "boolean")
  );
}

function isTaskPortArray(value: unknown): value is StageEntityData["inputs"] {
  return (
    Array.isArray(value) &&
    value.every((port) => {
      if (port === null || typeof port !== "object" || Array.isArray(port)) {
        return false;
      }
      const candidate = port as Record<string, unknown>;
      return (
        typeof candidate.name === "string" &&
        typeof candidate.type === "string" &&
        typeof candidate.description === "string" &&
        typeof candidate.detailsText === "string" &&
        Array.isArray(candidate.details)
      );
    })
  );
}

function isTaskAttributeArray(
  value: unknown,
): value is StageEntityData["attributes"] {
  return (
    Array.isArray(value) &&
    value.every((attribute) => {
      if (
        attribute === null ||
        typeof attribute !== "object" ||
        Array.isArray(attribute)
      ) {
        return false;
      }
      const candidate = attribute as Record<string, unknown>;
      return (
        typeof candidate.name === "string" &&
        typeof candidate.value === "string" &&
        typeof candidate.detailsText === "string" &&
        Array.isArray(candidate.details)
      );
    })
  );
}

function isTaskSectionArray(value: unknown): value is StageEntityData["sections"] {
  return (
    Array.isArray(value) &&
    value.every((section) => {
      if (section === null || typeof section !== "object" || Array.isArray(section)) {
        return false;
      }
      const candidate = section as Record<string, unknown>;
      return (
        typeof candidate.key === "string" &&
        typeof candidate.label === "string" &&
        isTaskPortArray(candidate.values)
      );
    })
  );
}

function isStageResultPayloadArray(value: unknown): value is StageResultPayload[] {
  return (
    Array.isArray(value) &&
    value.every((entry) => {
      if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
        return false;
      }
      const candidate = entry as Record<string, unknown>;
      return (
        candidate.view !== null &&
        typeof candidate.view === "object" &&
        "type" in candidate.view &&
        typeof (candidate.view as Record<string, unknown>).type === "string" &&
        "data" in candidate
      );
    })
  );
}

function isStageTaskData(value: unknown): value is StageEntityData {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.name === "string" &&
    typeof candidate.description === "string" &&
    isTaskAttributeArray(candidate.attributes) &&
    isTaskPortArray(candidate.inputs) &&
    isTaskPortArray(candidate.outputs) &&
    isTaskPortArray(candidate.config) &&
    isTaskSectionArray(candidate.sections)
  );
}

function isTaskPayload(payload: StageResultPayload): payload is StageEntityPayload {
  return (
    (payload.view.type === "task" || payload.view.type === "action") &&
    isStageTaskData(payload.data)
  );
}

function isResultListPayload(
  payload: StageResultPayload,
): payload is StageResultListPayload {
  return (
    payload.view.type === "result-list" && isStageResultPayloadArray(payload.data)
  );
}

function resolveSpecializedRenderer(
  payload: StageResultPayload,
): StageSpecializedRenderer | null {
  if (isQueryListPayload(payload)) {
    return { kind: "query-list", payload };
  }
  if (isTablePayload(payload)) {
    return { kind: "table", payload };
  }
  if (isLineagePayload(payload)) {
    return { kind: "lineage", payload };
  }
  if (isDagPayload(payload)) {
    return { kind: "dag", payload };
  }
  if (isActionGraphPayload(payload)) {
    return { kind: "action-graph", payload };
  }
  if (isSourceCodePayload(payload)) {
    return { kind: "source-code", payload };
  }
  if (isScalarPayload(payload)) {
    return { kind: "scalar", payload };
  }
  if (isTaskPayload(payload)) {
    return { kind: "task", payload };
  }
  if (isResultListPayload(payload)) {
    return { kind: "result-list", payload };
  }

  return null;
}

export function hasSpecializedStageRenderer(payload: StageResultPayload): boolean {
  return resolveSpecializedRenderer(payload) !== null;
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

export function StageResultBlock({
  payload,
  mode = "rendered",
}: StageResultBlockProps) {
  if (mode === "json") {
    return <JsonSyntaxBlock value={payload} />;
  }
  const specializedRenderer = resolveSpecializedRenderer(payload);

  if (specializedRenderer?.kind === "query-list") {
    return <StageQueryListBlock payload={specializedRenderer.payload} />;
  }
  if (specializedRenderer?.kind === "table") {
    return <StageTableBlock payload={specializedRenderer.payload} />;
  }
  if (specializedRenderer?.kind === "lineage") {
    return <StageLineageBlock payload={specializedRenderer.payload} />;
  }
  if (specializedRenderer?.kind === "dag") {
    return (
      <DagRenderBoundary payload={specializedRenderer.payload}>
        <StageDagBlock payload={specializedRenderer.payload} />
      </DagRenderBoundary>
    );
  }
  if (specializedRenderer?.kind === "action-graph") {
    return (
      <DagRenderBoundary payload={specializedRenderer.payload}>
        <StageActionGraphBlock payload={specializedRenderer.payload} />
      </DagRenderBoundary>
    );
  }
  if (specializedRenderer?.kind === "source-code") {
    return <StageSourceCodeBlock payload={specializedRenderer.payload} />;
  }
  if (specializedRenderer?.kind === "scalar") {
    return <StageScalarBlock payload={specializedRenderer.payload} />;
  }
  if (specializedRenderer?.kind === "task") {
    return <StageEntityBlock payload={specializedRenderer.payload} />;
  }
  if (specializedRenderer?.kind === "result-list") {
    return (
      <div className="StageResultBlock-resultList">
        {specializedRenderer.payload.data.map((result, index) => (
          <div
            className="StageResultBlock-resultListItem"
            key={`${index}-${result.view.type}`}
          >
            <StageResultBlock payload={result} mode={mode} />
          </div>
        ))}
      </div>
    );
  }

  return <JsonSyntaxBlock value={payload} />;
}
