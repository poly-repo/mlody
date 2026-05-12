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

  function buildExecutableInput(segments: string[], remainder: string): string {
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

    if (combinedInput === "" || currentCommand !== "show") {
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

  function handleSubmit(snapshot: {
    value: string;
    promotedSegments: string[];
  }) {
    const combinedInput = buildExecutableInput(
      snapshot.promotedSegments,
      snapshot.value,
    );

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
          promotedSegments={promotedSegments}
          onChange={setValue}
          disabled={disabled}
          placeholder={`Type what to ${currentCommand} and press Enter...`}
          onPromotedSegmentsChange={setPromotedSegments}
          onSubmit={handleSubmit}
          onAutocompleteRequest={() => {}}
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
