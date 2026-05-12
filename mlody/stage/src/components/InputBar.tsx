import { useEffect, useRef, useState } from "react";
import type {
  CommandOption,
  CommandSubmission,
  LocationCrumb,
  UserSummary,
  WorkspaceSummary,
} from "../types.js";
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
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grow the textarea to match its content, capped by CSS max-height
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [value]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const input = value.trim();

      if (currentCommand) {
        setValue("");
        onSubmit({ command: currentCommand, input });
      }
    }
  };

  return (
    <div className="CommandShell">
      <InputToolbar
        commandOptions={commandOptions}
        currentCommand={currentCommand}
        location={location}
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
        <textarea
          ref={textareaRef}
          className="CommandShell-textarea"
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          aria-label="Command input"
          placeholder={`Type what to ${currentCommand} and press Enter...`}
          spellCheck={false}
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
        </div>
      </div>
    </div>
  );
}
