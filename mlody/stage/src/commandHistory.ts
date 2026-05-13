import type { WorkspaceSummary } from "./types.js";

const COMMAND_HISTORY_STORAGE_KEY = "mlody.stage.command-history.v1";
const MAX_COMMAND_HISTORY = 200;

export interface CommandHistorySnapshot {
  command: string;
  value: string;
  promotedSegments: string[];
}

export interface CommandHistoryEntry {
  id: string;
  createdAt: string;
  command: string;
  prompt: string;
  breadcrumb: string[];
  workspace: WorkspaceSummary | null;
}

export interface CommandHistoryMatch {
  entry: CommandHistoryEntry;
  index: number;
}

export function buildCommandInput(
  command: string,
  segments: string[],
  remainder: string,
): string {
  const filteredSegments = segments.filter((segment) => segment.trim() !== "");
  const trimmedRemainder = remainder.trim();

  let combinedInput = "";
  filteredSegments.forEach((segment, index) => {
    if (index === 0) {
      combinedInput = segment;
      return;
    }

    const previousSegment = filteredSegments[index - 1] ?? "";
    if (previousSegment.endsWith(":")) {
      combinedInput += segment;
      return;
    }

    if (index === 1 && filteredSegments[0]?.startsWith("@")) {
      combinedInput += `//${segment}`;
      return;
    }

    combinedInput += `/${segment}`;
  });

  if (trimmedRemainder !== "") {
    if (combinedInput === "") {
      combinedInput = trimmedRemainder;
    } else if (
      combinedInput.endsWith(":") ||
      trimmedRemainder.startsWith(".")
    ) {
      combinedInput += trimmedRemainder;
    } else {
      combinedInput += `/${trimmedRemainder}`;
    }
  }

  if (combinedInput === "" || command !== "show") {
    return combinedInput;
  }

  if (combinedInput.startsWith("//") || combinedInput.includes("//")) {
    return combinedInput;
  }

  if (combinedInput.startsWith("@")) {
    return combinedInput;
  }

  return `//${combinedInput}`;
}

function cloneWorkspaceSummary(
  workspace: WorkspaceSummary | null,
): WorkspaceSummary | null {
  if (workspace === null) {
    return null;
  }

  return JSON.parse(JSON.stringify(workspace)) as WorkspaceSummary;
}

function normalizeSearchText(value: string): string {
  return value.trim().toLowerCase();
}

function historySearchText(entry: CommandHistoryEntry): string {
  return normalizeSearchText(
    `${entry.breadcrumb.join("/")} ${entry.prompt}`.trim(),
  );
}

function areEntriesEquivalent(
  left: CommandHistoryEntry,
  right: CommandHistoryEntry,
): boolean {
  return (
    left.command === right.command &&
    left.prompt === right.prompt &&
    left.breadcrumb.length === right.breadcrumb.length &&
    left.breadcrumb.every((segment, index) => segment === right.breadcrumb[index]) &&
    JSON.stringify(left.workspace) === JSON.stringify(right.workspace)
  );
}

function createHistoryId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  return `history-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function loadCommandHistory(): CommandHistoryEntry[] {
  if (typeof window === "undefined") {
    return [];
  }

  try {
    const raw = window.localStorage.getItem(COMMAND_HISTORY_STORAGE_KEY);
    if (!raw) {
      return [];
    }

    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }

    return parsed.filter((entry): entry is CommandHistoryEntry => {
      return (
        entry &&
        typeof entry === "object" &&
        typeof entry.command === "string" &&
        typeof entry.prompt === "string" &&
        Array.isArray(entry.breadcrumb)
      );
    });
  } catch {
    return [];
  }
}

export function saveCommandHistory(entries: CommandHistoryEntry[]): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(
    COMMAND_HISTORY_STORAGE_KEY,
    JSON.stringify(entries),
  );
}

export function appendCommandHistoryEntry(
  entries: CommandHistoryEntry[],
  snapshot: CommandHistorySnapshot,
  workspace: WorkspaceSummary | null,
): CommandHistoryEntry[] {
  const nextEntry: CommandHistoryEntry = {
    id: createHistoryId(),
    createdAt: new Date().toISOString(),
    command: snapshot.command,
    prompt: snapshot.value,
    breadcrumb: [...snapshot.promotedSegments],
    workspace: cloneWorkspaceSummary(workspace),
  };

  const previousEntry = entries.at(-1);
  const dedupedEntries =
    previousEntry && areEntriesEquivalent(previousEntry, nextEntry)
      ? [...entries.slice(0, -1), nextEntry]
      : [...entries, nextEntry];

  return dedupedEntries.slice(-MAX_COMMAND_HISTORY);
}

export function toHistorySnapshot(
  entry: CommandHistoryEntry,
): CommandHistorySnapshot {
  return {
    command: entry.command,
    value: entry.prompt,
    promotedSegments: [...entry.breadcrumb],
  };
}

export function findCommandHistoryMatches(
  entries: CommandHistoryEntry[],
  query: string,
): CommandHistoryMatch[] {
  const normalizedQuery = normalizeSearchText(query);
  if (normalizedQuery === "") {
    return [];
  }

  const matches: CommandHistoryMatch[] = [];
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (!entry) {
      continue;
    }

    if (historySearchText(entry).includes(normalizedQuery)) {
      matches.push({ entry, index });
    }
  }

  return matches;
}

export function describeCommandHistoryEntry(entry: CommandHistoryEntry): string {
  const commandText = entry.command.trim();
  const inputText = buildCommandInput(
    entry.command,
    entry.breadcrumb,
    entry.prompt,
  );
  return [commandText, inputText]
    .filter(Boolean)
    .join(" ")
    .trim();
}

export function describeCommandHistoryWorkspace(
  entry: CommandHistoryEntry,
): string {
  if (!entry.workspace) {
    return "no workspace";
  }

  const sha =
    typeof entry.workspace.info?.sha === "string"
      ? entry.workspace.info.sha.slice(0, 8)
      : null;
  return sha ?? entry.workspace.workspaceRoot;
}
