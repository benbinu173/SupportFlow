import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// Typography, loaded before anything renders. ADR-010 bans Inter; Geist is the
// named alternative, and the variable build keeps the whole weight axis in two
// files rather than one file per weight.
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'

import { App } from '@/app/App'

import './index.css'

const rootElement = document.getElementById('root')

if (!rootElement) {
  throw new Error('Root element #root not found in index.html')
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
