import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App.jsx'
import './styles.css'

// Токена здесь нет и быть не должно: он приходит от пользователя и живёт
// в localStorage. В статике — только каркас приложения.
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
