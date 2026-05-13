import { useEffect, useState } from "react";
import {
  appendCommandHistoryEntry,
  buildCommandInput,
  describeCommandHistoryEntry,
  describeCommandHistoryWorkspace,
  findCommandHistoryMatches,
  loadCommandHistory,
  saveCommandHistory,
  toHistorySnapshot,
  type CommandHistoryEntry,
  type CommandHistorySnapshot,
} from "../commandHistory.js";
import type {
  CommandOption,
  CommandSubmission,
  LocationCrumb,
  UserSummary,
  WorkspaceSummary,
} from "../types.js";
import { CommandInputEditor } from "./CommandInputEditor.js";
import { InputToolbar } from "./InputToolbar.js";

interface InputBarProps {
  commandOptions: CommandOption[];
  currentCommand: string;
  location: LocationCrumb[];
  topdir: string;
  workspace: WorkspaceSummary | null;
  showLocation: boolean;
  currentUser: UserSummary;
  onCommandChange: (command: string) => void;
  onSubmit: (submission: CommandSubmission) => void;
  disabled?: boolean;
}

interface HistorySearchState {
  query: string;
  draft: CommandHistorySnapshot;
  matches: number[];
  selectedMatch: number;
}

function createCommandSnapshot(
  command: string,
  value: string,
  promotedSegments: string[],
): CommandHistorySnapshot {
  return {
    command,
    value,
    promotedSegments: [...promotedSegments],
  };
}

