import { useEffect, useState } from "react";

type GateUser = { email: string; name?: string; role?: string };

/**
 * "Signed in as" chip and sign-out button.
 *
 * Boardman has no login of its own. In production the platform nginx puts a sign-in gate in front
 * of it (deepiri-platform: ops/boardman-gate) and answers `/_gate/me` for a signed-in visitor.
 * Anywhere else (local dev, no gate) that path is a 404 or the SPA's own index.html, so the chip
 * renders nothing and the page looks exactly as before.
 */
export function GateSession() {
  const [user, setUser] = useState<GateUser | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/_gate/me", { credentials: "same-origin", headers: { Accept: "application/json" } });
        const type = res.headers.get("content-type") ?? "";
        if (!res.ok || !type.includes("application/json")) return;
        const data = (await res.json()) as GateUser;
        if (!cancelled && typeof data?.email === "string" && data.email) setUser(data);
      } catch {
        /* no gate in front of this deployment */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!user) return null;
  return (
    <div className="gate-session" role="group" aria-label="Account">
      <span className="gate-session__who" title={user.role ? `${user.email} (${user.role})` : user.email}>
        Signed in as <strong>{user.name?.trim() || user.email}</strong>
      </span>
      {/* A POST form, not a link: signing out must not be triggerable by a GET. */}
      <form method="post" action="/_gate/logout">
        <button type="submit" className="gate-session__out">
          Sign out
        </button>
      </form>
    </div>
  );
}
