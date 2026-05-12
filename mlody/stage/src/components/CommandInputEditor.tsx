import CodeMirror from "@uiw/react-codemirror";
import {
  cursorCharLeft,
  cursorCharRight,
  cursorGroupLeft,
  cursorGroupRight,
  cursorLineEnd,
  cursorLineStart,
  defaultKeymap,
  deleteCharBackward,
  deleteCharForward,
  deleteGroupBackward,
  deleteGroupForward,
  deleteToLineEnd,
  history,
  historyKeymap,
} from "@codemirror/commands";
import { Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { useEffect, useRef } from "react";

type CommandInputImplementation = "codemirror" | "textarea";

// Keep the editor implementation swappable while we learn whether CodeMirror
// is worth the added complexity for the stage command shell.
const COMMAND_INPUT_IMPLEMENTATION: CommandInputImplementation = "codemirror";

interface CommandInputEditorProps {
  value: string;
  disabled?: boolean;
  placeholder: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onAutocompleteRequest: () => void;
  onPromoteSegments: (segments: string[]) => void;
  onPopLocationSegment: () => boolean;
}

function canPromoteLocationSegment(value: string): boolean {
  return value === "..." || /^@?[A-Za-z0-9_-]+$/.test(value);
}

function extractNextPromotableSegment(
  value: string,
): { promotedSegments: string[]; remainder: string } | null {
  if (value === "...") {
    return {
      promotedSegments: ["..."],
      remainder: "",
    };
  }

  if (value.startsWith("...//")) {
    return {
      promotedSegments: ["..."],
      remainder: value.slice(5),
    };
  }

  if (value.startsWith(".../")) {
    return {
      promotedSegments: ["..."],
      remainder: value.slice(4),
    };
  }

  if (value.startsWith("...:")) {
    return {
      promotedSegments: ["...:"],
      remainder: value.slice(4),
    };
  }

  const match = value.match(/^(@?[A-Za-z0-9_-]+)(.*)$/);
  if (!match) {
    return null;
  }

  const promotedSegment = match[1];
  const rest = match[2] ?? "";

  if (rest.startsWith("//")) {
    return {
      promotedSegments: [promotedSegment],
      remainder: rest.slice(2),
    };
  }

  if (rest.startsWith("/")) {
    return {
      promotedSegments: [promotedSegment],
      remainder: rest.slice(1),
    };
  }

  if (rest.startsWith(":")) {
    return {
      promotedSegments: [`${promotedSegment}:`],
      remainder: rest.slice(1),
    };
  }

  if (rest.startsWith(".")) {
    return {
      promotedSegments: [promotedSegment],
      remainder: rest,
    };
  }

  return null;
}

function extractPromotableSegments(value: string): {
  promotedSegments: string[];
  remainder: string;
} {
  const promotedSegments: string[] = [];
  let remainder = value;

  while (true) {
    const splitResult = extractNextPromotableSegment(remainder);
    if (!splitResult) {
      break;
    }

    promotedSegments.push(...splitResult.promotedSegments);
    remainder = splitResult.remainder;
  }

  return { promotedSegments, remainder };
}

function insertText(view: EditorView, text: string): void {
  view.dispatch(view.state.replaceSelection(text));
}

function replaceText(view: EditorView, text: string): void {
  view.dispatch({
    changes: {
      from: 0,
      to: view.state.doc.length,
      insert: text,
    },
  });
}

function isCursorAtEnd(view: EditorView): boolean {
  const selection = view.state.selection.main;
  return selection.empty && selection.head === view.state.doc.length;
}

function deletePreviousWordInView(view: EditorView): boolean {
  const selection = view.state.selection.main;
  if (!selection.empty || selection.head === 0) {
    return false;
  }

  const text = view.state.doc.toString();
  let from = selection.head;

  while (from > 0 && /\s/.test(text[from - 1] ?? "")) {
    from -= 1;
  }
  while (from > 0 && !/\s/.test(text[from - 1] ?? "")) {
    from -= 1;
  }

  if (from === selection.head) {
    return false;
  }

  view.dispatch({
    changes: {
      from,
      to: selection.head,
      insert: "",
    },
  });
  return true;
}

function runAndReport(
  view: EditorView,
  command: ((view: EditorView) => boolean) | undefined,
): boolean {
  return command?.(view) ?? false;
}

function shouldAttemptBulkPromotion(
  previousValue: string,
  nextValue: string,
): boolean {
  return Math.abs(nextValue.length - previousValue.length) > 1;
}

function popLocationSegment(
  view: EditorView,
  onPopLocationSegment: () => boolean,
): boolean {
  if (view.state.doc.length > 0) {
    return false;
  }

  return onPopLocationSegment();
}

function deletePreviousWordFromText(value: string, cursor: number): string {
  if (cursor <= 0) {
    return value;
  }

  let from = cursor;
  while (from > 0 && /\s/.test(value[from - 1] ?? "")) {
    from -= 1;
  }
  while (from > 0 && !/\s/.test(value[from - 1] ?? "")) {
    from -= 1;
  }

  return value.slice(0, from) + value.slice(cursor);
}

function CodeMirrorCommandInput({
  value,
  disabled = false,
  placeholder,
  onChange,
  onSubmit,
  onAutocompleteRequest,
  onPromoteSegments,
  onPopLocationSegment,
}: CommandInputEditorProps) {
  function handleChange(nextValue: string) {
    if (!shouldAttemptBulkPromotion(value, nextValue)) {
      onChange(nextValue);
      return;
    }

    const { promotedSegments, remainder } = extractPromotableSegments(nextValue);
    if (promotedSegments.length > 0) {
      onPromoteSegments(promotedSegments);
      onChange(remainder);
      return;
    }

    onChange(nextValue);
  }

  return (
    <CodeMirror
      className="CommandInputEditor"
      value={value}
      basicSetup={false}
      editable={!disabled}
      readOnly={disabled}
      placeholder={placeholder}
      onChange={handleChange}
      extensions={[
        EditorView.lineWrapping,
        EditorView.contentAttributes.of({
          "aria-label": "Command input",
          spellcheck: "false",
        }),
        Prec.high(
          keymap.of([
            {
              key: "Enter",
              run() {
                onSubmit();
                return true;
              },
            },
            {
              key: "Tab",
              run() {
                onAutocompleteRequest();
                return true;
              },
            },
            {
              key: "Ctrl-a",
              run(view) {
                return runAndReport(view, cursorLineStart);
              },
            },
            {
              key: "Ctrl-e",
              run(view) {
                return runAndReport(view, cursorLineEnd);
              },
            },
            {
              key: "Ctrl-b",
              run(view) {
                return runAndReport(view, cursorCharLeft);
              },
            },
            {
              key: "Ctrl-f",
              run(view) {
                return runAndReport(view, cursorCharRight);
              },
            },
            {
              key: "Alt-b",
              run(view) {
                return runAndReport(view, cursorGroupLeft);
              },
            },
            {
              key: "Alt-f",
              run(view) {
                return runAndReport(view, cursorGroupRight);
              },
            },
            {
              key: "Ctrl-d",
              run(view) {
                return runAndReport(view, deleteCharForward);
              },
            },
            {
              key: "Backspace",
              run(view) {
                return (
                  popLocationSegment(view, onPopLocationSegment) ||
                  deletePreviousWordInView(view)
                );
              },
            },
            {
              key: "Ctrl-h",
              run(view) {
                return (
                  popLocationSegment(view, onPopLocationSegment) ||
                  runAndReport(view, deleteCharBackward)
                );
              },
            },
            {
              key: "Ctrl-k",
              run(view) {
                return runAndReport(view, deleteToLineEnd);
              },
            },
            {
              key: "Ctrl-w",
              run(view) {
                return runAndReport(view, deleteGroupBackward);
              },
            },
            {
              key: "Alt-d",
              run(view) {
                return runAndReport(view, deleteGroupForward);
              },
            },
            {
              key: "/",
              run(view) {
                if (!isCursorAtEnd(view)) {
                  insertText(view, "/");
                  return true;
                }

                const segment = view.state.doc.toString();
                if (segment === "") {
                  return true;
                }
                if (!canPromoteLocationSegment(segment)) {
                  insertText(view, "/");
                  return true;
                }

                onPromoteSegments([segment]);
                view.dispatch({
                  changes: {
                    from: 0,
                    to: view.state.doc.length,
                    insert: "",
                  },
                });
                return true;
              },
            },
            {
              key: ":",
              run(view) {
                if (!isCursorAtEnd(view)) {
                  insertText(view, ":");
                  return true;
                }

                const segment = view.state.doc.toString();
                if (!segment || !canPromoteLocationSegment(segment)) {
                  insertText(view, ":");
                  return true;
                }

                onPromoteSegments([`${segment}:`]);
                replaceText(view, "");
                return true;
              },
            },
            {
              key: ".",
              run(view) {
                if (!isCursorAtEnd(view)) {
                  insertText(view, ".");
                  return true;
                }

                const segment = view.state.doc.toString();
                if (segment === "..") {
                  onPromoteSegments(["..."]);
                  replaceText(view, "");
                  return true;
                }

                if (!segment || !canPromoteLocationSegment(segment) || segment === "...") {
                  insertText(view, ".");
                  return true;
                }

                onPromoteSegments([segment]);
                replaceText(view, ".");
                return true;
              },
            },
          ]),
        ),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
      ]}
    />
  );
}

function TextareaCommandInput({
  value,
  disabled = false,
  placeholder,
  onChange,
  onSubmit,
  onAutocompleteRequest,
  onPromoteSegments,
  onPopLocationSegment,
}: CommandInputEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [value]);

  function handleChange(nextValue: string) {
    if (!shouldAttemptBulkPromotion(value, nextValue)) {
      onChange(nextValue);
      return;
    }

    const { promotedSegments, remainder } = extractPromotableSegments(nextValue);
    if (promotedSegments.length > 0) {
      onPromoteSegments(promotedSegments);
      onChange(remainder);
      return;
    }

    onChange(nextValue);
  }

  return (
    <textarea
      ref={textareaRef}
      className="CommandShell-textarea"
      rows={1}
      value={value}
      onChange={(event) => handleChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          onSubmit();
          return;
        }

        if (event.key === "Tab") {
          event.preventDefault();
          onAutocompleteRequest();
          return;
        }

        if (
          event.key === "/" &&
          event.currentTarget.selectionStart === value.length &&
          event.currentTarget.selectionEnd === value.length &&
          value === ""
        ) {
          event.preventDefault();
          return;
        }

        if (
          event.key === "/" &&
          event.currentTarget.selectionStart === value.length &&
          event.currentTarget.selectionEnd === value.length &&
          canPromoteLocationSegment(value)
        ) {
          event.preventDefault();
          onPromoteSegments([value]);
          onChange("");
          return;
        }

        if (
          event.key === ":" &&
          event.currentTarget.selectionStart === value.length &&
          event.currentTarget.selectionEnd === value.length &&
          value !== "" &&
          canPromoteLocationSegment(value)
        ) {
          event.preventDefault();
          onPromoteSegments([`${value}:`]);
          onChange("");
          return;
        }

        if (
          event.key === "." &&
          event.currentTarget.selectionStart === value.length &&
          event.currentTarget.selectionEnd === value.length
        ) {
          if (value === "..") {
            event.preventDefault();
            onPromoteSegments(["..."]);
            onChange("");
            return;
          }

          if (value !== "" && canPromoteLocationSegment(value) && value !== "...") {
            event.preventDefault();
            onPromoteSegments([value]);
            onChange(".");
            return;
          }
        }

        if (
          event.key === "Backspace" &&
          value === ""
        ) {
          if (onPopLocationSegment()) {
            event.preventDefault();
          }
          return;
        }

        if (
          event.key === "Backspace" &&
          value !== "" &&
          event.currentTarget.selectionStart === event.currentTarget.selectionEnd
        ) {
          event.preventDefault();
          onChange(
            deletePreviousWordFromText(value, event.currentTarget.selectionStart),
          );
          return;
        }

        if (event.ctrlKey && event.key === "h" && value === "") {
          if (onPopLocationSegment()) {
            event.preventDefault();
          }
        }
      }}
      disabled={disabled}
      aria-label="Command input"
      placeholder={placeholder}
      spellCheck={false}
    />
  );
}

export function CommandInputEditor(props: CommandInputEditorProps) {
  if (COMMAND_INPUT_IMPLEMENTATION === "textarea") {
    return <TextareaCommandInput {...props} />;
  }

  return <CodeMirrorCommandInput {...props} />;
}
