import { useAuth } from "../auth/useAuth";

interface Props {
  sidebarOpen?: boolean;
  onToggleSidebar?: () => void;
  onOpenAssistant?: () => void;
  assistantOpen?: boolean;
}

export function TopBar({ sidebarOpen, onToggleSidebar, onOpenAssistant, assistantOpen }: Props) {
  const { logout } = useAuth();

  return (
    <header className="h-14 bg-white border-b border-gray-200 flex items-center px-4 gap-4">
      <button
        onClick={onToggleSidebar}
        className="text-gray-500 hover:text-gray-700 text-lg"
        title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
      >
        &#x2630;
      </button>
      <div className="flex-1" />
      {!assistantOpen && (
        <button
          onClick={onOpenAssistant}
          className="text-sm text-gray-500 hover:text-gray-700 flex items-center gap-1"
          title="Open Assistant (/ or Cmd+K)"
        >
          <span>Assistant</span>
          <kbd className="px-1 py-0.5 bg-gray-100 rounded text-xs font-mono">/</kbd>
        </button>
      )}
      <button
        onClick={logout}
        className="text-sm text-gray-500 hover:text-gray-700 whitespace-nowrap"
      >
        Sign out
      </button>
    </header>
  );
}
