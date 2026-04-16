import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import AppRouter from './AppRouter'
import { initializeI18n } from './i18n'
import 'katex/dist/katex.min.css';
// Import KaTeX extensions at app startup to ensure they are registered before any rendering
import 'katex/contrib/mhchem'; // Chemistry formulas: \ce{} and \pu{}
import 'katex/contrib/copy-tex'; // Allow copying rendered formulas as LaTeX source

const renderApplication = async () => {
  await initializeI18n()

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <AppRouter />
    </StrictMode>
  )
}

void renderApplication()
