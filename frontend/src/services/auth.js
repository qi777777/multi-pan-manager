import api from './api'

export const authApi = {
    /**
     * 用户登录
     * @param {string} username 
     * @param {string} password 
     */
    login: async (username, password) => {
        const formData = new FormData()
        formData.append('username', username)
        formData.append('password', password)

        const { data } = await api.post('/auth/login', formData, {
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded'
            }
        })

        if (data.access_token) {
            localStorage.setItem('token', data.access_token)
        }
        return data
    },

    /**
     * 获取用户信息
     */
    getMe: () => api.get('/auth/me'),

    /**
     * 退出登录
     */
    logout: () => {
        localStorage.removeItem('token')
    },

    /**
     * 检查本地是否已有 Token 并初始化 Axios
     */
    init: () => {
        const token = localStorage.getItem('token')
        if (token) {
            return true
        }
        return false
    }
}
