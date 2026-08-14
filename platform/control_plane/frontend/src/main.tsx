import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { getUiFlavor } from './ui/uiPreference'

// UI-flavor seam (Epic 31E): consult the persisted preference before the first render so a
// future epic can bring a second UI back without reshaping this entry point.
if (getUiFlavor() === 'cloudscape') {
  // FUTURE EPIC: replace this arm with a React.lazy import of the Cloudscape root
  // (`const CloudscapeApp = lazy(() => import('./CloudscapeApp'))`) and render it here
  // instead of <App />. Until then the preference is accepted but Classic still renders.
  console.info('Cloudscape UI is parked; rendering Classic. Code preserved at tags e31c-complete / e31d-parked.')
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
