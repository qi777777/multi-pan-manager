import { createContext, useContext, useState, useEffect } from 'react'
import { authApi } from '../services/auth'
import { message } from 'antd'

const AuthContext = createContext()

export function AuthProvider({ children }) {
    const [user, setUser] = useState(null)
    const [loading, setLoading] = useState(true)

    // 初始化：检查本地 Token 并拉取用户信息
    useEffect(() => {
        const checkAuth = async () => {
            const hasToken = authApi.init()
            if (hasToken) {
                try {
                    const { data } = await authApi.getMe()
                    setUser(data)
                } catch (error) {
                    console.error('Auth Init Error:', error)
                    authApi.logout()
                    setUser(null)
                }
            }
            setLoading(false)
        }
        checkAuth()
    }, [])

    const login = async (username, password) => {
        try {
            await authApi.login(username, password)
            const { data } = await authApi.getMe()
            setUser(data)
            message.success('登录成功')
            return true
        } catch (error) {
            const errorMsg = error.response?.data?.detail || '登录失败，请检查用户名或密码'
            message.error(errorMsg)
            return false
        }
    }

    const logout = () => {
        authApi.logout()
        setUser(null)
        message.info('已退出登录')
    }

    return (
        <AuthContext.Provider value={{ user, loading, login, logout }}>
            {children}
        </AuthContext.Provider>
    )
}

export const useAuth = () => useContext(AuthContext)
