import axios from 'axios'

export const api = axios.create({
    baseURL: '/api',
    timeout: 60000,
    headers: {
        'Content-Type': 'application/json'
    }
})

// 网盘类型映射
export const DISK_TYPES = {
    0: { name: '夸克网盘', color: '#1890ff', icon: '🔷' },
    1: { name: '阿里云盘', color: '#ff9500', icon: '📦' },
    2: { name: '百度网盘', color: '#06a7ff', icon: '💾' },
    3: { name: 'UC网盘', color: '#f5222d', icon: '🔴' },
    4: { name: '迅雷云盘', color: '#52c41a', icon: '⚡' }
}

// 账户管理 API
export const accountApi = {
    // 获取账户列表
    getList: () => api.get('/accounts'),

    // 获取账户详情（含凭证）
    getDetail: (id) => api.get(`/accounts/${id}`),

    // 创建账户
    create: (data) => api.post('/accounts', data),

    // 更新账户
    update: (id, data) => api.put(`/accounts/${id}`, data),

    // 删除账户
    delete: (id) => api.delete(`/accounts/${id}`),

    // 检测状态
    checkStatus: (id) => api.post(`/accounts/${id}/check`)
}

// 文件管理 API
export const fileApi = {
    // 获取文件列表
    getList: (accountId, pdirFid = '0') =>
        api.get(`/files/${accountId}`, { params: { pdir_fid: pdirFid } }),

    // 删除文件
    delete: (accountId, fidList) =>
        api.delete(`/files/${accountId}`, { data: fidList }),

    // 上传文件
    upload: (formData, onUploadProgress) =>
        api.post('/files/upload', formData, {
            headers: {
                'Content-Type': 'multipart/form-data'
            },
            onUploadProgress
        }),

    // 搜索文件（全盘）
    search: (accountId, keyword, page = 1, size = 50) =>
        api.get(`/files/${accountId}/search`, { params: { keyword, page, size } }),

    // 发送验证码
    sendVerificationCode: (accountId, data) =>
        api.post(`/files/${accountId}/verify/send`, data),

    // 校验验证码
    checkVerificationCode: (accountId, data) =>
        api.post(`/files/${accountId}/verify/check`, data)
}

// 转存管理 API
export const transferApi = {
    // 解析链接
    parse: (url, code = '') =>
        api.post('/transfer/parse', { url, code }),

    // 执行转存
    execute: (data) => api.post('/transfer/execute', data),

    // 获取任务列表
    getTasks: (skip = 0, limit = 20) =>
        api.get('/transfer/tasks', { params: { skip, limit } }),

    // 获取任务详情
    getTask: (id) => api.get(`/transfer/tasks/${id}`)
}

// 请求拦截器：注入 Token
api.interceptors.request.use(
    (config) => {
        const token = localStorage.getItem('token')
        if (token) {
            config.headers.Authorization = `Bearer ${token}`
        }
        return config
    },
    (error) => Promise.reject(error)
)

// 分享管理 API
export const shareApi = {
    // 获取分享列表
    getList: (filters = {}, skip = 0, limit = 50) =>
        api.get('/shares', { params: { ...filters, skip, limit } }),

    // 创建分享
    create: (data) => api.post('/shares', data),

    // 删除分享
    delete: (id) => api.delete(`/shares/${id}`),

    // 删除分享并一并删除源文件
    deleteWithFile: (id) => api.delete(`/shares/${id}/file`),

    // 批量创建分享
    batchCreate: (data) => api.post('/shares/batch-create', data),

    // 批量操作（取消、删除等）
    batchAction: (data) => api.post('/shares/batch', data)
}

// 响应拦截器：处理 401
api.interceptors.response.use(
    (response) => response,
    (error) => {
        if (error.response?.status === 401) {
            localStorage.removeItem('token')
            if (!window.location.pathname.includes('/login')) {
                window.location.href = '/login'
            }
        }
        return Promise.reject(error)
    }
)

export default api
