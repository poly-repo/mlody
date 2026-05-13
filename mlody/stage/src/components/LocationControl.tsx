import { Anchor } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  LocationCrumb,
  LocationPiece,
  WorkspaceSummary,
} from "../types.js";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "./ui/breadcrumb.js";

interface LocationControlProps {
  location: LocationCrumb[];
  topdir: string;
  availableWorkspaces: WorkspaceSummary[];
  workspace: WorkspaceSummary | null;
  onWorkspaceChange: (workspace: WorkspaceSummary | null) => void;
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

function normalizePath(value: string): string {
  return value.replace(/\/+$/, "");
}

function getWorkspaceTopdir(workspace: WorkspaceSummary): string {
  const monorepoRoot = normalizePath(workspace.monorepoRoot);
  const workspaceRoot = normalizePath(workspace.workspaceRoot);

  if (workspaceRoot === monorepoRoot) {
    return "/";
  }

  if (workspaceRoot.startsWith(`${monorepoRoot}/`)) {
    return workspaceRoot.slice(monorepoRoot.length + 1);
  }

  return workspace.workspaceRoot;
}

function renderLocationPiece(piece: LocationPiece, crumbId: string) {
  return (
    <span
      key={`${crumbId}-${piece.kind}-${piece.text}`}
      className={`LocationControl-piece LocationControl-piece--${piece.kind}`}
    >
      {piece.text}
    </span>
  );
}

export function LocationControl({
  location,
  topdir,
  availableWorkspaces,
  workspace,
  onWorkspaceChange,
}: LocationControlProps) {
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const workspacePickerRef = useRef<HTMLDivElement>(null);
  const workspaceButtonRef = useRef<HTMLButtonElement>(null);
  const branch = getRecordValue(workspace?.info ?? null, "branch");
  const sha = getRecordValue(workspace?.info ?? null, "sha");
  const workspaceUser = getRecordValue(
    workspace?.context?.workspace ?? null,
    "user",
  );
  const runUser = getRecordValue(workspace?.context?.run ?? null, "user");
  const stateLabel = getWorkspaceStateLabel(workspace);
  const workspaceChoices = useMemo(() => {
    const selectedRoot = workspace ? normalizePath(workspace.workspaceRoot) : null;
    const seen = new Set<string>();
    const ordered: WorkspaceSummary[] = [];

    if (workspace) {
      ordered.push(workspace);
      seen.add(selectedRoot ?? workspace.workspaceRoot);
    }

    const remaining = availableWorkspaces
      .filter((candidate) => {
        const key = normalizePath(candidate.workspaceRoot);
        if (seen.has(key)) {
          return false;
        }
        seen.add(key);
        return true;
      })
      .sort((left, right) => {
        return (
          getWorkspaceTopdir(left).localeCompare(getWorkspaceTopdir(right)) ||
          left.workspaceRoot.localeCompare(right.workspaceRoot)
        );
      });

    return [...ordered, ...remaining];
  }, [availableWorkspaces, workspace]);

  useEffect(() => {
    if (!workspacePickerOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent | TouchEvent) {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }

      if (workspacePickerRef.current?.contains(target)) {
        return;
      }

      if (workspaceButtonRef.current?.contains(target)) {
        return;
      }

      setWorkspacePickerOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") {
        return;
      }

      event.preventDefault();
      setWorkspacePickerOpen(false);
      workspaceButtonRef.current?.focus();
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [workspacePickerOpen]);

  function handleWorkspaceSelect(nextWorkspace: WorkspaceSummary) {
    onWorkspaceChange(nextWorkspace);
    setWorkspacePickerOpen(false);
    workspaceButtonRef.current?.focus();
  }

  return (
    <div
      className="LocationControl"
      tabIndex={0}
      data-workspace-picker-open={workspacePickerOpen ? "true" : undefined}
    >
      <div className="LocationControl-shell">
        <div className="LocationControl-primaryRow">
          <span className="LocationControl-stateBadge">
            <Anchor className="LocationControl-stateIcon" />
            <span>{stateLabel}</span>
          </span>
          <Breadcrumb>
            <BreadcrumbList className="LocationControl-breadcrumbs">
              {location.map((crumb, index) => (
                <BreadcrumbItem key={crumb.id}>
                  {index === location.length - 1 ? (
                    <BreadcrumbPage className="LocationControl-segment LocationControl-segment--current">
                      {crumb.pieces.map((piece) => renderLocationPiece(piece, crumb.id))}
                    </BreadcrumbPage>
                  ) : (
                    <BreadcrumbLink
                      href={crumb.href ?? "#"}
                      className="LocationControl-segment LocationControl-segment--link"
                    >
                      {crumb.pieces.map((piece) => renderLocationPiece(piece, crumb.id))}
                    </BreadcrumbLink>
                  )}
                  {index < location.length - 1 && <BreadcrumbSeparator />}
                </BreadcrumbItem>
              ))}
            </BreadcrumbList>
          </Breadcrumb>
        </div>
        {topdir ? (
          <button
            ref={workspaceButtonRef}
            type="button"
            className="LocationControl-topdirButton"
            title={workspace?.workspaceRoot ?? topdir}
            aria-label="Choose workspace"
            aria-haspopup="dialog"
            aria-expanded={workspacePickerOpen}
            onClick={() => setWorkspacePickerOpen((open) => !open)}
          >
            <span className="LocationControl-topdir">{topdir}</span>
            <span className="LocationControl-topdirCaret" aria-hidden="true">
              ▾
            </span>
          </button>
        ) : null}
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

      {workspacePickerOpen ? (
        <div
          ref={workspacePickerRef}
          className="LocationControl-workspacePicker"
          role="dialog"
          aria-label="Workspace picker"
        >
          <div className="LocationControl-workspacePickerHeader">
            <span className="LocationControl-workspacePickerEyebrow">
              Available workspaces
            </span>
            <span className="LocationControl-workspacePickerCurrent">
              {workspace ? getWorkspaceTopdir(workspace) : "Unavailable"}
            </span>
          </div>
          {workspaceChoices.length === 0 ? (
            <p className="LocationControl-workspacePickerEmpty">
              No workspace entries are available yet.
            </p>
          ) : (
            <div className="LocationControl-workspacePickerList">
              {workspaceChoices.map((candidate) => {
                const candidateTopdir = getWorkspaceTopdir(candidate);
                const isSelected =
                  workspace !== null &&
                  normalizePath(workspace.workspaceRoot) ===
                    normalizePath(candidate.workspaceRoot);
                const candidateSha = getRecordValue(candidate.info ?? null, "sha");

                return (
                  <button
                    key={candidate.workspaceRoot}
                    type="button"
                    className="LocationControl-workspacePickerChoice"
                    data-selected={isSelected ? "true" : undefined}
                    onClick={() => handleWorkspaceSelect(candidate)}
                  >
                    <span className="LocationControl-workspacePickerLabelRow">
                      <span className="LocationControl-workspacePickerLabel">
                        {candidateTopdir}
                      </span>
                      <span className="LocationControl-workspacePickerMode">
                        {candidateSha ? candidateSha.slice(0, 8) : getWorkspaceMode(candidate)}
                      </span>
                    </span>
                    <span className="LocationControl-workspacePickerPath">
                      {candidate.workspaceRoot}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
