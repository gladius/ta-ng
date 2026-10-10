import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import './styles.css'
import App from './App'

const queries = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, retry: 1 } } })

// Printing (or saving as PDF) shows every finding and its evidence, not just the ones someone opened.
addEventListener('beforeprint', () => document.querySelectorAll('details:not([open])').forEach((d) => d.setAttribute('open', '')))

// No StrictMode: in development it runs effects twice, which would open a paid audit stream twice.
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queries}>
    <BrowserRouter basename="/app">
      <App />
    </BrowserRouter>
  </QueryClientProvider>,
)
