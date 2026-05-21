import type { WorkspaceUser } from "./types.js";

type UserLike = Pick<
  WorkspaceUser,
  "name" | "description" | "groups" | "avatar" | "avatarUrl"
> & {
  team?: string;
};

export interface UserTeamMembership {
  team: string;
  isAdmin: boolean;
}

export function getUserDisplayName(user: Pick<UserLike, "name" | "description">): string {
  return user.description?.trim() || user.name;
}

export function getUserInitials(user: Pick<UserLike, "name" | "description">): string {
  const displayName = getUserDisplayName(user);
  const initials = displayName
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => part[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return initials || user.name.slice(0, 2).toUpperCase();
}

export function getNormalizedUserGroups(
  user: Pick<UserLike, "groups">,
): string[] {
  return (
    user.groups?.filter(
      (group, index, allGroups) =>
        group.trim() !== "" && allGroups.indexOf(group) === index,
    ) ?? []
  );
}

export function getTeamMemberships(user: UserLike): UserTeamMembership[] {
  const groups = getNormalizedUserGroups(user);
  const explicitTeam = user.team?.trim() || null;
  const memberships = new Map<string, UserTeamMembership>();

  if (explicitTeam !== null) {
    memberships.set(explicitTeam, {
      team: explicitTeam,
      isAdmin: false,
    });
  }

  if (groups.length === 0 && memberships.size === 0) {
    return [{ team: "workspace", isAdmin: false }];
  }

  for (const group of groups) {
    if (group === "admin") {
      memberships.set("admin", { team: "admin", isAdmin: true });
      continue;
    }

    const adminMatch = group.match(/^(.*)-admin$/);
    if (adminMatch) {
      const baseTeam = adminMatch[1] ?? group;
      const existingMembership = memberships.get(baseTeam);
      memberships.set(baseTeam, {
        team: baseTeam,
        isAdmin: existingMembership?.isAdmin || true,
      });
      continue;
    }

    const existingMembership = memberships.get(group);
    memberships.set(group, {
      team: group,
      isAdmin: existingMembership?.isAdmin ?? false,
    });
  }

  return [...memberships.values()];
}

export function getPrimaryTeam(user: UserLike): string {
  const memberships = getTeamMemberships(user);
  const firstNonAdminMembership = memberships.find(
    (membership) => membership.team !== "admin",
  );
  return firstNonAdminMembership?.team ?? memberships[0]?.team ?? "workspace";
}
