import { useAuth } from "../auth/useAuth";
import { AssistantInput } from "../assistant/AssistantInput";

export function TopBar() {
  const { logout } = useAuth();

  return (
    <header className="h-14 bg-white border-b border-gray-200 flex items-center px-4 gap-4">
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
