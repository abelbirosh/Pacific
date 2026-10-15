/*
 * The password gate, run by Vercel before it serves anything. The password lives in the SITE_PASSWORD
 * environment variable, never in the bundle. A visitor who gets it right is handed a cookie holding a hash of
 * it, so changing the variable signs everyone out. Locally, `vite dev` skips this file and the page is open.
 */

const COOKIE = 'pacific-gate'
const MONTH = 60 * 60 * 24 * 30
// the gate's own backdrop has to load before anyone is in
const OPEN_PATHS = new Set(['/favicon.svg', '/assets/pacific-sea-poster.jpg'])

async function token(password: string) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`pacific:${password}`))
  return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, '0')).join('')
}

function cookie(request: Request, name: string) {
  for (const part of (request.headers.get('cookie') ?? '').split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return rest.join('=')
  }
  return undefined
}

function page(wrong: boolean) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="robots" content="noindex" />
<title>Pacific</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<link href="https://api.fontshare.com/v2/css?f[]=switzer@400,500,600&display=swap" rel="stylesheet" />
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body {
    margin: 0; display: grid; place-items: center; min-height: 100svh; padding: clamp(1rem, 3vw, 2.5rem);
    background: linear-gradient(rgb(4 29 68 / 0.6), rgb(4 29 68 / 0.6)), url('/assets/pacific-sea-poster.jpg') center / cover, #0e171d;
    color: #fff; font-family: 'Switzer', ui-sans-serif, system-ui, sans-serif; font-size: 1.0625rem; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  form { width: min(100%, 22rem); display: grid; gap: 0.75rem; }
  p { margin: 0; }
  .wordmark { font-size: 2.5rem; font-weight: 600; letter-spacing: -0.04em; margin-bottom: 1rem; }
  label, .error { font-size: 0.875rem; font-weight: 500; }
  .row { display: flex; gap: 8px; }
  input, button { font: inherit; border: 0; border-radius: 0; padding: 0.75rem 1rem; }
  input { flex: 1; min-width: 0; background: rgb(255 255 255 / 0.92); color: #0b1218; }
  input[aria-invalid='true'] { outline: 3px solid #ff3b1f; }
  button { background: #fff; color: #0b1218; font-weight: 600; cursor: pointer; transition: background 200ms ease; }
  @media (hover: hover) and (pointer: fine) { button:hover { background: #e4ecf0; } }
  :focus-visible { outline: 3px solid #ff3b1f; outline-offset: 3px; }
  .error { min-height: 1.5em; }
</style>
</head>
<body>
<form method="post" action="/unlock">
  <p class="wordmark">Pacific</p>
  <label for="password">Password</label>
  <div class="row">
    <input id="password" name="password" type="password" autocomplete="current-password" autofocus required
      ${wrong ? 'aria-invalid="true" aria-describedby="error"' : ''} />
    <button type="submit">Enter</button>
  </div>
  <p id="error" class="error" role="alert">${wrong ? 'That’s not it. Try again.' : ''}</p>
</form>
</body>
</html>`
}

function gate(wrong: boolean) {
  return new Response(page(wrong), {
    status: 401,
    headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' },
  })
}

export default async function middleware(request: Request) {
  const url = new URL(request.url)
  if (OPEN_PATHS.has(url.pathname)) return

  const password = process.env.SITE_PASSWORD
  // fail closed: without a password set, nothing is served
  if (!password) return new Response('SITE_PASSWORD is not set.', { status: 503 })
  const expected = await token(password)

  if (url.pathname === '/unlock' && request.method === 'POST') {
    const form = await request.formData()
    const given = String(form.get('password') ?? '').trim().toLowerCase()
    if ((await token(given)) !== expected) return gate(true)
    return new Response(null, {
      status: 303,
      headers: {
        location: '/',
        'set-cookie': `${COOKIE}=${expected}; Path=/; Max-Age=${MONTH}; HttpOnly; Secure; SameSite=Lax`,
      },
    })
  }

  if (cookie(request, COOKIE) === expected) return
  return gate(false)
}
