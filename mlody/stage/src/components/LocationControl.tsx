import { Anchor } from "lucide-react";
import type {
  BreadcrumbSegment,
  WorkspaceSummary,
} from "../types.js";
import {
  Breadcrumb,
  BreadcrumbEllipsis,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "./ui/breadcrumb.js";

interface LocationControlProps {
  breadcrumbs: BreadcrumbSegment[];
  workspace: WorkspaceSummary | null;
}

function buildCompactBreadcrumbs(segments: BreadcrumbSegment[]) {
  if (segments.length <= 4) {
    return segments.map((segment, index) => ({
      ...segment,
      key: `${segment.label}-${index}`,
      kind: "segment" as const,
    }));
  }

  const tail = segments.slice(-2);
  return [
    {
      ...segments[0],
      key: `${segments[0]?.label ?? "root"}-0`,
      kind: "segment" as const,
    },
    {
      key: "ellipsis",
      kind: "ellipsis" as const,
    },
    ...tail.map((segment, index) => ({
      ...segment,
      key: `${segment.label}-${segments.length - tail.length + index}`,
      kind: "segment" as const,
    })),
  ];
}

function getRecordValue(
  record: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  if (!record) return null;
  const value = record[key];
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function getWorkspaceStateLabel(workspace: WorkspaceSummary | null): string {
  const sha = getRecordValue(workspace?.info ?? null, "sha");
  if (sha) return sha.slice(0, 8);

  const branch = getRecordValue(workspace?.info ?? null, "branch");
  if (branch) return branch;

  return "unknown";
}

function getWorkspaceMode(workspace: WorkspaceSummary | null): string {
  if (!workspace) {
    return "Unavailable";
  }

  return workspace.fullWorkspace ? "Full workspace" : "Scoped workspace";
}

function getRootsFileLabel(workspace: WorkspaceSummary | null): string {
  if (!workspace?.rootsFile) {
    return "default roots";
  }

  return workspace.rootsFile;
}

export function LocationControl({
  breadcrumbs,
  workspace,
}: LocationControlProps) {
  const compactBreadcrumbs = buildCompactBreadcrumbs(breadcrumbs);
  const branch = getRecordValue(workspace?.info ?? null, "branch");
  const sha = getRecordValue(workspace?.info ?? null, "sha");
  const workspaceUser = getRecordValue(
    workspace?.context?.workspace ?? null,
    "user",
  );
  const runUser = getRecordValue(workspace?.context?.run ?? null, "user");
  const stateLabel = getWorkspaceStateLabel(workspace);

  return (
    <div className="LocationControl" tabIndex={0}>
      <div className="LocationControl-shell">
        <span className="LocationControl-stateBadge">
          <Anchor className="LocationControl-stateIcon" />
          <span>{stateLabel}</span>
        </span>
        <Breadcrumb>
          <BreadcrumbList className="LocationControl-breadcrumbs">
            {compactBreadcrumbs.map((segment, index) => (
              <BreadcrumbItem key={segment.key}>
                {segment.kind === "ellipsis" ? (
                  <>
                    <BreadcrumbEllipsis className="LocationControl-ellipsis" />
                    {index < compactBreadcrumbs.length - 1 && (
                      <BreadcrumbSeparator />
                    )}
                  </>
                ) : (
                  <>
                    {index === compactBreadcrumbs.length - 1 ? (
                      <BreadcrumbPage className="LocationControl-current">
                        {segment.label}
                      </BreadcrumbPage>
                    ) : (
                      <BreadcrumbLink
                        href={segment.href ?? "#"}
                        className="LocationControl-link"
                      >
                        {segment.label}
                      </BreadcrumbLink>
                    )}
                    {index < compactBreadcrumbs.length - 1 && (
                      <BreadcrumbSeparator />
                    )}
                  </>
                )}
              </BreadcrumbItem>
            ))}
          </BreadcrumbList>
        </Breadcrumb>
      </div>

      <div className="LocationControl-popup" role="tooltip">
        <div className="LocationControl-popupHeader">
          <span className="LocationControl-popupEyebrow">Workspace</span>
          <span className="LocationControl-popupState">
            {sha ?? "No commit SHA"}
          </span>
        </div>

        {workspace ? (
          <>
            <dl className="LocationControl-meta">
              <div className="LocationControl-metaRow">
                <dt>Mode</dt>
                <dd>{getWorkspaceMode(workspace)}</dd>
              </div>
              <div className="LocationControl-metaRow">
                <dt>Branch</dt>
                <dd>{branch ?? "Unknown"}</dd>
              </div>
              <div className="LocationControl-metaRow">
                <dt>Workspace</dt>
                <dd>{workspace.workspaceRoot}</dd>
              </div>
              <div className="LocationControl-metaRow">
                <dt>Monorepo</dt>
                <dd>{workspace.monorepoRoot}</dd>
              </div>
              <div className="LocationControl-metaRow">
                <dt>Roots</dt>
                <dd>{getRootsFileLabel(workspace)}</dd>
              </div>
              {(workspaceUser || runUser) && (
                <div className="LocationControl-metaRow">
                  <dt>Users</dt>
                  <dd>
                    {[workspaceUser, runUser]
                      .filter(Boolean)
                      .map((user, index) =>
                        index === 0 ? `workspace ${user}` : `run ${user}`,
                      )
                      .join(" · ")}
                  </dd>
                </div>
              )}
            </dl>

            {workspace.rootInfos.length > 0 && (
              <div className="LocationControl-roots">
                {workspace.rootInfos.map((root) => (
                  <div key={root.name} className="LocationControl-rootChip">
                    <span className="LocationControl-rootName">{root.name}</span>
                    <span className="LocationControl-rootDescription">
                      {root.description}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <p className="LocationControl-unavailable">
            Workspace metadata is not available yet. Stage will update this panel
            as soon as the local server responds.
          </p>
        )}
      </div>
    </div>
  );
}
