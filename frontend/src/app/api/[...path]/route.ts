export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: Request, context: Context) {
  const { path } = await context.params;
  if (path.some((segment) => !/^[a-zA-Z0-9_.-]+$/.test(segment) || segment === "." || segment === "..")) {
    return Response.json({ detail: "bad_path" }, { status: 400 });
  }
  const headers = new Headers();
  for (const name of ["content-type", "accept", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const target = `${process.env.INTERNAL_API_URL ?? "http://api:8000"}/api/${path.join("/")}`;
  try {
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store",
      redirect: "manual",
      signal: request.signal,
    });
    const outgoing = new Headers();
    for (const name of ["content-type", "cache-control", "x-accel-buffering"]) {
      const value = response.headers.get(name);
      if (value) outgoing.set(name, value);
    }
    return new Response(response.body, { status: response.status, headers: outgoing });
  } catch {
    return Response.json({ detail: "api_unavailable" }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;
