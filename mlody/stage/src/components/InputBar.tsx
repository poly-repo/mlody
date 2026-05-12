import { useState } from "react";
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

  const locationWithDraftSegments =
    promotedSegments.length === 0
      ? location
      : [
          ...location,
          ...promotedSegments.map((segment, index) => ({
            id: `draft-segment-${index}-${segment}`,
            pieces: [
              {
                kind: segment.startsWith("@")
                  ? ("entity" as const)
                  : ("mlody-folder" as const),
                text: segment,
              },
            ],
          })),
        ];

  function buildExecutableInput(segments: string[], remainder: string): string {
    const combinedInput = [...segments, remainder]
      .filter((segment) => segment.trim() !== "")
      .join("/")
      .trim();

    if (combinedInput === "" || currentCommand !== "show") {
      return combinedInput;
    }

    if (combinedInput.includes("//")) {
      return combinedInput;
    }

    const [firstSegment, ...restSegments] = combinedInput.split("/");
    if (!firstSegment) {
      return combinedInput;
    }

    if (!firstSegment.startsWith("@")) {
      return `//${combinedInput}`;
    }

    if (restSegments.length === 0) {
      return firstSegment;
    }

    return `${firstSegment}//${restSegments.join("/")}`;
  }

  function popPromotedSegment(): boolean {
    let removed = false;
    setPromotedSegments((prev) => {
      if (prev.length === 0) {
        return prev;
      }

      removed = true;
      return prev.slice(0, -1);
    });
    return removed;
  }

  function handleSubmitRequest() {
    const combinedInput = buildExecutableInput(promotedSegments, value);

    if (currentCommand) {
      setValue("");
      setPromotedSegments([]);
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
          onChange={setValue}
          disabled={disabled}
          placeholder={`Type what to ${currentCommand} and press Enter...`}
          onSubmit={handleSubmitRequest}
          onAutocompleteRequest={() => {}}
          onPromoteSegments={(segments) => {
            if (segments.length === 0) {
              return;
            }
            setPromotedSegments((prev) => [...prev, ...segments]);
          }}
          onPopLocationSegment={popPromotedSegment}
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
        </div>
      </div>
    </div>
  );
}
