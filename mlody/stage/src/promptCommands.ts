import { listStageE2eScenarioNames } from "./e2eTests.js";

export type StagePromptCommandParseResult =
  | { kind: "plain" }
  | {
      kind: "command";
      raw: string;
      name: string;
      args: string;
    }
  | {
      kind: "invalid";
      raw: string;
      message: string;
    };

const STAGE_PROMPT_COMMAND_PATTERN = /^,([^\s]+)(?:\s+([\s\S]*))?$/;
const STAGE_PROMPT_COMMAND_NAME_PATTERN = /^,[^\s]*$/;
const STAGE_PROMPT_COMMAND_VALID_FOR = /^[A-Za-z0-9_-]*$/;

export interface StagePromptAutocompleteOption {
  label: string;
  detail?: string;
  applySuffix?: string;
  triggerCompletionAfterApply?: boolean;
}

export interface StagePromptAutocompleteResult {
  from: number;
  options: StagePromptAutocompleteOption[];
  validFor: RegExp;
}

interface StagePromptCommandDefinition {
  name: string;
  description: string;
  completeArgs?: (args: string) => StagePromptAutocompleteResult | null;
}

const STAGE_QUERY_LIST_ENTITIES = [
  {
    name: "teams",
    description: "List team roots available in the selected workspace.",
  },
  {
    name: "users",
    description: "List registered users available in the selected workspace.",
  },
  {
    name: "tasks",
    description: "List registered tasks available in the selected workspace.",
  },
  {
    name: "types",
    description: "List registered types available in the selected workspace.",
  },
  {
    name: "locations",
    description: "List registered locations available in the selected workspace.",
  },
  {
    name: "values",
    description: "List top-level registered values available in the selected workspace.",
  },
] as const;

function completeE2eArgs(args: string): StagePromptAutocompleteResult | null {
  if (/\s/.test(args)) {
    return null;
  }

  return {
    from: ",e2e ".length,
    options: listStageE2eScenarioNames().map((name) => ({
      label: name,
      detail: "Named e2e scenario",
    })),
    validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
  };
}

function completeServerArgs(args: string): StagePromptAutocompleteResult | null {
  if (/\s/.test(args)) {
    return null;
  }

  return {
    from: ",server ".length,
    options: [
      {
        label: "status",
        detail: "Show live backend runtime details.",
      },
      {
        label: "restart",
        detail: "Restart the backend with its original cwd and argv.",
      },
    ],
    validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
  };
}

function completeDbArgs(args: string): StagePromptAutocompleteResult | null {
  if (/\s/.test(args)) {
    return null;
  }

  return {
    from: ",db ".length,
    options: [
      {
        label: "clear",
        detail: "Delete all rows from every table.",
      },
      {
        label: "status",
        detail: "Show row counts, date ranges, and storage statistics.",
      },
    ],
    validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
  };
}

function completeCacheArgs(args: string): StagePromptAutocompleteResult | null {
  if (/\s/.test(args)) {
    return null;
  }

  return {
    from: ",cache ".length,
    options: [
      {
        label: "status",
        detail: "Show cache size, top assets, and unreferenced entries.",
      },
      {
        label: "clean",
        detail: "Delete unreferenced cache entries.",
      },
      {
        label: "clean --all",
        detail: "Delete all cache entries.",
      },
    ],
    validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
  };
}

function completeQueryArgs(args: string): StagePromptAutocompleteResult | null {
  if (!/\s/.test(args)) {
    return {
      from: ",query ".length,
      options: "list".startsWith(args)
        ? [
            {
              label: "list",
              detail: "List a supported entity type for the selected workspace.",
              applySuffix: " ",
              triggerCompletionAfterApply: true,
            },
          ]
        : [],
      validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
    };
  }

  const entityMatch = args.match(/^list\s+([A-Za-z0-9_-]*)$/);
  if (entityMatch === null) {
    return null;
  }

  const prefix = entityMatch[1] ?? "";
  return {
    from: ",query list ".length,
    options: STAGE_QUERY_LIST_ENTITIES.filter((entity) =>
      entity.name.startsWith(prefix),
    ).map((entity) => ({
      label: entity.name,
      detail: entity.description,
    })),
    validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
  };
}

const STAGE_PROMPT_COMMANDS: readonly StagePromptCommandDefinition[] = [
  {
    name: "cache",
    description: "Show file-system cache statistics.",
    completeArgs: completeCacheArgs,
  },
  {
    name: "db",
    description: "Show database statistics.",
    completeArgs: completeDbArgs,
  },
  {
    name: "e2e",
    description: "Run a named client-side end-to-end scenario.",
    completeArgs: completeE2eArgs,
  },
  {
    name: "query",
    description: "Inspect registered workspace entities.",
    completeArgs: completeQueryArgs,
  },
  {
    name: "server",
    description: "Manage the local stage backend.",
    completeArgs: completeServerArgs,
  },
];

function getStagePromptCommandDefinition(
  name: string,
): StagePromptCommandDefinition | null {
  return STAGE_PROMPT_COMMANDS.find((definition) => definition.name === name) ?? null;
}

export function listStagePromptCommandNames(): string[] {
  return STAGE_PROMPT_COMMANDS.map((definition) => definition.name).sort(
    (left, right) => left.localeCompare(right),
  );
}

export function isStagePromptCommandName(name: string): boolean {
  return getStagePromptCommandDefinition(name) !== null;
}

export function listStageQueryListEntityNames(): string[] {
  return STAGE_QUERY_LIST_ENTITIES.map((entity) => entity.name);
}

export function parseStagePromptCommand(
  input: string,
): StagePromptCommandParseResult {
  const trimmed = input.trim();
  if (!trimmed.startsWith(",")) {
    return { kind: "plain" };
  }

  const match = trimmed.match(STAGE_PROMPT_COMMAND_PATTERN);
  if (!match) {
    return {
      kind: "invalid",
      raw: trimmed,
      message:
        "Stage commands must start with ',' immediately followed by the command name, for example ',e2e smoketest'.",
    };
  }

  return {
    kind: "command",
    raw: trimmed,
    name: match[1] ?? "",
    args: match[2]?.trim() ?? "",
  };
}

export function analyzeStagePromptAutocomplete(
  input: string,
): StagePromptAutocompleteResult | null {
  if (input === "" || !input.startsWith(",")) {
    return null;
  }

  if (STAGE_PROMPT_COMMAND_NAME_PATTERN.test(input)) {
    return {
      from: 1,
      options: STAGE_PROMPT_COMMANDS.map((definition) => ({
        label: definition.name,
        detail: definition.description,
        applySuffix: " ",
        triggerCompletionAfterApply: true,
      })),
      validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
    };
  }

  const match = input.match(/^,([^\s]+)\s+([\s\S]*)$/);
  if (match === null) {
    return null;
  }

  const definition = getStagePromptCommandDefinition(match[1] ?? "");
  return definition?.completeArgs?.(match[2] ?? "") ?? null;
}
