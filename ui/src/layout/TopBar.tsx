import { useAuth } from "../auth/useAuth";
import { AssistantInput } from "../assistant/AssistantInput";

interface Props {
  sidebarOpen?: boolean;
  onToggleSidebar?: () => void;
}

export function TopBar({ sidebarOpen, onToggleSidebar }: Props) {
  const { logout } = useAuth();

  return (
    <header className="h-14 bg-white border-b border-gray-200 flex items-center px-4 gap-4">
      <button
        onClick={onToggleSidebar}
        className="text-gray-500 hover:text-gray-700 text-lg"
        title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
      >
        {sidebarOpen ? "\u2630" : "\u2630"}
      </button>
      <AssistantInput />
      <button
        onClick={logout}
        className="text-sm text-gray-500 hover:text-gray-700 whitespace-nowrap"
      >
        Sign out
      </button>
    </header>
  );
}
