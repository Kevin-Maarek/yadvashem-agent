export async function api<T>(path: string, data?: unknown): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    cache: "no-store",
    ...(data === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
    }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(typeof payload?.detail === "string" ? payload.detail : "invalid_input");
  }
  return response.json() as Promise<T>;
}
