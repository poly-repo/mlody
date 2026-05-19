import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import {
  acceptCompletion,
  autocompletion,
  type CompletionContext,
  type CompletionResult,
  startCompletion,
} from "@codemirror/autocomplete";
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
  invertedEffects,
  historyKeymap,
  isolateHistory,
} from "@codemirror/commands";
import { Prec, StateEffect, StateField, Transaction } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { useEffect, useRef } from "react";
import {
  analyzeStagePromptAutocomplete,
  type StagePromptAutocompleteOption,
} from "../promptCommands.js";
import { fetchStageAutocomplete } from "../serverApi.js";
import type {
  StageAutocompleteCompletion,
  StageAutocompleteCompletionKind,
} from "../types.js";

type CommandInputImplementation = "codemirror" | "textarea";

// Keep the editor implementation swappable while we learn whether CodeMirror
// is worth the added complexity for the stage command shell.
const COMMAND_INPUT_IMPLEMENTATION: CommandInputImplementation = "codemirror";

interface CommandInputEditorProps {
  value: string;
  promotedSegments: string[];
  cursorToEndToken: number;
  workspaceRoot: string | null;
  historySearchActive: boolean;
  disabled?: boolean;
  placeholder: string;
  onChange: (value: string) => void;
  onPromotedSegmentsChange: (segments: string[]) => void;
  onSubmit: (snapshot: CommandInputSnapshot) => void;
  onAutocompleteRequest: () => void;
  onHistoryPrevious: () => boolean;
  onHistoryNext: () => boolean;
  onHistorySearchRequest: () => boolean;
  onHistorySearchBackspace: () => boolean;
  onHistorySearchAccept: () => boolean;
  onHistorySearchCancel: () => boolean;
  onHistorySearchAppend: (text: string) => boolean;
  onHistorySearchPreviousMatch: () => boolean;
  onHistorySearchNextMatch: () => boolean;
}

interface CommandInputSnapshot {
  value: string;
  promotedSegments: string[];
}

interface AutocompleteAnalysis {
  from: number;
  source: "stage" | "prompt-command";
  kind?: "root" | "package" | "target" | "field";
  options?: StagePromptAutocompleteOption[];
  validFor?: RegExp;
}

const AUTOCOMPLETE_VALID_FOR = /^[A-Za-z0-9_-]*$/;

const setPromotedSegmentsEffect = StateEffect.define<readonly string[]>();

const promotedSegmentsField = StateField.define<readonly string[]>({
  create: () => [],
  update(value, transaction) {
    for (const effect of transaction.effects) {
      if (effect.is(setPromotedSegmentsEffect)) {
        return [...effect.value];
      }
    }

    return value;
  },
});

const promotedSegmentsHistory = invertedEffects.of((transaction) => {
  for (const effect of transaction.effects) {
    if (effect.is(setPromotedSegmentsEffect)) {
      return [
        setPromotedSegmentsEffect.of(
          transaction.startState.field(promotedSegmentsField),
        ),
      ];
    }
  }

  return [];
});

function canPromoteLocationSegment(value: string): boolean {
  return value === "..." || /^@?[A-Za-z0-9_-]+$/.test(value);
}

function isRootLocationSegment(value: string): boolean {
  return /^@[A-Za-z0-9_-]+$/.test(value);
}

function shouldStartAbsoluteScope(
  promotedSegments: readonly string[],
): boolean {
  const lastSegment = promotedSegments[promotedSegments.length - 1] ?? "";
  return promotedSegments.length === 0 || isRootLocationSegment(lastSegment);
}

