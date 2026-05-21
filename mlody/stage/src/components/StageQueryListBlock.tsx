import { Avatar, AvatarFallback, AvatarImage } from "./ui/avatar.js";
import { normalizeAvatarUrl } from "../serverApi.js";
import type { StageResultPayload } from "../types.js";
import {
  getNormalizedUserGroups,
  getPrimaryTeam,
  getUserDisplayName,
  getUserInitials,
} from "../userPresentation.js";

type StageQueryListPayload = StageResultPayload & {
  view: {
    type: "query-list";
    title?: string;
    entity?: string;
    rowCount?: number;
  };
  data: Record<string, unknown>[];
};

interface StageQueryListBlockProps {
  payload: StageQueryListPayload;
}

interface StageQueryUserRow {
  name: string;
  description?: string;
  avatar?: string;
  team?: string;
  groups?: string[];
}

function readCellText(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.filter((entry): entry is string => typeof entry === "string");
}

function normalizeQueryUserRow(row: Record<string, unknown>): StageQueryUserRow | null {
  const name = readCellText(row.name);
  if (name === null) {
    return null;
  }

  const normalizedRow: StageQueryUserRow = {
    name,
  };

  const description = readCellText(row.description);
  if (description !== null) {
    normalizedRow.description = description;
  }

  const avatar = readCellText(row.avatar);
  if (avatar !== null) {
    normalizedRow.avatar = avatar;
  }

  const team = readCellText(row.team);
  if (team !== null) {
    normalizedRow.team = team;
  }

  const groups = readStringArray(row.groups);
  if (groups.length > 0) {
    normalizedRow.groups = groups;
  }

  return normalizedRow;
}

function renderUsersList(rows: StageQueryUserRow[]) {
  return (
    <div className="StageQueryUserList">
      {rows.map((row) => {
        const displayName = getUserDisplayName(row);
        const shortName = row.name;
        const avatarUrl = normalizeAvatarUrl(row.avatar);
        const team = getPrimaryTeam(row);
        const groups = getNormalizedUserGroups(row);

        return (
          <div key={shortName} className="StageQueryUserCard">
            <div className="StageQueryUserCard-avatarColumn">
              <Avatar size="lg" className="StageQueryUserCard-avatar">
                {avatarUrl ? (
                  <AvatarImage src={avatarUrl} alt={displayName} />
                ) : null}
                <AvatarFallback>{getUserInitials(row)}</AvatarFallback>
              </Avatar>
            </div>
            <div className="StageQueryUserCard-nameColumn">
              <span className="StageQueryUserCard-displayName">
                {displayName}
              </span>
              <span className="StageQueryUserCard-shortName">
                ({shortName})
              </span>
            </div>
            <div className="StageQueryUserCard-teamColumn">
              <span className="StageQueryUserCard-metaLabel">Team</span>
              <span className="StageQueryUserCard-teamValue">{team}</span>
            </div>
            <div className="StageQueryUserCard-groupsColumn">
              <span className="StageQueryUserCard-metaLabel">Groups</span>
              {groups.length > 0 ? (
                <div className="StageQueryUserCard-groups">
                  {groups.map((group) => (
                    <span key={`${shortName}-${group}`} className="StageTableBlock-pill">
                      {group}
                    </span>
                  ))}
                </div>
              ) : (
                <span className="StageTableBlock-value StageTableBlock-value--muted">
                  —
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function StageQueryListBlock({ payload }: StageQueryListBlockProps) {
  const rowCount =
    typeof payload.view.rowCount === "number"
      ? payload.view.rowCount
      : payload.data.length;
  const userRows =
    payload.view.entity === "users"
      ? payload.data
          .map(normalizeQueryUserRow)
          .filter((row): row is StageQueryUserRow => row !== null)
      : [];

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
        ) : payload.view.entity === "users" ? (
          renderUsersList(userRows)
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
