import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "./useAuth";

export function MfaChallenge() {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [useBackup, setUseBackup] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const partialToken = sessionStorage.getItem("mfa_partial_token");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    const endpoint = useBackup ? "/auth/mfa/backup" : "/auth/mfa/verify";
    const body = useBackup
      ? { partial_token: partialToken, backup_code: code }
      : { partial_token: partialToken, totp_code: code };

    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const data = await res.json();

      if (!res.ok) {
        setError(data.detail || "Verification failed");
        return;
      }

      sessionStorage.removeItem("mfa_partial_token");
      login(data.access_token);
      navigate("/");
    } catch {
      setError("Network error");
    }
  };

  if (!partialToken) {
    navigate("/login");
    return null;
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="max-w-md w-full space-y-6 p-8 bg-white rounded-lg shadow">
        <h2 className="text-2xl font-bold text-center text-gray-900">
          {useBackup ? "Backup Code" : "Two-Factor Authentication"}
        </h2>
        {error && (
          <div className="bg-red-50 text-red-700 p-3 rounded text-sm">{error}</div>
        )}
        <form onSubmit={handleSubmit} className="space-y-4">
          <input
            type="text"
            placeholder={useBackup ? "Backup code" : "6-digit code"}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            className="w-full px-3 py-2 border rounded focus:ring-2 focus:ring-blue-400 text-center text-lg tracking-widest"
            autoFocus
            required
          />
          <button
            type="submit"
            className="w-full py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            Verify
          </button>
        </form>
        <button
          onClick={() => {
            setUseBackup(!useBackup);
            setCode("");
          }}
          className="w-full text-sm text-blue-600 hover:underline"
        >
          {useBackup ? "Use authenticator app" : "Use backup code"}
        </button>
      </div>
    </div>
  );
}