export function InputBar({
  commandOptions,
  currentCommand,
  location,
  topdir,
  workspace,
  showLocation,
  currentUser,
  onCommandChange,
  onSubmit,
  disabled = false,
}: InputBarProps) {
  const [value, setValue] = useState("");
  const [promotedSegments, setPromotedSegments] = useState<string[]>([]);
  const [historyEntries, setHistoryEntries] = useState<CommandHistoryEntry[]>(
    () => loadCommandHistory(),
  );
  const [historyCursor, setHistoryCursor] = useState<number | null>(null);
  const [historyDraft, setHistoryDraft] =
    useState<CommandHistorySnapshot | null>(null);
  const [historySearch, setHistorySearch] = useState<HistorySearchState | null>(
    null,
  );

  useEffect(() => {
    saveCommandHistory(historyEntries);
  }, [historyEntries]);

  const locationWithDraftSegments =
    promotedSegments.length === 0
      ? location
      : [
          ...location,
          ...promotedSegments.map((segment, index) => ({
            id: `draft-segment-${index}-${segment}`,
            pieces: [
              {
                kind: segment === "..." || segment === "...:"
                  ? ("wildcard" as const)
                  : segment.startsWith("@")
                  ? ("entity" as const)
                  : ("mlody-folder" as const),
                text: segment,
              },
            ],
          })),
        ];

  const historyMatchEntry =
    historySearch && historySearch.matches.length > 0
      ? historyEntries[historySearch.matches[historySearch.selectedMatch] ?? -1] ?? null
      : null;

  function currentSnapshot(): CommandHistorySnapshot {
    return createCommandSnapshot(currentCommand, value, promotedSegments);
  }

  function applySnapshot(snapshot: CommandHistorySnapshot) {
    setValue(snapshot.value);
    setPromotedSegments([...snapshot.promotedSegments]);

    if (snapshot.command !== currentCommand) {
      onCommandChange(snapshot.command);
    }
  }

  function resetHistoryNavigation() {
    setHistoryCursor(null);
    setHistoryDraft(null);
  }

  function closeHistorySearch(options?: {
    restoreDraft?: boolean;
    keepCursor?: boolean;
  }) {
    const restoreDraft = options?.restoreDraft ?? false;
    const keepCursor = options?.keepCursor ?? false;
    const activeSearch = historySearch;

    if (restoreDraft && activeSearch) {
      applySnapshot(activeSearch.draft);
    }

    setHistorySearch(null);

    if (!keepCursor) {
      resetHistoryNavigation();
    }
  }

  function updateHistorySearch(
    query: string,
    draft: CommandHistorySnapshot,
    selectedMatch = 0,
  ): boolean {
    const matches = findCommandHistoryMatches(historyEntries, query);
    const nextSelectedMatch =
      matches.length === 0
        ? 0
        : Math.min(Math.max(selectedMatch, 0), matches.length - 1);
    const nextState: HistorySearchState = {
      query,
      draft,
      matches: matches.map((match) => match.index),
      selectedMatch: nextSelectedMatch,
    };

    setHistorySearch(nextState);

    if (query.trim() === "" || matches.length === 0) {
      setHistoryCursor(null);
      setHistoryDraft(draft);
      applySnapshot(draft);
    } else {
      applySnapshot(toHistorySnapshot(matches[nextSelectedMatch]!.entry));
      setHistoryCursor(matches[nextSelectedMatch]!.index);
      setHistoryDraft(draft);
    }

    return true;
  }

  function handleHistoryPrevious(): boolean {
    if (historySearch) {
      return handleHistorySearchPreviousMatch();
    }

    if (historyEntries.length === 0) {
      return false;
    }

    const draft = historyDraft ?? currentSnapshot();
    const nextCursor =
      historyCursor === null
        ? historyEntries.length - 1
        : Math.max(historyCursor - 1, 0);

    setHistoryDraft(draft);
    setHistoryCursor(nextCursor);
    applySnapshot(toHistorySnapshot(historyEntries[nextCursor]!));
    return true;
  }

  function handleHistoryNext(): boolean {
    if (historySearch) {
      return handleHistorySearchNextMatch();
    }

    if (historyCursor === null) {
      return false;
    }

    const draft = historyDraft ?? currentSnapshot();
    const nextCursor = historyCursor + 1;
    if (nextCursor >= historyEntries.length) {
      applySnapshot(draft);
      resetHistoryNavigation();
      return true;
    }

    setHistoryDraft(draft);
    setHistoryCursor(nextCursor);
    applySnapshot(toHistorySnapshot(historyEntries[nextCursor]!));
    return true;
  }

  function handleHistorySearchRequest(): boolean {
    const draft = historySearch?.draft ?? historyDraft ?? currentSnapshot();

    if (historySearch) {
      return handleHistorySearchPreviousMatch();
    }

    setHistoryDraft(draft);
    setHistoryCursor(null);
    setHistorySearch({
      query: "",
      draft,
      matches: [],
      selectedMatch: 0,
    });
    return true;
  }

  function handleHistorySearchAppend(text: string): boolean {
    const draft = historySearch?.draft ?? historyDraft ?? currentSnapshot();
    const nextQuery = `${historySearch?.query ?? ""}${text}`;
    setHistoryDraft(draft);
    return updateHistorySearch(nextQuery, draft);
  }

  function handleHistorySearchBackspace(): boolean {
    if (!historySearch) {
      return false;
    }

    const nextQuery = historySearch.query.slice(0, -1);
    return updateHistorySearch(nextQuery, historySearch.draft);
  }

  function handleHistorySearchAccept(): boolean {
    if (!historySearch) {
      return false;
    }

    if (historySearch.query.trim() === "" || historySearch.matches.length === 0) {
      closeHistorySearch({
        restoreDraft: historySearch.query.trim() !== "",
      });
      return true;
    }

    setHistoryCursor(historySearch.matches[historySearch.selectedMatch] ?? null);
    setHistoryDraft(historySearch.draft);
    setHistorySearch(null);
    return true;
  }

  function handleHistorySearchCancel(): boolean {
    if (!historySearch) {
      return false;
    }

    closeHistorySearch({ restoreDraft: true });
    return true;
  }

  function handleHistorySearchPreviousMatch(): boolean {
    if (!historySearch) {
      return false;
    }

    if (historySearch.matches.length === 0) {
      return true;
    }

    const nextSelectedMatch =
      (historySearch.selectedMatch + 1) % historySearch.matches.length;
    return updateHistorySearch(
      historySearch.query,
      historySearch.draft,
      nextSelectedMatch,
    );
  }

  function handleHistorySearchNextMatch(): boolean {
    if (!historySearch) {
      return false;
    }

    if (historySearch.matches.length === 0) {
      return true;
    }

    const nextSelectedMatch =
      historySearch.selectedMatch === 0
        ? historySearch.matches.length - 1
        : historySearch.selectedMatch - 1;
    return updateHistorySearch(
      historySearch.query,
      historySearch.draft,
      nextSelectedMatch,
    );
  }

  function handleSubmit(snapshot: {
    value: string;
    promotedSegments: string[];
  }) {
    const commandSnapshot = createCommandSnapshot(
      currentCommand,
      snapshot.value,
      snapshot.promotedSegments,
    );
    const combinedInput = buildCommandInput(
      currentCommand,
      snapshot.promotedSegments,
      snapshot.value,
    );

    if (currentCommand) {
      setHistoryEntries((prev) =>
        appendCommandHistoryEntry(prev, commandSnapshot, workspace),
      );
      setValue("");
      setPromotedSegments([]);
      setHistorySearch(null);
      resetHistoryNavigation();
      onSubmit({ command: currentCommand, input: combinedInput });
    }
  }

  return (
    <div className="CommandShell">
      <InputToolbar
        commandOptions={commandOptions}
        currentCommand={currentCommand}
        location={locationWithDraftSegments}
        topdir={topdir}
        workspace={workspace}
        showLocation={showLocation}
        currentUser={currentUser}
        onCommandChange={onCommandChange}
      />
      <div className="CommandShell-entry">
        <div className="CommandShell-gutter" aria-hidden="true">
          <span className="CommandShell-prompt">&gt;</span>
        </div>
        <CommandInputEditor
          value={value}
          promotedSegments={promotedSegments}
          historySearchActive={historySearch !== null}
          onChange={setValue}
          disabled={disabled}
          placeholder={`Type what to ${currentCommand} and press Enter...`}
          onPromotedSegmentsChange={setPromotedSegments}
          onSubmit={handleSubmit}
          onAutocompleteRequest={() => {}}
          onHistoryPrevious={handleHistoryPrevious}
          onHistoryNext={handleHistoryNext}
          onHistorySearchRequest={handleHistorySearchRequest}
          onHistorySearchBackspace={handleHistorySearchBackspace}
          onHistorySearchAccept={handleHistorySearchAccept}
          onHistorySearchCancel={handleHistorySearchCancel}
          onHistorySearchAppend={handleHistorySearchAppend}
          onHistorySearchPreviousMatch={handleHistorySearchPreviousMatch}
          onHistorySearchNextMatch={handleHistorySearchNextMatch}
        />
        <div className="CommandShell-shortcuts" aria-hidden="true">
          <span className="CommandShell-shortcut">
            <kbd>Enter</kbd>
            run
          </span>
          <span className="CommandShell-shortcut">
            <kbd>Shift</kbd>
            <kbd>Enter</kbd>
            newline
          </span>
          <span className="CommandShell-shortcut">
            <kbd>Tab</kbd>
            autocomplete
          </span>
          <span className="CommandShell-shortcut">
            <kbd>Ctrl</kbd>
            <kbd>P</kbd>
            <kbd>N</kbd>
            <kbd>R</kbd>
            history
          </span>
        </div>
      </div>
      {historySearch ? (
        <div className="CommandShell-historyPanel" role="status" aria-live="polite">
          <span className="CommandShell-historyLabel">history search</span>
          <span className="CommandShell-historyQuery">
            {historySearch.query === "" ? "Type to search breadcrumb and prompt history." : historySearch.query}
          </span>
          <span className="CommandShell-historyResult">
            {historyMatchEntry
              ? `${describeCommandHistoryEntry(historyMatchEntry)} · ${describeCommandHistoryWorkspace(historyMatchEntry)}`
              : historySearch.query === ""
              ? "Ctrl-R older match · Ctrl-N newer · Enter accept · Esc cancel"
              : "No matching history entry"}
          </span>
        </div>
      ) : null}
    </div>
  );
}
