import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import MainLayout from './components/MainLayout'
import LoginPage from './pages/LoginPage'
import AccountsPage from './pages/AccountsPage'
import FilesPage from './pages/FilesPage'
import TransferPage from './pages/TransferPage'
import CrossTransferPage from './pages/CrossTransferPage'
import SharesPage from './pages/SharesPage'
import LogsPage from './pages/LogsPage'
import DatabasePage from './pages/DatabasePage'
import { Spin } from 'antd'

// 路由守卫
function PrivateRoute({ children }) {
    const { user, loading } = useAuth()
    const location = useLocation()

    if (loading) {
        return (
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#1a1a2e' }}>
                <Spin size="large" />
            </div>
        )
    }

    if (!user) {
        return <Navigate to="/login" state={{ from: location }} replace />
    }

    return children
}

function App() {
    return (
        <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <AuthProvider>
                <Routes>
                    <Route path="/login" element={<LoginPage />} />
                    <Route
                        path="/"
                        element={
                            <PrivateRoute>
                                <MainLayout />
                            </PrivateRoute>
                        }
                    >
                        <Route index element={<Navigate to="/accounts" replace />} />
                        <Route path="accounts" element={<AccountsPage />} />
                        <Route path="files" element={<FilesPage />} />
                        <Route path="transfer" element={<TransferPage />} />
                        <Route path="cross-transfer" element={<CrossTransferPage />} />
                        <Route path="shares" element={<SharesPage />} />
                        <Route path="logs" element={<LogsPage />} />
                        <Route path="database" element={<DatabasePage />} />
                    </Route>
                    <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
            </AuthProvider>
        </BrowserRouter>
    )
}

export default App
