// Private access layer for the dashboard (runs on Vercel before every request).
// The browser asks for a username + password; both live in Vercel Environment
// Variables (DASHBOARD_USER, DASHBOARD_PASSWORD) - never in this repository.
// If the password variable is missing the site stays closed.

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default function middleware(request) {
  const user = process.env.DASHBOARD_USER || 'admin';
  const password = process.env.DASHBOARD_PASSWORD;

  if (!password) {
    return new Response('Dashboard is locked: set DASHBOARD_PASSWORD in Vercel > Settings > Environment Variables, then redeploy.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
    });
  }

  const header = request.headers.get('authorization') || '';
  if (header.startsWith('Basic ')) {
    try {
      const bytes = Uint8Array.from(atob(header.slice(6)), (c) => c.charCodeAt(0));
      const decoded = new TextDecoder().decode(bytes);
      const sep = decoded.indexOf(':');
      const givenUser = decoded.slice(0, sep);
      const givenPassword = decoded.slice(sep + 1);
      if (sep > -1 && safeEqual(givenUser, user) && safeEqual(givenPassword, password)) {
        // Let the request continue to the static files.
        return new Response(null, { headers: { 'x-middleware-next': '1' } });
      }
    } catch (_) {
      // fall through to the 401 below
    }
  }

  return new Response('Authentication required.', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="Private deals dashboard", charset="UTF-8"',
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}
