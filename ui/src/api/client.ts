/** fetch wrapper with auth token injection. */

const TOKEN_KEY = "indemn_access_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export async function apiClient<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const baseUrl = (import.meta as any).env?.VITE_API_URL || "";
  const url = path.startsWith("http") ? path : `${baseUrl}${path}`;
  const response = await fetch(url, { ...options, headers });

  // Check for refreshed token header [G-39]
  const refreshedToken = response.headers.get("X-Refreshed-Token");
  if (refreshedToken) {
    setToken(refreshedToken);
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.detail || body.error || response.statusText);
  }

  return response.json();
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
  }
}
