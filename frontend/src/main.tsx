import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { ConfirmProvider } from './components/ConfirmProvider'
import './index.css'
import './workbench.css'
import './studio.css'

let initialTheme = 'bright'
try { const saved = localStorage.getItem('theme'); if (['warm', 'cool', 'bright', 'light'].includes(saved ?? '')) initialTheme = saved! } catch { /* Use the light default. */ }
if (initialTheme !== 'warm') document.documentElement.setAttribute('data-theme', initialTheme)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfirmProvider><App /></ConfirmProvider>
  </React.StrictMode>
)