function analyzeAutocompleteRequest(
  prompt: string,
  promotedSegments: readonly string[],
): AutocompleteAnalysis | null {
  if (promotedSegments.length === 0) {
    const promptCommandAutocomplete = analyzeStagePromptAutocomplete(prompt);
    if (promptCommandAutocomplete !== null) {
      return {
        from: promptCommandAutocomplete.from,
        source: "prompt-command",
        options: promptCommandAutocomplete.options,
        validFor: promptCommandAutocomplete.validFor,
      };
    }
  }

  if (prompt.includes("\n") || prompt.includes("\r")) {
    return null;
  }

  if (
    prompt.includes("'") ||
    prompt.includes("[") ||
    prompt.includes("]") ||
    prompt.includes("|")
  ) {
    return null;
  }

  if (prompt.startsWith("@")) {
    return /^@[A-Za-z0-9_-]*$/.test(prompt)
      ? {
          from: 1,
          source: "stage",
          kind: "root",
        }
      : null;
  }

  if (prompt.startsWith(".")) {
    const lastSegment = promotedSegments[promotedSegments.length - 1];
    return prompt.startsWith(".") &&
        /^\.?[A-Za-z0-9_-]*$/.test(prompt) &&
        typeof lastSegment === "string" &&
        promotedSegments.some((segment) => segment.endsWith(":"))
      ? {
          from: 1,
          source: "stage",
          kind: "field",
        }
      : null;
  }

  if (prompt.includes("/") || prompt.includes(":") || prompt.includes(".")) {
    return null;
  }

  if (!AUTOCOMPLETE_VALID_FOR.test(prompt)) {
    return null;
  }

  const lastSegment = promotedSegments[promotedSegments.length - 1] ?? null;
  if (typeof lastSegment === "string" && lastSegment.endsWith(":")) {
    return {
      from: 0,
      source: "stage",
      kind: "target",
    };
  }

  if (promotedSegments.some((segment) => segment.endsWith(":"))) {
    return null;
  }

  if (promotedSegments.includes("//")) {
    return {
      from: 0,
      source: "stage",
      kind: "package",
    };
  }

  return null;
}

function applyPromptAutocompleteCompletion(
  view: EditorView,
  from: number,
  to: number,
  completion: StagePromptAutocompleteOption,
): void {
  const suffix = completion.applySuffix ?? "";
  const insertedText = `${completion.label}${suffix}`;
  view.dispatch({
    changes: {
      from,
      to,
      insert: insertedText,
    },
    selection: {
      anchor: from + insertedText.length,
    },
  });

  if (completion.triggerCompletionAfterApply) {
    queueMicrotask(() => {
      startCompletion(view);
    });
  }
}

function autocompleteSuffixForKind(
  kind: StageAutocompleteCompletionKind,
): string {
  switch (kind) {
    case "root":
      return "//";
    case "folder":
      return "/";
    case "source_file":
      return ":";
    default:
      return "";
  }
}

function applyStageAutocompleteCompletion(
  view: EditorView,
  from: number,
  to: number,
  completion: StageAutocompleteCompletion,
): void {
  const suffix = autocompleteSuffixForKind(completion.kind);
  view.dispatch({
    changes: {
      from,
      to,
      insert: `${completion.label}${suffix}`,
    },
  });

  if (suffix !== "") {
    queueMicrotask(() => {
      startCompletion(view);
    });
  }
}

