import { House, Sparkles } from "lucide-react";
import type {
  BreadcrumbSegment,
  CommandOption,
  UserSummary,
} from "../types.js";
import {
  Avatar,
  AvatarBadge,
  AvatarFallback,
  AvatarImage,
} from "./ui/avatar.js";
import {
  Breadcrumb,
  BreadcrumbEllipsis,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "./ui/breadcrumb.js";
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
  breadcrumbs: BreadcrumbSegment[];
  currentUser: UserSummary;
  onCommandChange: (command: string) => void;
}

function buildCompactBreadcrumbs(segments: BreadcrumbSegment[]) {
  if (segments.length <= 4) {
    return segments.map((segment, index) => ({
      ...segment,
      key: `${segment.label}-${index}`,
      kind: "segment" as const,
    }));
  }

  const tail = segments.slice(-2);
  return [
    {
      ...segments[0],
      key: `${segments[0]?.label ?? "root"}-0`,
      kind: "segment" as const,
    },
    {
      key: "ellipsis",
      kind: "ellipsis" as const,
    },
    ...tail.map((segment, index) => ({
      ...segment,
      key: `${segment.label}-${segments.length - tail.length + index}`,
      kind: "segment" as const,
    })),
  ];
}

export function InputToolbar({
  commandOptions,
  currentCommand,
  breadcrumbs,
  currentUser,
  onCommandChange,
}: InputToolbarProps) {
  const compactBreadcrumbs = buildCompactBreadcrumbs(breadcrumbs);
  const currentCommandOption =
    commandOptions.find((option) => option.value === currentCommand) ?? null;

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
        <Breadcrumb>
          <BreadcrumbList className="CommandToolbar-breadcrumbs">
            <BreadcrumbItem>
              <BreadcrumbLink
                href="#workspace-root"
                className="CommandToolbar-home"
              >
                <House className="CommandToolbar-homeIcon" />
                <span className="sr-only">Workspace root</span>
              </BreadcrumbLink>
              {compactBreadcrumbs.length > 0 && <BreadcrumbSeparator />}
            </BreadcrumbItem>
            {compactBreadcrumbs.map((segment, index) => (
              <BreadcrumbItem key={segment.key}>
                {segment.kind === "ellipsis" ? (
                  <>
                    <BreadcrumbEllipsis className="CommandToolbar-ellipsis" />
                    {index < compactBreadcrumbs.length - 1 && <BreadcrumbSeparator />}
                  </>
                ) : (
                  <>
                    {index === compactBreadcrumbs.length - 1 ? (
                      <BreadcrumbPage className="CommandToolbar-current">
                        {segment.label}
                      </BreadcrumbPage>
                    ) : (
                      <BreadcrumbLink
                        href={segment.href ?? "#"}
                        className="CommandToolbar-link"
                      >
                        {segment.label}
                      </BreadcrumbLink>
                    )}
                    {index < compactBreadcrumbs.length - 1 && <BreadcrumbSeparator />}
                  </>
                )}
              </BreadcrumbItem>
            ))}
          </BreadcrumbList>
        </Breadcrumb>
      </div>

      <div className="CommandToolbar-user">
        <div className="CommandToolbar-userCard" tabIndex={0}>
          <Avatar size="lg" className="CommandToolbar-avatar">
            {currentUser.avatarUrl && (
              <AvatarImage src={currentUser.avatarUrl} alt={currentUser.name} />
            )}
            <AvatarFallback>{currentUser.initials}</AvatarFallback>
            <AvatarBadge>
              <Sparkles className="CommandToolbar-badgeIcon" />
            </AvatarBadge>
          </Avatar>
          <div className="CommandToolbar-userPopup" role="tooltip">
            <span className="CommandToolbar-userName">{currentUser.name}</span>
            <span className="CommandToolbar-userRole">{currentUser.role}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
