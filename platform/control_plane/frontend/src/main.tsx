import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { bootAuth } from './auth/authBoot'
import { getUiFlavor } from './ui/uiPreference'

// Rationale: frontend/README.md "The UI is Classic, and that is deliberate" (parked Cloudscape arm)
if (getUiFlavor() === 'cloudscape') {
  console.info('Cloudscape UI is parked; rendering Classic. Code preserved at tags e31c-complete / e31d-parked.')
}

void bootAuth().then(async (step) => {
  if (step !== 'mount') return
  const { default: App } = await import('./App')
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
})
