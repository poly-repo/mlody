import { Sparkles } from "lucide-react";
import type {
  BreadcrumbSegment,
  CommandOption,
  UserSummary,
  WorkspaceSummary,
} from "../types.js";
import { LocationControl } from "./LocationControl.js";
import {
  Avatar,
  AvatarBadge,
  AvatarFallback,
  AvatarImage,
} from "./ui/avatar.js";
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
  workspace: WorkspaceSummary | null;
  showLocation: boolean;
  currentUser: UserSummary;
  onCommandChange: (command: string) => void;
}

export function InputToolbar({
  commandOptions,
  currentCommand,
  breadcrumbs,
  workspace,
  showLocation,
  currentUser,
  onCommandChange,
}: InputToolbarProps) {
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
        {showLocation ? (
          <LocationControl breadcrumbs={breadcrumbs} workspace={workspace} />
        ) : null}
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
