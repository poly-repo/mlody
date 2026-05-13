import { Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  CommandOption,
  LocationCrumb,
  UserSummary,
  WorkspaceUser,
  WorkspaceSummary,
} from "../types.js";
import { LocationControl } from "./LocationControl.js";
import {
  Avatar,
  AvatarBadge,
  AvatarFallback,
  AvatarImage,
} from "./ui/avatar.js";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select.js";

interface InputToolbarProps {
  commandOptions: CommandOption[];
  currentCommand: string;
  location: LocationCrumb[];
  topdir: string;
  availableWorkspaces: WorkspaceSummary[];
  workspace: WorkspaceSummary | null;
  showLocation: boolean;
  availableUsers: WorkspaceUser[];
  currentUserName: string;
  currentUser: UserSummary;
  onCommandChange: (command: string) => void;
  onCurrentUserChange: (name: string) => void;
  onWorkspaceChange: (workspace: WorkspaceSummary | null) => void;
}

interface UserTeamMember {
  team: string;
  user: WorkspaceUser;
  isAdmin: boolean;
}

interface UserTeamRow {
  team: string;
  members: UserTeamMember[];
}

function getUserDisplayName(user: WorkspaceUser): string {
  return user.description?.trim() || user.name;
}

function compareUserDisplayName(a: WorkspaceUser, b: WorkspaceUser): number {
  return (
    getUserDisplayName(a).localeCompare(getUserDisplayName(b)) ||
    a.name.localeCompare(b.name)
  );
}

function compareTeamName(a: string, b: string): number {
  if (a === "admin") return -1;
  if (b === "admin") return 1;
  return a.localeCompare(b);
}

function getTeamMemberships(user: WorkspaceUser): UserTeamMember[] {
  const groups =
    user.groups?.filter((group, index, allGroups) =>
      group.trim() !== "" && allGroups.indexOf(group) === index,
    ) ?? [];

  if (groups.length === 0) {
    return [{ team: "workspace", user, isAdmin: false }];
  }

  const memberships = new Map<string, UserTeamMember>();

  for (const group of groups) {
    if (group === "admin") {
      memberships.set("admin", { team: "admin", user, isAdmin: true });
      continue;
    }

    const adminMatch = group.match(/^(.*)-admin$/);
    if (adminMatch) {
      const baseTeam = adminMatch[1] ?? group;
      const existingMembership = memberships.get(baseTeam);
      memberships.set(baseTeam, {
        team: baseTeam,
        user,
        isAdmin: existingMembership?.isAdmin || true,
      });
      continue;
    }

    const existingMembership = memberships.get(group);
    memberships.set(group, {
      team: group,
      user,
      isAdmin: existingMembership?.isAdmin ?? false,
    });
  }

  return [...memberships.values()];
}

function buildUserTeamRows(users: WorkspaceUser[]): UserTeamRow[] {
  const rows = new Map<string, UserTeamMember[]>();

  for (const user of users) {
    for (const membership of getTeamMemberships(user)) {
      const rowKey = membership.team;
      const row = rows.get(rowKey) ?? [];
      const existingIndex = row.findIndex(
        (entry) => entry.user.name === membership.user.name,
      );
      if (existingIndex >= 0) {
        row[existingIndex] = {
          team: rowKey,
          user: membership.user,
          isAdmin: row[existingIndex]!.isAdmin || membership.isAdmin,
        };
      } else {
        row.push(membership);
      }
      rows.set(rowKey, row);
    }
  }

  return [...rows.entries()]
    .sort(([teamA], [teamB]) => compareTeamName(teamA, teamB))
    .map(([team, teamUsers]) => ({
      team,
      members: [...teamUsers].sort((a, b) => {
        if (a.isAdmin !== b.isAdmin) {
          return a.isAdmin ? -1 : 1;
        }
        return compareUserDisplayName(a.user, b.user);
      }),
    }));
}

