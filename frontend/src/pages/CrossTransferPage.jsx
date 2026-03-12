import { useState, useEffect, useRef } from 'react'
import { Card, Select, Button, Input, Table, Tag, Space, message, Breadcrumb, Divider, Modal } from 'antd'
import { SwapOutlined, FolderOutlined, FileOutlined, ArrowRightOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { accountApi, fileApi, transferApi } from '../services/api'
import api from '../services/api'
import FolderPicker from '../components/FolderPicker'

const { Option } = Select

export default function CrossTransferPage() {
    // 状态定义
    const [accounts, setAccounts] = useState([])
    const [sourceAccount, setSourceAccount] = useState(null)
    const [targetAccounts, setTargetAccounts] = useState([])  // 改为数组支持多选
    const [targetPaths, setTargetPaths] = useState({})  // 每个目标账户的独立路径 {accountId: path}
    const [resetKey, setResetKey] = useState(0) // 用于强制重置目录选择器

    // 文件浏览器状态
    const [currentPath, setCurrentPath] = useState([{ name: '根目录', fid: '0' }])
    const [files, setFiles] = useState([])
    const [loadingFiles, setLoadingFiles] = useState(false)
    const [selectedFile, setSelectedFile] = useState(null)

    // 搜索状态
    const [searchKeyword, setSearchKeyword] = useState('')
    const [searchResults, setSearchResults] = useState([])
    const [isSearching, setIsSearching] = useState(false)
    const [searchMode, setSearchMode] = useState(false)  // 是否在搜索模式

    // 任务状态
    const [tasks, setTasks] = useState([])
    const [loadingTasks, setLoadingTasks] = useState(false)
    const [submitting, setSubmitting] = useState(false)
    const [pagination, setPagination] = useState({
        current: 1,
        pageSize: 10,
        total: 0
    })
    const paginationRef = useRef(pagination)
    useEffect(() => { paginationRef.current = pagination }, [pagination])

    // 目标路径选择状态
    const [targetBrowserVisible, setTargetBrowserVisible] = useState(false)
    const [targetPathStack, setTargetPathStack] = useState([{ name: '根目录', fid: '0' }])
    const [targetFiles, setTargetFiles] = useState([])
    const [loadingTargetFiles, setLoadingTargetFiles] = useState(false)

    const fetchTargetFiles = async (fid) => {
        if (targetAccounts.length === 0) return
        setLoadingTargetFiles(true)
        try {
            const { data } = await fileApi.getList(targetAccounts[0], fid)
            setTargetFiles(data)
        } catch (error) {
            message.error('获取目标文件列表失败')
        } finally {
            setLoadingTargetFiles(false)
        }
    }

    // 初始化
    useEffect(() => {
        fetchAccounts()
        fetchTasks()
    }, [])

    // 自动刷新：当有进行中、待处理或正在取消的任务时，每 3 秒静默刷新一次
    // 4. SSE 实时任务更新
    useEffect(() => {
        // 使用变量记录上一次刷新的时间，实现简单的防抖
        let lastRefreshTime = 0
        let refreshTimeout = null

        const debouncedFetchTasks = () => {
            const now = Date.now()
            // 如果距离上次刷新小于 500ms，则延迟刷新
            if (now - lastRefreshTime < 500) {
                if (refreshTimeout) clearTimeout(refreshTimeout)
                refreshTimeout = setTimeout(() => {
                    lastRefreshTime = Date.now()
                    fetchTasks(false)
                }, 500)
                return
            }
            lastRefreshTime = now
            fetchTasks(false)
        }

        // 建立 SSE 连接，通过 query parameter 传 token 解决 Header 鉴权限制
        console.log('%c[SSE] 组件挂载，准备初始化连接...', 'color: gray')
        const token = localStorage.getItem('token')
        const eventSource = new EventSource(`/api/cross-transfer/events?token_query=${token}&t=${new Date().getTime()}`)

        eventSource.onopen = () => {
            console.log('%c[SSE] 连接已成功建立', 'color: green; font-weight: bold')
        }

        eventSource.onerror = (error) => {
            console.error('%c[SSE] 连接异常:', 'color: red', error)
            // EventSource 默认会自动尝试重连，这里仅作记录
        }

        eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data)

                // 处理任务创建事件 (本地追加以实现零延迟显示)
                if (data.type === 'task_created' && data.task) {
                    const newTask = data.task
                    setTasks(prevTasks => {
                        // 防止重复添加
                        if (prevTasks.find(t => String(t.id) === String(newTask.id))) {
                            return prevTasks
                        }

                        // 仅当新任务是顶层任务（父ID和主ID均为空）时，才添加到外部列表
                        if (newTask.parent_task_id === null && newTask.master_task_id === null) {
                            return [newTask, ...prevTasks]
                        }

                        // 对于子任务或父任务，触发刷新的防抖逻辑以更新整体结构
                        debouncedFetchTasks()
                        return prevTasks
                    })
                    return
                }

                // [核心优化] 局部更新任务状态
                if (data.type === 'task_updated' && data.task_id) {
                    const eventTaskId = String(data.task_id)
                    let found = false

                    setTasks(prevTasks => {
                        const newTasks = prevTasks.map(task => {
                            if (String(task.id) === eventTaskId) {
                                found = true
                                return { ...task, ...data }
                            }
                            // 递归更新三层结构 (Master -> Parent -> Child)
                            if (task.target_tasks) {
                                const newTargetTasks = task.target_tasks.map(tt => {
                                    if (String(tt.id) === eventTaskId) {
                                        found = true
                                        return { ...tt, ...data }
                                    }
                                    if (tt.children) {
                                        const newChildren = tt.children.map(ct => {
                                            if (String(ct.id) === eventTaskId) {
                                                found = true
                                                return { ...ct, ...data }
                                            }
                                            return ct
                                        })
                                        return { ...tt, children: newChildren }
                                    }
                                    return tt
                                })
                                return { ...task, target_tasks: newTargetTasks }
                            }
                            // 递归更新两层结构 (Parent -> Child)
                            if (task.children) {
                                const newChildren = task.children.map(ct => {
                                    if (String(ct.id) === eventTaskId) {
                                        found = true
                                        return { ...ct, ...data }
                                    }
                                    return ct
                                })
                                return { ...task, children: newChildren }
                            }
                            return task
                        })
                        return newTasks
                    })

                    // 如果更新的是一个当前列表中没有的任务，或者状态发生了重大变更，则触发防抖刷新
                    if (!found || (data.status !== undefined && data.status !== 1)) {
                        debouncedFetchTasks()
                    }
                } else if (data.type !== 'heartbeat') {
                    // 其他指令降级为全量刷新
                    debouncedFetchTasks()
                }
            } catch (e) {
                // 忽略非 JSON 消息
            }
        }

        eventSource.onerror = (err) => {
            // 只有当连接不是被主动关闭时才报错
            if (eventSource.readyState !== EventSource.CLOSED) {
                console.error('[SSE] SSE 异常 (状态码: ' + eventSource.readyState + ')，将自动尝试重连...', err)
            }
        }

        return () => {
            console.log('[SSE] 正在关闭连接...')
            eventSource.close()
        }
    }, [])



    // 监听源账户变化，加载文件列表
    useEffect(() => {
        if (sourceAccount) {
            setCurrentPath([{ name: '根目录', fid: '0' }])
            fetchFiles('0')
        } else {
            setFiles([])
        }
    }, [sourceAccount])

    // API 调用
    const fetchAccounts = async () => {
        try {
            const { data } = await accountApi.getList()
            setAccounts(data.filter(a => a.status === 1))
        } catch (error) {
            message.error('获取账户列表失败')
        }
    }

    const fetchFiles = async (fid) => {
        if (!sourceAccount) return
        setLoadingFiles(true)
        try {
            const { data } = await fileApi.getList(sourceAccount, fid)
            setFiles(data)
        } catch (error) {
            message.error('获取文件列表失败')
        } finally {
            setLoadingFiles(false)
        }
    }

    // 搜索文件
    const handleSearch = async () => {
        if (!sourceAccount) {
            message.warning('请先选择源网盘')
            return
        }
        if (!searchKeyword.trim()) {
            message.warning('请输入搜索关键词')
            return
        }

        setIsSearching(true)
        setSearchMode(true)
        try {
            const { data } = await fileApi.search(sourceAccount, searchKeyword.trim())
            setSearchResults(data.list || [])
            if (data.list?.length === 0) {
                message.info('未找到匹配的文件')
            }
        } catch (error) {
            message.error('搜索失败: ' + (error.response?.data?.detail || error.message))
        } finally {
            setIsSearching(false)
        }
    }

    // 清除搜索
    const clearSearch = () => {
        setSearchMode(false)
        setSearchKeyword('')
        setSearchResults([])
    }

    const fetchTasks = async (showLoading = true, page = paginationRef.current.current, pageSize = paginationRef.current.pageSize) => {
        // 只在手动刷新时显示 loading 状态，自动刷新时静默更新
        if (showLoading) {
            setLoadingTasks(true)
        }
        try {
            // 添加时间戳防止缓存
            const skip = (page - 1) * pageSize
            const { data } = await api.get(`/cross-transfer/tasks?skip=${skip}&limit=${pageSize}&t=${new Date().getTime()}`)

            // 兼容旧版 API (Array) 和新版 API ({total, items})
            let parentTasks = []
            let total = 0

            if (Array.isArray(data)) {
                parentTasks = data
                total = data.length // 旧版无法获取真实总数
            } else {
                parentTasks = data.items
                total = data.total
            }

            // 前端双重过滤：彻底排除所有子任务（确保 parent_task_id 为 null 或 undefined）
            // 注意：因为后端已经过滤了，这里其实是保险
            const filteredTasks = parentTasks.filter(t => t.parent_task_id === null || t.parent_task_id === undefined)

            setTasks(filteredTasks)
            setPagination(prev => ({ ...prev, current: page, pageSize: pageSize, total: total }))

        } catch (error) {
            console.error('获取任务列表失败', error)
        } finally {
            if (showLoading) {
                setLoadingTasks(false)
            }
        }
    }


    const handleTransfer = async () => {
        if (!sourceAccount || targetAccounts.length === 0 || !selectedFile) {
            message.warning('请选择源账户、源文件/文件夹和目标账户')
            return
        }

        setSubmitting(true)
        try {
            // 百度网盘文件夹传输需要 path 而非 fid
            const currentAccount = accounts.find(a => a.id === sourceAccount)
            let finalFid = selectedFile.fid
            if (currentAccount?.type === 2 && selectedFile.is_dir && selectedFile.path) {
                finalFid = selectedFile.path
            }

            const response = await api.post('/cross-transfer/start', {
                source_account_id: sourceAccount,
                source_fid: finalFid,
                source_file_name: selectedFile.name,
                target_account_ids: targetAccounts,
                target_paths: targetPaths,  // 发送每个目标的独立路径
                is_folder: selectedFile.is_dir || false
            })
            const data = response.data
            if (selectedFile.is_dir) {
                message.success(`文件夹传输任务已启动`)
            } else if (data.task_ids) {
                message.success(`已创建 ${data.task_ids.length} 个转存任务`)
            } else {
                message.success('任务已启动')
            }
            fetchTasks()
            setSelectedFile(null)
            // [优化] 任务启动后保留目标配置，仅重置已选文件，方便连续操作
            // setTargetAccounts([]) 
            // setTargetPaths({})    
            // setResetKey(prev => prev + 1) 
        } catch (error) {
            message.error(error.response?.data?.detail || '启动失败')
        } finally {
            setSubmitting(false)
        }
    }

    // 文件浏览相关
    const handleFolderClick = (record) => {
        const newPath = [...currentPath, { name: record.name, fid: record.fid }]
        setCurrentPath(newPath)
        fetchFiles(record.fid)
    }

    const handleBreadcrumbClick = (index) => {
        const newPath = currentPath.slice(0, index + 1)
        setCurrentPath(newPath)
        fetchFiles(newPath[newPath.length - 1].fid)
    }

    // 渲染
    const columns = [
        {
            title: '文件名',
            dataIndex: 'name',
            key: 'name',
            render: (text, record) => (
                <Space>
                    {record.is_dir ? <FolderOutlined style={{ color: '#faad14' }} /> : <FileOutlined />}
                    {record.is_dir ? (
                        <a onClick={() => handleFolderClick(record)}>{text}</a>
                    ) : (
                        <span style={record.disabled ? { color: '#ccc', textDecoration: 'line-through' } : {}}>{text}</span>
                    )}
                    {record.disabled && (
                        <Tag color="error" style={{ fontSize: 10, lineHeight: '16px' }}>违规/无法下载</Tag>
                    )}
                </Space>
            )
        },
        {
            title: '大小',
            dataIndex: 'size',
            key: 'size',
            width: 120,
            render: (size) => {
                if (size === 0) return '-'
                if (size < 1024) return size + ' B'
                if (size < 1024 * 1024) return (size / 1024).toFixed(2) + ' KB'
                if (size < 1024 * 1024 * 1024) return (size / 1024 / 1024).toFixed(2) + ' MB'
                return (size / 1024 / 1024 / 1024).toFixed(2) + ' GB'
            }
        },
        {
            title: '修改时间',
            dataIndex: 'updated_at',
            key: 'updated_at',
            width: 180,
            render: (text) => {
                if (!text) return '-';
                // 处理时间戳（毫秒或秒）和 ISO 格式
                let date;
                if (typeof text === 'number') {
                    // 如果是时间戳，判断是秒还是毫秒
                    date = new Date(text > 1e12 ? text : text * 1000);
                } else {
                    date = new Date(text);
                }
                return date.toLocaleString('zh-CN', { hour12: false });
            }
        }
    ]



    // 扩展任务列 - 添加方向和进度
    const enhancedTaskColumns = [
        {
            title: '方向',
            key: 'direction',
            width: 160,
            render: (_, record) => {
                const typeEmoji = { 0: '🌟夸克', 1: '💡阿里', 2: '☁️百度', 4: '⚡迅雷', 3: '🐿️UC' }
                const srcType = record.source_account_type

                // 处理三层结构的主任务：显示所有目标
                if (record.is_master === 1 && record.target_tasks) {
                    return (
                        <div style={{ fontSize: 11, lineHeight: '1.4' }}>
                            {record.target_tasks.map((t, idx) => (
                                <div key={idx}>
                                    {typeEmoji[srcType] || srcType} → {typeEmoji[t.target_account_type] || t.target_account_type}
                                </div>
                            ))}
                        </div>
                    )
                }

                // 处理普通任务
                const tgtType = record.target_account_type
                return (
                    <span style={{ fontSize: 12 }}>
                        {typeEmoji[srcType] || srcType} → {typeEmoji[tgtType] || tgtType}
                    </span>
                )
            }
        },
        {
            title: '源文件',
            dataIndex: 'source_file_name',
            key: 'source_file_name',
            ellipsis: true
        },
        {
            title: '目标路径',
            dataIndex: 'target_path',
            key: 'target_path',
            ellipsis: true,
            width: 150,
            render: (text) => (text === '0' || !text) ? '/' : text
        },
        {
            title: '状态',
            key: 'status_progress',
            width: 180,
            render: (_, record) => {
                const colors = { '待处理': 'default', '进行中': 'processing', '成功': 'success', '失败': 'error', '部分成功': 'warning', '已取消': 'default' }
                if (record.status === 1) {
                    // 进行中 - 显示进度
                    return (
                        <div>
                            <Tag color="processing">{record.progress || 0}%</Tag>
                            <div style={{ fontSize: 11, color: '#666', marginTop: 2 }}>{record.current_step || '处理中...'}</div>
                        </div>
                    )
                }
                return <Tag color={colors[record.status_name]}>{record.status_name}</Tag>
            }
        },
        {
            title: '结果',
            key: 'result',
            ellipsis: true,
            render: (_, record) => {
                if (record.status_name === '失败') {
                    return <span style={{ color: 'red', fontSize: 12 }} title={record.error_message}>{record.error_message?.substring(0, 30)}...</span>
                }
                if (record.status_name === '部分成功') {
                    return <span style={{ color: '#faad14', fontSize: 12 }}>{record.current_step}</span>
                }
                if (record.status_name === '成功') {
                    const typeMap = { 0: '秒传', 1: '普通上传', 2: '流式传输' }
                    return <span style={{ color: 'green' }}>{typeMap[record.transfer_type]}</span>
                }
                return '-'
            }
        },
        {
            title: '操作',
            key: 'action',
            width: 150,
            render: (_, record) => {
                const handleAction = async (action) => {
                    try {
                        await api.post(`/cross-transfer/${action}/${record.id}`)
                        message.success('操作成功')
                        fetchTasks()
                    } catch (e) {
                        message.error('操作失败')
                    }
                }

                // 失败、部分成功或已取消: 重试
                if (['失败', '部分成功', '已取消'].includes(record.status_name)) {
                    return (
                        <Button type="link" size="small" onClick={() => handleAction('retry')}>重试</Button>
                    )
                }

                // 进行中: 暂停/取消
                if (record.status_name === '进行中') {
                    return (
                        <Space size={2}>
                            <Button type="link" size="small" onClick={() => handleAction('pause')}>暂停</Button>
                            <Button type="link" danger size="small" onClick={() => handleAction('cancel')}>取消</Button>
                        </Space>
                    )
                }
                // 已暂停: 恢复/取消
                if (record.status_name === '已暂停') {
                    return (
                        <Space size={2}>
                            <Button type="link" size="small" onClick={() => handleAction('resume')}>恢复</Button>
                            <Button type="link" danger size="small" onClick={() => handleAction('cancel')}>取消</Button>
                        </Space>
                    )
                }
                return '-'
            }
        },
        {
            title: '时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 160,
            render: (text) => new Date(text).toLocaleString()
        }
    ]

    // 账户映射
    const getAccountName = (id) => accounts.find(a => a.id === id)?.name || id

    return (
        <div>
            <div className="page-header">
                <h2>网盘互传</h2>
            </div>

            <Card title="创建任务">
                <Space direction="vertical" style={{ width: '100%' }} size="large">
                    {/* 1. 选择源和目标 */}
                    <Space split={<Divider type="vertical" />}>
                        <div>
                            <span style={{ marginRight: 8 }}>从：</span>
                            <Select
                                style={{ width: 200 }}
                                placeholder="选择源网盘"
                                value={sourceAccount}
                                onChange={(val) => {
                                    setSourceAccount(val)
                                    setTargetAccounts([]) // 切换源账号时清空目标账号
                                    setTargetPaths({})    // 切换源账号时清空目标路径，防止残留
                                    setResetKey(prev => prev + 1) // 强行触发 FolderPicker 重置
                                }}
                            >
                                {accounts.map(a => (
                                    <Option key={a.id} value={a.id}>{a.name} ({a.type_name})</Option>
                                ))}
                            </Select>
                        </div>
                        <ArrowRightOutlined />
                        <div>
                            <span style={{ marginRight: 8 }}>到：</span>
                            <Select
                                mode="multiple"
                                style={{ width: 300 }}
                                placeholder="选择目标网盘（可多选）"
                                value={targetAccounts}
                                onChange={setTargetAccounts}
                            >
                                {accounts
                                    .filter(a => a.id !== sourceAccount)  // 排除源网盘
                                    .map(a => (
                                        <Option key={a.id} value={a.id}>{a.name} ({a.type_name})</Option>
                                    ))
                                }
                            </Select>
                        </div>
                    </Space>

                    {/* 2. 选择文件 */}
                    {sourceAccount && (
                        <Card type="inner" title="选择文件" size="small">
                            {/* 搜索框 */}
                            <div style={{ marginBottom: 16 }}>
                                <Space>
                                    <Input.Search
                                        placeholder="全盘搜索文件..."
                                        value={searchKeyword}
                                        onChange={e => setSearchKeyword(e.target.value)}
                                        onSearch={handleSearch}
                                        loading={isSearching}
                                        style={{ width: 300 }}
                                        enterButton={<SearchOutlined />}
                                    />
                                    {searchMode && (
                                        <Button onClick={clearSearch}>返回目录</Button>
                                    )}
                                </Space>
                            </div>

                            {/* 搜索模式显示搜索结果 */}
                            {searchMode ? (
                                <>
                                    <div style={{ marginBottom: 8, color: '#666' }}>
                                        搜索结果: {searchResults.length} 个文件
                                    </div>
                                    <Table
                                        rowSelection={{
                                            type: 'radio',
                                            onChange: (_, rows) => setSelectedFile(rows[0]),
                                            getCheckboxProps: (record) => ({ disabled: record.disabled })
                                        }}
                                        columns={columns}
                                        dataSource={searchResults}
                                        rowKey="fid"
                                        loading={isSearching}
                                        pagination={{ pageSize: 10 }}
                                        size="small"
                                        scroll={{ y: 300 }}
                                    />
                                </>
                            ) : (
                                <>
                                    <Breadcrumb
                                        style={{ marginBottom: 16 }}
                                        items={currentPath.map((item, index) => ({
                                            key: item.fid,
                                            title: <a onClick={() => handleBreadcrumbClick(index)}>{item.name}</a>
                                        }))}
                                    />

                                    <Table
                                        rowSelection={{
                                            type: 'radio',
                                            onChange: (_, rows) => setSelectedFile(rows[0]),
                                            getCheckboxProps: (record) => ({ disabled: record.disabled })
                                        }}
                                        columns={columns}
                                        dataSource={files}
                                        rowKey="fid"
                                        loading={loadingFiles}
                                        pagination={{ pageSize: 5 }}
                                        size="small"
                                        scroll={{ y: 240 }}
                                    />
                                </>
                            )}
                        </Card>
                    )}

                    {/* 3. 目标存储路径（每个目标独立配置） */}
                    {targetAccounts.length > 0 && (
                        <Card type="inner" title="目标存储路径" size="small">
                            {targetAccounts.map(accountId => {
                                const account = accounts.find(a => a.id === accountId)
                                return (
                                    <div key={`${accountId}-${resetKey}`} style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 12 }}>
                                        <span style={{ minWidth: 120 }}>{account?.name}：</span>
                                        <div style={{ flex: 1, maxWidth: 350 }}>
                                            <FolderPicker
                                                accountId={accountId}
                                                value={targetPaths[accountId] ? [targetPaths[accountId]] : []} // 这里简化处理，Cascader 需要数组
                                                placeholder={`选择目录 (默认: /)`}
                                                onChange={(info) => setTargetPaths(prev => ({
                                                    ...prev,
                                                    [accountId]: info.path
                                                }))}
                                            />
                                        </div>
                                    </div>
                                )
                            })}
                        </Card>
                    )}

                    {/* 4. 开始传输按钮 */}
                    <Space align="center">
                        <Button
                            type="primary"
                            icon={<SwapOutlined />}
                            onClick={handleTransfer}
                            loading={submitting}
                            disabled={!selectedFile || targetAccounts.length === 0}
                        >
                            开始传输
                        </Button>
                    </Space>
                </Space>
            </Card>

            <Divider />

            <Card title="传输任务" extra={<Button icon={<ReloadOutlined />} onClick={fetchTasks} type="text" />}>
                <Table
                    columns={enhancedTaskColumns}
                    dataSource={tasks}
                    rowKey="id"
                    loading={loadingTasks}
                    size="small"
                    pagination={{
                        current: pagination.current,
                        pageSize: pagination.pageSize,
                        total: pagination.total,
                        showSizeChanger: true,
                        showTotal: (total) => `共 ${total} 条任务`
                    }}
                    onChange={(p) => fetchTasks(true, p.current, p.pageSize)}
                    childrenColumnName="__ignored_children"
                    expandable={{
                        expandedRowRender: (record) => {
                            // 第一层到第二层：主任务(is_master=1) -> 显示 各个目标账户的父任务
                            if (record.is_master === 1 && record.target_tasks && record.target_tasks.length > 0) {
                                return (
                                    <Table
                                        columns={[
                                            {
                                                title: '目标账户',
                                                key: 'target',
                                                width: 180,
                                                render: (_, r) => {
                                                    const typeEmoji = { 0: '🌟', 1: '💡', 2: '☁️', 4: '⚡', 3: '🐿️' }
                                                    return <span style={{ fontWeight: 500 }}>{typeEmoji[r.target_account_type]} {r.target_account_name}</span>
                                                }
                                            },
                                            {
                                                title: '目标路径',
                                                dataIndex: 'target_path',
                                                key: 'target_path',
                                                ellipsis: true,
                                                width: 200,
                                                render: (t) => <span style={{ color: '#888', fontSize: 12 }}>{(t === '0' || !t) ? '/' : t}</span>
                                            },
                                            {
                                                title: '完成度',
                                                key: 'progress',
                                                width: 120,
                                                render: (_, r) => <span style={{ color: '#444' }}>{r.completed_files || 0} / {r.total_files || 0}</span>
                                            },
                                            {
                                                title: '状态',
                                                key: 'status',
                                                width: 100,
                                                render: (_, r) => {
                                                    const colors = { '待处理': 'default', '进行中': 'processing', '成功': 'success', '失败': 'error', '部分成功': 'warning', '已取消': 'default' }
                                                    return <Tag color={colors[r.status_name]} style={{ borderRadius: 10, fontSize: 11 }}>{r.status_name}</Tag>
                                                }
                                            },
                                            {
                                                title: '进展',
                                                key: 'result',
                                                ellipsis: true,
                                                render: (_, r) => <span style={{ color: '#666', fontSize: 12 }}>{r.current_step || '-'}</span>
                                            },
                                            {
                                                title: '操作',
                                                key: 'action',
                                                width: 120,
                                                render: (_, r) => {
                                                    const handleTargetAction = async (action) => {
                                                        try {
                                                            await api.post(`/cross-transfer/${action}/${r.id}`)
                                                            message.success('操作成功')
                                                            fetchTasks()
                                                        } catch (e) {
                                                            message.error('操作失败')
                                                        }
                                                    }
                                                    return (
                                                        <Space size={0}>
                                                            {r.status_name === '进行中' && (
                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleTargetAction('pause')}>暂停</Button>
                                                            )}
                                                            {r.status_name === '已暂停' && (
                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleTargetAction('resume')}>恢复</Button>
                                                            )}
                                                            {['失败', '部分成功', '已取消'].includes(r.status_name) && (
                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleTargetAction('retry')}>重试</Button>
                                                            )}
                                                            {(['进行中', '待处理', '已暂停', '失败', '部分成功'].includes(r.status_name)) && (
                                                                <Button type="link" danger size="small" style={{ fontSize: 11, paddingLeft: 8 }} onClick={() => handleTargetAction('cancel')}>取消</Button>
                                                            )}
                                                        </Space>
                                                    )
                                                }
                                            }
                                        ]}
                                        dataSource={record.target_tasks}
                                        rowKey="id"
                                        size="small"
                                        pagination={false}
                                        childrenColumnName="__ignored_children"
                                        style={{ margin: '4px 0 8px 32px', border: '1px solid #f0f0f0', borderRadius: 8, background: '#fcfcfc', overflow: 'hidden' }}
                                        expandable={{
                                            expandedRowRender: (targetTask) => {
                                                const handleFileAction = async (childId, action) => {
                                                    try {
                                                        await api.post(`/cross-transfer/${action}/${childId}`)
                                                        message.success('操作成功')
                                                        fetchTasks()
                                                    } catch (e) {
                                                        message.error('操作失败')
                                                    }
                                                }

                                                if (!targetTask.children || targetTask.children.length === 0) {
                                                    return <div style={{ padding: '12px 48px', color: '#999', background: '#fff', fontSize: 12 }}>暂无子文件任务</div>
                                                }
                                                return (
                                                    <div style={{ margin: '8px 0 8px 48px', border: '1px solid #f0f0f0', borderRadius: 8, background: '#fcfcfc', overflow: 'hidden' }}>
                                                        <div style={{ background: '#fafafa', padding: '8px 16px', borderBottom: '1px solid #f0f0f0', fontSize: 12, fontWeight: 500, color: '#666' }}>子文件传输状态</div>
                                                        <div style={{ padding: '8px 0', background: '#fff' }}>
                                                            {targetTask.children.map((child) => (
                                                                <div key={child.id} style={{
                                                                    display: 'flex',
                                                                    alignItems: 'center',
                                                                    padding: '6px 16px',
                                                                    borderBottom: '1px solid #f9f9f9',
                                                                    fontSize: 12
                                                                }}>
                                                                    <div style={{ width: 300, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#444' }}>
                                                                        📄 {child.source_file_name || child.source_fid || '未命名文件'}
                                                                    </div>
                                                                    <div style={{ flex: 1, padding: '0 12px', color: '#999', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                                        {(!child.target_path || child.target_path === '0') ? '/' : child.target_path}
                                                                    </div>
                                                                    <div style={{ width: 80, textAlign: 'center' }}>
                                                                        {(() => {
                                                                            const colors = { '待处理': 'default', '进行中': 'processing', '成功': 'success', '失败': 'error' }
                                                                            return <Tag color={colors[child.status_name]} style={{ fontSize: 10, margin: 0, zoom: 0.85 }}>{child.status_name}</Tag>
                                                                        })()}
                                                                    </div>
                                                                    <div style={{ width: 150, paddingLeft: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                                        {child.status_name === '失败' ? (
                                                                            <span style={{ color: '#ff4d4f', fontSize: 11 }} title={child.error_message}>{child.error_message}</span>
                                                                        ) : child.status_name === '成功' ? (
                                                                            <span style={{ color: '#52c41a' }}>✓ 已完成</span>
                                                                        ) : (
                                                                            <span style={{ color: '#999', fontSize: 11 }}>{child.current_step || '-'}</span>
                                                                        )}
                                                                    </div>
                                                                    <div style={{ width: 140, paddingLeft: 8, color: '#999', fontSize: 11 }}>
                                                                        {(['成功', '失败'].includes(child.status_name)) && child.completed_at ? new Date(child.completed_at).toLocaleString() : '-'}
                                                                    </div>
                                                                    <div style={{ width: 110, textAlign: 'right' }}>
                                                                        <Space size={0}>
                                                                            {child.status_name === '进行中' && (
                                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleFileAction(child.id, 'pause')}>暂停</Button>
                                                                            )}
                                                                            {child.status_name === '已暂停' && (
                                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleFileAction(child.id, 'resume')}>恢复</Button>
                                                                            )}
                                                                            {['失败', '已取消'].includes(child.status_name) && (
                                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleFileAction(child.id, 'retry')}>重试</Button>
                                                                            )}
                                                                            {(child.status_name === '进行中' || child.status_name === '失败' || child.status_name === '已暂停' || child.status_name === '待处理') && (
                                                                                <Button type="link" danger size="small" style={{ fontSize: 11, paddingLeft: 8 }} onClick={() => handleFileAction(child.id, 'cancel')}>取消</Button>
                                                                            )}
                                                                        </Space>
                                                                    </div>
                                                                </div>
                                                            ))}
                                                        </div>
                                                    </div>
                                                )
                                            },
                                            rowExpandable: (targetTask) => targetTask.children && targetTask.children.length > 0
                                        }}
                                    />
                                )
                            }

                            // 两层结构：单目标文件夹任务 -> 直接显示子任务列表
                            if (record.is_folder === 1 && record.children && record.children.length > 0) {
                                const handleFileAction = async (childId, action) => {
                                    try {
                                        await api.post(`/cross-transfer/${action}/${childId}`)
                                        message.success('操作成功')
                                        fetchTasks()
                                    } catch (e) {
                                        message.error('操作失败')
                                    }
                                }

                                return (
                                    <div style={{ margin: '4px 0 8px 32px', border: '1px solid #f0f0f0', borderRadius: 8, background: '#fcfcfc', overflow: 'hidden' }}>
                                        <div style={{ background: '#fafafa', padding: '8px 16px', borderBottom: '1px solid #f0f0f0', fontSize: 12, fontWeight: 500, color: '#666' }}>子文件传输状态</div>
                                        <div style={{ padding: '8px 0', background: '#fff' }}>
                                            {record.children.map((child) => (
                                                <div key={child.id} style={{
                                                    display: 'flex',
                                                    alignItems: 'center',
                                                    padding: '8px 16px',
                                                    borderBottom: '1px solid #f9f9f9',
                                                    fontSize: 12
                                                }}>
                                                    <div style={{ width: 300, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#444' }}>
                                                        📄 {child.source_file_name || child.source_fid || '未命名文件'}
                                                    </div>
                                                    <div style={{ flex: 1, padding: '0 12px', color: '#999', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                        {(!child.target_path || child.target_path === '0') ? '/' : child.target_path}
                                                    </div>
                                                    <div style={{ width: 80, textAlign: 'center' }}>
                                                        {(() => {
                                                            const colors = { '待处理': 'default', '进行中': 'processing', '成功': 'success', '失败': 'error' }
                                                            return <Tag color={colors[child.status_name]} style={{ fontSize: 10, margin: 0, zoom: 0.9 }}>{child.status_name}</Tag>
                                                        })()}
                                                    </div>
                                                    <div style={{ width: 150, paddingLeft: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                        {child.status_name === '失败' ? (
                                                            <span style={{ color: '#ff4d4f', fontSize: 11 }} title={child.error_message}>{child.error_message}</span>
                                                        ) : child.status_name === '成功' ? (
                                                            <span style={{ color: '#52c41a' }}>✓ 已完成</span>
                                                        ) : (
                                                            <span style={{ color: '#666', fontSize: 11 }}>{child.current_step || '-'}</span>
                                                        )}
                                                    </div>
                                                    <div style={{ width: 140, paddingLeft: 8, color: '#999', fontSize: 11 }}>
                                                        {(['成功', '失败'].includes(child.status_name)) && child.completed_at ? new Date(child.completed_at).toLocaleString() : '-'}
                                                    </div>
                                                    <div style={{ width: 110, textAlign: 'right' }}>
                                                        <Space size={0}>
                                                            {child.status_name === '进行中' && (
                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleFileAction(child.id, 'pause')}>暂停</Button>
                                                            )}
                                                            {child.status_name === '已暂停' && (
                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleFileAction(child.id, 'resume')}>恢复</Button>
                                                            )}
                                                            {['失败', '已取消'].includes(child.status_name) && (
                                                                <Button type="link" size="small" style={{ fontSize: 11, padding: 0 }} onClick={() => handleFileAction(child.id, 'retry')}>重试</Button>
                                                            )}
                                                            {(child.status_name === '进行中' || child.status_name === '失败' || child.status_name === '已暂停' || child.status_name === '待处理') && (
                                                                <Button type="link" danger size="small" style={{ fontSize: 11, paddingLeft: 8 }} onClick={() => handleFileAction(child.id, 'cancel')}>取消</Button>
                                                            )}
                                                        </Space>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )
                            }

                            return <div style={{ padding: '8px 48px', color: '#ccc', fontSize: 12 }}>暂无更多细节</div>
                        },
                        rowExpandable: (record) =>
                            (record.is_master === 1 && record.target_tasks && record.target_tasks.length > 0) ||
                            (record.is_folder === 1 && record.children && record.children.length > 0),
                        indentSize: 0,
                    }}

                />
            </Card >

            {/* 目标路径选择弹窗 */}
            <Modal
                title="选择存储路径"
                open={targetBrowserVisible}
                onOk={() => {
                    // 构建路径字符串 (忽略根目录)
                    const path = targetPathStack.length > 1
                        ? '/' + targetPathStack.slice(1).map(p => p.name).join('/') + '/'
                        : '/'
                    setTargetPath(path)
                    setTargetBrowserVisible(false)
                }}
                onCancel={() => setTargetBrowserVisible(false)}
                width={600}
            >
                <Breadcrumb
                    style={{ marginBottom: 16 }}
                    items={targetPathStack.map((item, index) => ({
                        key: item.fid,
                        title: (
                            <a onClick={() => {
                                const newPath = targetPathStack.slice(0, index + 1)
                                setTargetPathStack(newPath)
                                fetchTargetFiles(item.fid)
                            }}>{item.name}</a>
                        )
                    }))}
                />
                <Table
                    columns={[
                        {
                            title: '文件夹',
                            dataIndex: 'name',
                            key: 'name',
                            render: (text, record) => (
                                <Space>
                                    <FolderOutlined style={{ color: '#faad14' }} />
                                    <a onClick={() => {
                                        const newPath = [...targetPathStack, { name: record.name, fid: record.fid }]
                                        setTargetPathStack(newPath)
                                        fetchTargetFiles(record.fid)
                                    }}>{text}</a>
                                </Space>
                            )
                        }
                    ]}
                    dataSource={targetFiles.filter(f => f.is_dir)}
                    rowKey="fid"
                    loading={loadingTargetFiles}
                    pagination={{ pageSize: 5 }}
                    size="small"
                    scroll={{ y: 300 }}
                />
            </Modal >
        </div >
    )
}