function extractNextPromotableSegment(
  value: string,
): { promotedSegments: string[]; remainder: string } | null {
  if (value === "//") {
    return {
      promotedSegments: ["//"],
      remainder: "",
    };
  }

  if (value.startsWith("//")) {
    return {
      promotedSegments: ["//"],
      remainder: value.slice(2),
    };
  }

  if (value === "...") {
    return {
      promotedSegments: ["..."],
      remainder: "",
    };
  }

  if (value.startsWith("...//")) {
    return {
      promotedSegments: ["...", "//"],
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
      promotedSegments: [promotedSegment, "//"],
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

function isCursorAtEnd(view: EditorView): boolean {
  const selection = view.state.selection.main;
  return selection.empty && selection.head === view.state.doc.length;
}

function findPreviousWordStart(value: string, cursor: number): number {
  let from = cursor;
  while (from > 0 && /\s/.test(value[from - 1] ?? "")) {
    from -= 1;
  }
  while (from > 0 && !/\s/.test(value[from - 1] ?? "")) {
    from -= 1;
  }
  return from;
}

function deletePreviousWordInView(view: EditorView): boolean {
  const selection = view.state.selection.main;
  if (!selection.empty || selection.head === 0) {
    return false;
  }

  const text = view.state.doc.toString();
  const from = findPreviousWordStart(text, selection.head);

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

function areSegmentsEqual(
  left: readonly string[],
  right: readonly string[],
): boolean {
  if (left.length !== right.length) {
    return false;
  }

  return left.every((segment, index) => segment === right[index]);
}

function getPromotedSegments(view: EditorView): readonly string[] {
  return view.state.field(promotedSegmentsField);
}

function dispatchPromotedSegmentsUpdate(
  view: EditorView,
  nextSegments: readonly string[],
  options: {
    insert?: string;
    addToHistory?: boolean;
    selectionAnchor?: number;
  } = {},
): void {
  const addToHistory = options.addToHistory ?? true;
  view.dispatch({
    ...(options.insert !== undefined
      ? {
          changes: {
            from: 0,
            to: view.state.doc.length,
            insert: options.insert,
          },
        }
      : {}),
    ...(options.selectionAnchor !== undefined
      ? {
          selection: {
            anchor: options.selectionAnchor,
          },
        }
      : {}),
    effects: setPromotedSegmentsEffect.of(nextSegments),
    annotations: addToHistory
      ? [Transaction.addToHistory.of(true), isolateHistory.of("full")]
      : [Transaction.addToHistory.of(false)],
  });
}

function popLastPromotedSegmentChar(
  segments: readonly string[],
): { nextSegments: readonly string[]; insert: string } | null {
  if (segments.length === 0) {
    return null;
  }

  const lastSegment = segments[segments.length - 1] ?? "";
  return {
    nextSegments: segments.slice(0, -1),
    insert: lastSegment.slice(0, -1),
  };
}

function popLocationSegment(view: EditorView): boolean {
  if (view.state.doc.length > 0) {
    return false;
  }

  const segments = getPromotedSegments(view);
  if (segments.length === 0) {
    return false;
  }

  dispatchPromotedSegmentsUpdate(view, segments.slice(0, -1));
  return true;
}

function demoteLocationSegmentAfterSingleDelete(view: EditorView): boolean {
  if (view.state.doc.length > 0) {
    return false;
  }

  const demotedSegment = popLastPromotedSegmentChar(getPromotedSegments(view));
  if (!demotedSegment) {
    return false;
  }

  dispatchPromotedSegmentsUpdate(view, demotedSegment.nextSegments, {
    insert: demotedSegment.insert,
    selectionAnchor: demotedSegment.insert.length,
  });
  return true;
}

function deletePreviousWordFromText(value: string, cursor: number): string {
  if (cursor <= 0) {
    return value;
  }

  const from = findPreviousWordStart(value, cursor);

  return value.slice(0, from) + value.slice(cursor);
}

function CodeMirrorCommandInput({
  value,
  promotedSegments,
  cursorToEndToken,
  workspaceRoot,
  historySearchActive,
  disabled = false,
  placeholder,
  onChange,
  onPromotedSegmentsChange,
  onSubmit,
  onAutocompleteRequest,
  onHistoryPrevious,
  onHistoryNext,
  onHistorySearchRequest,
  onHistorySearchBackspace,
  onHistorySearchAccept,
  onHistorySearchCancel,
  onHistorySearchAppend,
  onHistorySearchPreviousMatch,
  onHistorySearchNextMatch,
}: CommandInputEditorProps) {
  const editorRef = useRef<ReactCodeMirrorRef>(null);
  const autocompleteRequestKeyRef = useRef<string | null>(null);
  const autocompleteRequestSequenceRef = useRef(0);
  const cursorToEndTokenRef = useRef(cursorToEndToken);
  const escapePrefixActiveRef = useRef(false);

  useEffect(() => {
    if (!disabled) {
      editorRef.current?.view?.focus();
    }
  }, [disabled]);

  useEffect(() => {
    const view = editorRef.current?.view;
    if (!view) {
      return;
    }

    const currentValue = view.state.doc.toString();
    const shouldMoveCursorToEnd =
      cursorToEndTokenRef.current !== cursorToEndToken;
    cursorToEndTokenRef.current = cursorToEndToken;

    if (currentValue !== value || shouldMoveCursorToEnd) {
      view.dispatch({
        ...(currentValue !== value
          ? {
              changes: {
                from: 0,
                to: view.state.doc.length,
                insert: value,
              },
            }
          : {}),
        ...(shouldMoveCursorToEnd
          ? {
              selection: {
                anchor: value.length,
              },
            }
          : {}),
        annotations: [Transaction.addToHistory.of(false)],
      });

      if (shouldMoveCursorToEnd && !disabled) {
        view.focus();
      }
    }

    const currentSegments = getPromotedSegments(view);
    if (!areSegmentsEqual(currentSegments, promotedSegments)) {
      dispatchPromotedSegmentsUpdate(view, promotedSegments, {
        addToHistory: false,
      });
    }
  }, [cursorToEndToken, disabled, promotedSegments, value]);

  function deletePreviousWholeThing(view: EditorView): boolean {
    if (!view.state.selection.main.empty) {
      return runAndReport(view, deleteCharBackward);
    }

    return popLocationSegment(view) || deletePreviousWordInView(view);
  }

  function deletePreviousCharOrDemoteSegment(view: EditorView): boolean {
    if (runAndReport(view, deleteCharBackward)) {
      return true;
    }

    return demoteLocationSegmentAfterSingleDelete(view);
  }

  const completionSource = async (
    context: CompletionContext,
  ): Promise<CompletionResult | null> => {
    if (context.pos !== context.state.doc.length) {
      return null;
    }

    const prompt = context.state.doc.toString();
    const breadcrumb = [...context.state.field(promotedSegmentsField)];
    const analysis = analyzeAutocompleteRequest(prompt, breadcrumb);
    if (analysis === null) {
      return null;
    }

    if (analysis.source === "prompt-command") {
      return {
        from: analysis.from,
        options: (analysis.options ?? []).map((completion) => ({
          label: completion.label,
          detail: completion.detail,
          apply(view, _completion, from, to) {
            applyPromptAutocompleteCompletion(view, from, to, completion);
          },
        })),
        validFor: analysis.validFor ?? AUTOCOMPLETE_VALID_FOR,
      };
    }

    const requestKey = JSON.stringify({
      workspaceRoot,
      breadcrumb,
      prompt,
      cursorContext: analysis.kind,
    });
    autocompleteRequestSequenceRef.current += 1;
    const requestSequence = autocompleteRequestSequenceRef.current;
    autocompleteRequestKeyRef.current = requestKey;

    let response: Awaited<ReturnType<typeof fetchStageAutocomplete>>;
    try {
      response = await fetchStageAutocomplete(
        workspaceRoot,
        breadcrumb,
        prompt,
      );
    } catch {
      return null;
    }
    if (
      autocompleteRequestSequenceRef.current !== requestSequence ||
      autocompleteRequestKeyRef.current !== requestKey
    ) {
      return null;
    }

    return {
      from: analysis.from,
      options: response.completions.map((completion) => ({
        label: completion.label,
        apply(view, _completion, from, to) {
          applyStageAutocompleteCompletion(view, from, to, completion);
        },
      })),
      validFor: AUTOCOMPLETE_VALID_FOR,
    };
  };

  return (
    <CodeMirror
      ref={editorRef}
      className="CommandInputEditor"
      value={value}
      autoFocus={!disabled}
      basicSetup={false}
      editable={!disabled}
      readOnly={disabled}
      placeholder={placeholder}
      onCreateEditor={(view) => {
        if (!disabled) {
          view.focus();
        }
      }}
      extensions={[
        EditorView.lineWrapping,
        EditorView.contentAttributes.of({
          "aria-label": "Command input",
          spellcheck: "false",
        }),
        EditorView.domEventHandlers({
          keydown(event) {
            const lowerKey = event.key.toLowerCase();
            if (
              escapePrefixActiveRef.current &&
              event.key !== "Escape" &&
              event.key !== "Backspace" &&
              !(event.ctrlKey && lowerKey === "h")
            ) {
              escapePrefixActiveRef.current = false;
            }

            if (!historySearchActive) {
              return false;
            }

            if (
              event.key.length === 1 &&
              !event.ctrlKey &&
              !event.metaKey &&
              !event.altKey
            ) {
              event.preventDefault();
              return onHistorySearchAppend(event.key);
            }

            return false;
          },
        }),
        promotedSegmentsField,
        promotedSegmentsHistory,
        autocompletion({
          override: [completionSource],
        }),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            const previousValue = update.startState.doc.toString();
            const nextValue = update.state.doc.toString();

            if (shouldAttemptBulkPromotion(previousValue, nextValue)) {
              const splitResult = extractPromotableSegments(nextValue);
              if (splitResult.promotedSegments.length > 0) {
                dispatchPromotedSegmentsUpdate(
                  update.view,
                  [
                    ...getPromotedSegments(update.view),
                    ...splitResult.promotedSegments,
                  ],
                  { insert: splitResult.remainder },
                );
                return;
              }
            }

            onChange(nextValue);
          }

          const previousSegments = update.startState.field(promotedSegmentsField);
          const nextSegments = update.state.field(promotedSegmentsField);
          if (!areSegmentsEqual(previousSegments, nextSegments)) {
            onPromotedSegmentsChange([...nextSegments]);
          }
        }),
        Prec.high(
          keymap.of([
            {
              key: "Enter",
              run(view) {
                if (historySearchActive) {
                  return onHistorySearchAccept();
                }

                onSubmit({
                  value: view.state.doc.toString(),
                  promotedSegments: [...getPromotedSegments(view)],
                });
                return true;
              },
            },
            {
              key: "Tab",
              run(view) {
                if (historySearchActive) {
                  return true;
                }

                onAutocompleteRequest();
                if (acceptCompletion(view)) {
                  return true;
                }

                startCompletion(view);
                return true;
              },
            },
            {
              key: ",",
              run(view) {
                if (!isCursorAtEnd(view)) {
                  insertText(view, ",");
                  return true;
                }

                if (
                  getPromotedSegments(view).length !== 0 ||
                  view.state.doc.length !== 0
                ) {
                  insertText(view, ",");
                  return true;
                }

                insertText(view, ",");
                queueMicrotask(() => {
                  startCompletion(view);
                });
                return true;
              },
            },
            {
              key: "Space",
              run(view) {
                if (!isCursorAtEnd(view)) {
                  insertText(view, " ");
                  return true;
                }

                if (
                  getPromotedSegments(view).length === 0 &&
                  /^,[^\s]+$/.test(view.state.doc.toString())
                ) {
                  insertText(view, " ");
                  queueMicrotask(() => {
                    startCompletion(view);
                  });
                  return true;
                }

                insertText(view, " ");
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
              key: "Ctrl-p",
              run() {
                if (historySearchActive) {
                  return onHistorySearchPreviousMatch();
                }

                return onHistoryPrevious();
              },
            },
            {
              key: "ArrowUp",
              run() {
                if (historySearchActive) {
                  return onHistorySearchPreviousMatch();
                }

                return onHistoryPrevious();
              },
            },
            {
              key: "Ctrl-n",
              run() {
                if (historySearchActive) {
                  return onHistorySearchNextMatch();
                }

                return onHistoryNext();
              },
            },
            {
              key: "ArrowDown",
              run() {
                if (historySearchActive) {
                  return onHistorySearchNextMatch();
                }

                return onHistoryNext();
              },
            },
            {
              key: "Ctrl-r",
              run() {
                if (historySearchActive) {
                  return onHistorySearchPreviousMatch();
                }

                return onHistorySearchRequest();
              },
            },
            {
              key: "Escape",
              run() {
                if (historySearchActive) {
                  return onHistorySearchCancel();
                }

                escapePrefixActiveRef.current = true;
                return true;
              },
            },
            {
              key: "Ctrl-g",
              run() {
                return historySearchActive ? onHistorySearchCancel() : false;
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
                if (historySearchActive) {
                  return onHistorySearchBackspace();
                }

                const deleteWholeThing = escapePrefixActiveRef.current;
                escapePrefixActiveRef.current = false;
                return deleteWholeThing
                  ? deletePreviousWholeThing(view)
                  : deletePreviousCharOrDemoteSegment(view);
              },
            },
            {
              key: "Alt-Backspace",
              run(view) {
                escapePrefixActiveRef.current = false;
                return deletePreviousWholeThing(view);
              },
            },
            {
              key: "Ctrl-Backspace",
              run(view) {
                escapePrefixActiveRef.current = false;
                return deletePreviousWholeThing(view);
              },
            },
            {
              key: "Ctrl-h",
              run(view) {
                if (historySearchActive) {
                  return onHistorySearchBackspace();
                }

                const deleteWholeThing = escapePrefixActiveRef.current;
                escapePrefixActiveRef.current = false;
                return deleteWholeThing
                  ? deletePreviousWholeThing(view)
                  : deletePreviousCharOrDemoteSegment(view);
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

                const promotedSegments = getPromotedSegments(view);
                const segment = view.state.doc.toString();
                if (segment === "/") {
                  dispatchPromotedSegmentsUpdate(
                    view,
                    [...promotedSegments, "//"],
                    { insert: "" },
                  );
                  startCompletion(view);
                  return true;
                }

                if (segment === "") {
                  if (shouldStartAbsoluteScope(promotedSegments)) {
                    insertText(view, "/");
                    return true;
                  }
                  startCompletion(view);
                  return true;
                }

                if (isRootLocationSegment(segment)) {
                  dispatchPromotedSegmentsUpdate(
                    view,
                    [...promotedSegments, segment],
                    { insert: "/" },
                  );
                  return true;
                }

                if (!canPromoteLocationSegment(segment)) {
                  insertText(view, "/");
                  return true;
                }

                dispatchPromotedSegmentsUpdate(
                  view,
                  [...promotedSegments, segment],
                  { insert: "" },
                );
                startCompletion(view);
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

                dispatchPromotedSegmentsUpdate(
                  view,
                  [...getPromotedSegments(view), `${segment}:`],
                  { insert: "" },
                );
                startCompletion(view);
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
                  dispatchPromotedSegmentsUpdate(
                    view,
                    [...getPromotedSegments(view), "..."],
                    { insert: "" },
                  );
                  startCompletion(view);
                  return true;
                }

                if (!segment || !canPromoteLocationSegment(segment) || segment === "...") {
                  insertText(view, ".");
                  return true;
                }

                dispatchPromotedSegmentsUpdate(
                  view,
                  [...getPromotedSegments(view), segment],
                  { insert: "." },
                );
                startCompletion(view);
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
  promotedSegments,
  cursorToEndToken,
  historySearchActive,
  disabled = false,
  placeholder,
  onChange,
  onPromotedSegmentsChange,
  onSubmit,
  onAutocompleteRequest,
  onHistoryPrevious,
  onHistoryNext,
  onHistorySearchRequest,
  onHistorySearchBackspace,
  onHistorySearchAccept,
  onHistorySearchCancel,
  onHistorySearchAppend,
  onHistorySearchPreviousMatch,
  onHistorySearchNextMatch,
}: CommandInputEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const cursorToEndTokenRef = useRef(cursorToEndToken);
  const escapePrefixActiveRef = useRef(false);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [value]);

  useEffect(() => {
    if (!disabled) {
      textareaRef.current?.focus();
    }
  }, [disabled]);

  useEffect(() => {
    const shouldMoveCursorToEnd =
      cursorToEndTokenRef.current !== cursorToEndToken;
    cursorToEndTokenRef.current = cursorToEndToken;

    if (!shouldMoveCursorToEnd || disabled) {
      return;
    }

    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }

    const cursor = value.length;
    textarea.focus();
    textarea.setSelectionRange(cursor, cursor);
  }, [cursorToEndToken, disabled, value]);

  function moveTextareaCursor(cursor: number): void {
    requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea || disabled) {
        return;
      }

      textarea.focus();
      textarea.setSelectionRange(cursor, cursor);
    });
  }

  function handleChange(nextValue: string) {
    if (!shouldAttemptBulkPromotion(value, nextValue)) {
      onChange(nextValue);
      return;
    }

    const splitResult = extractPromotableSegments(nextValue);
    if (splitResult.promotedSegments.length > 0) {
      onPromotedSegmentsChange([
        ...promotedSegments,
        ...splitResult.promotedSegments,
      ]);
      onChange(splitResult.remainder);
      return;
    }

    onChange(nextValue);
  }

  function popTextareaLocationSegment(): boolean {
    if (promotedSegments.length === 0) {
      return false;
    }

    onPromotedSegmentsChange(promotedSegments.slice(0, -1));
    return true;
  }

  function demoteTextareaLocationSegmentAfterSingleDelete(): boolean {
    const demotedSegment = popLastPromotedSegmentChar(promotedSegments);
    if (!demotedSegment) {
      return false;
    }

    onPromotedSegmentsChange([...demotedSegment.nextSegments]);
    onChange(demotedSegment.insert);
    moveTextareaCursor(demotedSegment.insert.length);
    return true;
  }

  function deletePreviousWholeThingInTextarea(cursor: number): boolean {
    if (value === "") {
      return popTextareaLocationSegment();
    }

    const from = findPreviousWordStart(value, cursor);
    const nextValue = deletePreviousWordFromText(value, cursor);
    if (nextValue === value) {
      return false;
    }

    onChange(nextValue);
    moveTextareaCursor(from);
    return true;
  }

  return (
    <textarea
      ref={textareaRef}
      className="CommandShell-textarea"
      rows={1}
      value={value}
      onChange={(event) => handleChange(event.target.value)}
      onKeyDown={(event) => {
        const lowerKey = event.key.toLowerCase();
        const selectionStart = event.currentTarget.selectionStart;
        const selectionEnd = event.currentTarget.selectionEnd;
        const selectionCollapsed = selectionStart === selectionEnd;

        if (
          escapePrefixActiveRef.current &&
          event.key !== "Escape" &&
          event.key !== "Backspace" &&
          !(event.ctrlKey && lowerKey === "h")
        ) {
          escapePrefixActiveRef.current = false;
        }

        if (historySearchActive) {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            onHistorySearchAccept();
            return;
          }

          if (
            event.key === "Escape" ||
            (event.ctrlKey && lowerKey === "g")
          ) {
            event.preventDefault();
            onHistorySearchCancel();
            return;
          }

          if (
            event.key === "Backspace" ||
            (event.ctrlKey && lowerKey === "h")
          ) {
            event.preventDefault();
            onHistorySearchBackspace();
            return;
          }

          if (event.ctrlKey && lowerKey === "r") {
            event.preventDefault();
            onHistorySearchPreviousMatch();
            return;
          }

          if (event.ctrlKey && lowerKey === "p") {
            event.preventDefault();
            onHistorySearchPreviousMatch();
            return;
          }

          if (event.key === "ArrowUp") {
            event.preventDefault();
            onHistorySearchPreviousMatch();
            return;
          }

          if (event.ctrlKey && lowerKey === "n") {
            event.preventDefault();
            onHistorySearchNextMatch();
            return;
          }

          if (event.key === "ArrowDown") {
            event.preventDefault();
            onHistorySearchNextMatch();
            return;
          }

          if (
            event.key.length === 1 &&
            !event.ctrlKey &&
            !event.metaKey &&
            !event.altKey
          ) {
            event.preventDefault();
            onHistorySearchAppend(event.key);
            return;
          }

          if (event.key === "Tab") {
            event.preventDefault();
            return;
          }
        }

        if (event.key === "Escape") {
          event.preventDefault();
          escapePrefixActiveRef.current = true;
          return;
        }

        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          onSubmit({
            value,
            promotedSegments,
          });
          return;
        }

        if (event.key === "Tab") {
          event.preventDefault();
          onAutocompleteRequest();
          return;
        }

        if (event.ctrlKey && lowerKey === "p") {
          event.preventDefault();
          onHistoryPrevious();
          return;
        }

        if (event.key === "ArrowUp") {
          event.preventDefault();
          onHistoryPrevious();
          return;
        }

        if (event.ctrlKey && lowerKey === "n") {
          event.preventDefault();
          onHistoryNext();
          return;
        }

        if (event.key === "ArrowDown") {
          event.preventDefault();
          onHistoryNext();
          return;
        }

        if (event.ctrlKey && lowerKey === "r") {
          event.preventDefault();
          onHistorySearchRequest();
          return;
        }

        if (
          event.key === "/" &&
          selectionStart === value.length &&
          selectionEnd === value.length &&
          value === "/"
        ) {
          event.preventDefault();
          onPromotedSegmentsChange([...promotedSegments, "//"]);
          onChange("");
          return;
        }

        if (
          event.key === "/" &&
          selectionStart === value.length &&
          selectionEnd === value.length &&
          value === ""
        ) {
          event.preventDefault();
          if (shouldStartAbsoluteScope(promotedSegments)) {
            onChange("/");
          }
          return;
        }

        if (
          event.key === "/" &&
          selectionStart === value.length &&
          selectionEnd === value.length &&
          isRootLocationSegment(value)
        ) {
          event.preventDefault();
          onPromotedSegmentsChange([...promotedSegments, value]);
          onChange("/");
          return;
        }

        if (
          event.key === "/" &&
          selectionStart === value.length &&
          selectionEnd === value.length &&
          canPromoteLocationSegment(value)
        ) {
          event.preventDefault();
          onPromotedSegmentsChange([...promotedSegments, value]);
          onChange("");
          return;
        }

        if (
          event.key === ":" &&
          selectionStart === value.length &&
          selectionEnd === value.length &&
          value !== "" &&
          canPromoteLocationSegment(value)
        ) {
          event.preventDefault();
          onPromotedSegmentsChange([...promotedSegments, `${value}:`]);
          onChange("");
          return;
        }

        if (
          event.key === "." &&
          selectionStart === value.length &&
          selectionEnd === value.length
        ) {
          if (value === "..") {
            event.preventDefault();
            onPromotedSegmentsChange([...promotedSegments, "..."]);
            onChange("");
            return;
          }

          if (value !== "" && canPromoteLocationSegment(value) && value !== "...") {
            event.preventDefault();
            onPromotedSegmentsChange([...promotedSegments, value]);
            onChange(".");
            return;
          }
        }

        if (event.key === "Backspace") {
          const deleteWholeThing =
            escapePrefixActiveRef.current || event.altKey || event.ctrlKey;
          escapePrefixActiveRef.current = false;

          if (!selectionCollapsed) {
            return;
          }

          if (deleteWholeThing) {
            if (deletePreviousWholeThingInTextarea(selectionStart)) {
              event.preventDefault();
            }
            return;
          }

          if (value === "" && demoteTextareaLocationSegmentAfterSingleDelete()) {
            event.preventDefault();
          }
          return;
        }

        if (event.ctrlKey && lowerKey === "h") {
          event.preventDefault();
          const deleteWholeThing = escapePrefixActiveRef.current;
          escapePrefixActiveRef.current = false;

          if (!selectionCollapsed) {
            onChange(value.slice(0, selectionStart) + value.slice(selectionEnd));
            moveTextareaCursor(selectionStart);
            return;
          }

          if (deleteWholeThing) {
            deletePreviousWholeThingInTextarea(selectionStart);
            return;
          }

          if (value === "") {
            demoteTextareaLocationSegmentAfterSingleDelete();
            return;
          }

          if (selectionStart <= 0) {
            return;
          }

          onChange(
            value.slice(0, selectionStart - 1) + value.slice(selectionStart),
          );
          moveTextareaCursor(selectionStart - 1);
        }
      }}
      disabled={disabled}
      autoFocus={!disabled}
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