export function InputToolbar({
  commandOptions,
  currentCommand,
  location,
  topdir,
  availableWorkspaces,
  workspace,
  showLocation,
  availableUsers,
  currentUserName,
  currentUser,
  onCommandChange,
  onCurrentUserChange,
  onWorkspaceChange,
}: InputToolbarProps) {
  const currentCommandOption =
    commandOptions.find((option) => option.value === currentCommand) ?? null;
  const [userPickerOpen, setUserPickerOpen] = useState(false);
  const userPickerRef = useRef<HTMLDivElement>(null);
  const userPickerButtonRef = useRef<HTMLButtonElement>(null);
  const userTeamRows = useMemo(
    () => buildUserTeamRows(availableUsers),
    [availableUsers],
  );

  useEffect(() => {
    if (!userPickerOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent | TouchEvent) {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }

      if (userPickerRef.current?.contains(target)) {
        return;
      }

      setUserPickerOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") {
        return;
      }

      event.preventDefault();
      setUserPickerOpen(false);
      userPickerButtonRef.current?.focus();
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [userPickerOpen]);

  function toggleUserPicker() {
    setUserPickerOpen((open) => !open);
  }

  function openUserPickerFromContextMenu(
    event: React.MouseEvent<HTMLButtonElement>,
  ) {
    event.preventDefault();
    setUserPickerOpen(true);
  }

  function handleUserSelect(name: string) {
    onCurrentUserChange(name);
    setUserPickerOpen(false);
    userPickerButtonRef.current?.focus();
  }

  return (
    <div className="CommandToolbar">
      <div className="CommandToolbar-command">
        <div className="CommandToolbar-commandControl">
          <Select value={currentCommand} onValueChange={onCommandChange}>
            <SelectTrigger
              aria-label="Current command"
              className="CommandToolbar-select"
            >
              <SelectValue placeholder="Select a command" />
            </SelectTrigger>
            <SelectContent className="CommandToolbar-selectContent">
              {commandOptions.map((option) => (
                <SelectItem
                  key={option.value}
                  value={option.value}
                  title={option.description}
                >
                  <span className="CommandToolbar-option">
                    <strong>{option.label}</strong>
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {currentCommandOption?.description && (
            <div className="CommandToolbar-commandTooltip" role="tooltip">
              <span className="CommandToolbar-commandTooltipTitle">
                {currentCommandOption.label}
              </span>
              <span>{currentCommandOption.description}</span>
            </div>
          )}
        </div>
      </div>

      <div className="CommandToolbar-path">
        {showLocation ? (
          <LocationControl
            location={location}
            topdir={topdir}
            availableWorkspaces={availableWorkspaces}
            workspace={workspace}
            onWorkspaceChange={onWorkspaceChange}
          />
        ) : null}
      </div>

      <div className="CommandToolbar-user">
        <div
          ref={userPickerRef}
          className="CommandToolbar-userMenu"
          data-picker-open={userPickerOpen ? "true" : undefined}
        >
          <button
            ref={userPickerButtonRef}
            type="button"
            className="CommandToolbar-userCard"
            aria-label="Choose current user"
            aria-haspopup="dialog"
            aria-expanded={userPickerOpen}
            onClick={toggleUserPicker}
            onContextMenu={openUserPickerFromContextMenu}
          >
            <Avatar size="lg" className="CommandToolbar-avatar">
              {currentUser.avatarUrl && (
                <AvatarImage src={currentUser.avatarUrl} alt={currentUser.name} />
              )}
              <AvatarFallback>{currentUser.initials}</AvatarFallback>
              <AvatarBadge>
                <Sparkles className="CommandToolbar-badgeIcon" />
              </AvatarBadge>
            </Avatar>
          </button>
          <div className="CommandToolbar-userPopup" role="tooltip">
            <span className="CommandToolbar-userName">{currentUser.name}</span>
            <span className="CommandToolbar-userRole">{currentUser.role}</span>
          </div>
          {userPickerOpen ? (
            <div className="CommandToolbar-userPicker" role="dialog" aria-label="User picker">
              <div className="CommandToolbar-userPickerHeader">
                <span className="CommandToolbar-userPickerEyebrow">Current user</span>
                <span className="CommandToolbar-userPickerCurrent">
                  {currentUser.name}
                </span>
              </div>
              {userTeamRows.length === 0 ? (
                <p className="CommandToolbar-userPickerEmpty">
                  No workspace users are currently available.
                </p>
              ) : (
                <div className="CommandToolbar-userPickerRows">
                  {userTeamRows.map((row) => (
                    <div key={row.team} className="CommandToolbar-userPickerRow">
                      <div className="CommandToolbar-userPickerTeam">
                        {row.team}
                      </div>
                      <div className="CommandToolbar-userPickerUsers">
                        {row.members.map(({ user, isAdmin }) => {
                          const displayName = getUserDisplayName(user);
                          const initials =
                            displayName
                              .split(/\s+/)
                              .filter(Boolean)
                              .map((part) => part[0] ?? "")
                              .join("")
                              .slice(0, 2)
                              .toUpperCase() || user.name.slice(0, 2).toUpperCase();
                          const isSelected = user.name === currentUserName;

                          return (
                            <button
                              key={`${row.team}-${user.name}`}
                              type="button"
                              className="CommandToolbar-userPickerChoice"
                              data-selected={isSelected ? "true" : undefined}
                              data-admin={isAdmin ? "true" : undefined}
                              onClick={() => handleUserSelect(user.name)}
                            >
                              <Avatar
                                size="default"
                                className="CommandToolbar-userPickerAvatar"
                              >
                                {user.avatarUrl && (
                                  <AvatarImage src={user.avatarUrl} alt={displayName} />
                                )}
                                <AvatarFallback>{initials}</AvatarFallback>
                              </Avatar>
                              <span className="CommandToolbar-userPickerLabel">
                                {displayName}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
