import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'

/*
 * A soft gate: the password lives in the bundle, so this keeps the page from casual eyes, not from anyone
 * who reads the source. Real protection needs the host to ask for it before serving anything.
 */
const PASSWORD = 'ocean'
const KEY = 'pacific-unlocked'

function remembered() {
  try {
    return localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

export default function Gate({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(remembered)
  const [value, setValue] = useState('')
  const [wrong, setWrong] = useState(false)

  if (open) return children

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (value.trim().toLowerCase() === PASSWORD) {
      try {
        localStorage.setItem(KEY, '1')
      } catch {
        // private mode: it opens for this visit only
      }
      setOpen(true)
    } else {
      setWrong(true)
      setValue('')
    }
  }

  return (
    <main className="gate">
      <form className="gate-form" onSubmit={submit}>
        <p className="wordmark">Pacific</p>
        <label className="gate-label" htmlFor="gate-password">Password</label>
        <div className="gate-row">
          <input id="gate-password" className="gate-input" type="password" autoComplete="current-password"
            autoFocus value={value} aria-invalid={wrong} aria-describedby={wrong ? 'gate-error' : undefined}
            onChange={(e) => { setValue(e.target.value); setWrong(false) }} />
          <button className="gate-button" type="submit">Enter</button>
        </div>
        <p id="gate-error" className="gate-error" role="alert">{wrong ? 'That’s not it. Try again.' : ''}</p>
      </form>
    </main>
  )
}
