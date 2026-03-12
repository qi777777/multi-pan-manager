import { useState, useEffect } from 'react'
import { Card, Input, Button, Select, Space, Table, Tag, message, Alert, Divider, Switch, Tabs } from 'antd'
import { SwapOutlined, LinkOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, ShareAltOutlined } from '@ant-design/icons'
import { accountApi, transferApi, DISK_TYPES } from '../services/api'
import FolderPicker from '../components/FolderPicker'

const { TextArea } = Input

const URL_PATTERNS = {
    'pan.quark.cn': 0,
    'alipan.com': 1,
    'aliyundrive.com': 1,
    'pan.baidu.com': 2,
    'drive.uc.cn': 3,
    'fast.uc.cn': 3,
    'pan.xunlei.com': 4
}

export default function TransferPage() {
    const [accounts, setAccounts] = useState([])
    const [url, setUrl] = useState('')
    const [code, setCode] = useState('')
    const [targetAccounts, setTargetAccounts] = useState([])
    const [targetPaths, setTargetPaths] = useState({})
    const [needShare, setNeedShare] = useState({})  // 每个账户的分享开关
    const [sharePeriods, setSharePeriods] = useState({})  // 每个账户的分享时效
    const [resetKey, setResetKey] = useState(0)
    const [loading, setLoading] = useState(false)
    const [tasks, setTasks] = useState([])
    const [tasksLoading, setTasksLoading] = useState(false)
    const [enableCrossPan, setEnableCrossPan] = useState(true)  // 跨网盘互传开关
    const [pagination, setPagination] = useState({
        current: 1,
        pageSize: 10,
        total: 0
    })

    // 批量转存（多链接）
    const [batchUrls, setBatchUrls] = useState('')      // 每行一个链接
    const [batchTransferring, setBatchTransferring] = useState(false)
    const [batchResults, setBatchResults] = useState(null)  // 批量转存结果


    useEffect(() => {
        fetchAccounts()
        fetchTasks(false, 1, pagination.pageSize)
    }, [])

    // 自动刷新：有进行中的任务时每3秒刷新
    useEffect(() => {
        const hasRunning = tasks.some(t => {
            // 父任务是否在执行中
            if (t.status === 0 || t.status === 1) {
                // 如果后端卡在正在分发，我们不再根据 t.status盲目判定 running
                if (t.chain_status && t.chain_status.includes('分发已结束')) {
                    // fall through
                } else {
                    return true
                }
            }
            if (t.children && t.children.length > 0) {
                return t.children.some(c => {
                    // 只要 TransferTask 自身还在进行中(等待互传或正在创建分享)，就必须继续轮询
                    if (c.status === 0 || c.status === 1) return true

                    if (c.cross_parent) {
                        return c.cross_parent.status === 0 || c.cross_parent.status === 1
                    }
                    if (c.cross_tasks && c.cross_tasks.length > 0) {
                        return c.cross_tasks.some(ct => ct.status === 0 || ct.status === 1)
                    }
                    return false
                })
            }
            return false
        })
        if (!hasRunning) return
        const timer = setInterval(() => fetchTasks(true, pagination.current, pagination.pageSize), 3000)
        return () => clearInterval(timer)
    }, [tasks, pagination.current, pagination.pageSize])

    const fetchAccounts = async () => {
        try {
            const { data } = await accountApi.getList()
            setAccounts(data.filter(a => a.status === 1))
        } catch (error) {
            message.error('获取账户列表失败')
        }
    }

    const fetchTasks = async (silent = false, page = pagination.current, pageSize = pagination.pageSize) => {
        if (!silent) setTasksLoading(true)
        try {
            const skip = (page - 1) * pageSize
            const { data } = await transferApi.getTasks(skip, pageSize)
            // 兼容旧版(Array)和新版({total, items})
            if (Array.isArray(data)) {
                setTasks(data)
                setPagination(prev => ({ ...prev, total: data.length }))
            } else {
                setTasks(data.items || [])
                setPagination(prev => ({ ...prev, current: page, pageSize, total: data.total || 0 }))
            }
        } catch (error) {
            console.error('获取任务列表失败')
        } finally {
            if (!silent) setTasksLoading(false)
        }
    }



    const detectDiskType = (url) => {
        for (const [pattern, type] of Object.entries(URL_PATTERNS)) {
            if (url.includes(pattern)) return type
        }
        return -1
    }

    const getLinksDiskTypes = (urlsStr) => {
        const lines = urlsStr.split('\n').map(l => l.trim()).filter(Boolean)
        const types = new Set()
        lines.forEach(line => {
            const type = detectDiskType(line)
            if (type !== -1) types.add(type)
        })
        return Array.from(types)
    }

    const handleBatchTransfer = async () => {
        const lines = batchUrls.split('\n').map(l => l.trim()).filter(Boolean)
        if (lines.length === 0) {
            message.warning('请输入至少一个分享链接')
            return
        }
        if (targetAccounts.length === 0) {
            message.warning('请至少选择一个目标网盘')
            return
        }

        const linkTypes = getLinksDiskTypes(batchUrls)
        const selectedAccountTypes = targetAccounts.map(id => accounts.find(a => a.id === id)?.type)

        if (!enableCrossPan) {
            const missingTypes = linkTypes.filter(lt => !selectedAccountTypes.includes(lt))
            if (missingTypes.length > 0) {
                const missingNames = missingTypes.map(t => DISK_TYPES[t]?.name).join('、')
                message.error(`已关闭跨网盘互传，但未选择 ${missingNames} 账号，相关链接将无法转存`)
                return
            }
        }

        setBatchTransferring(true)
        setBatchResults(null)
        const results = []
        const targets = targetAccounts.map(id => ({
            account_id: id,
            path: targetPaths[id] || '/',
            need_share: needShare[id] !== undefined ? needShare[id] : true,
            expired_type: sharePeriods[id] !== undefined ? sharePeriods[id] : 1
        }))
        for (const lineUrl of lines) {
            try {
                const { data } = await transferApi.execute({
                    url: lineUrl,
                    code: '',  // 批量模式下无法逐个输入提取码
                    targets,
                    expired_type: 1,
                    enable_cross_pan: enableCrossPan
                })
                results.push({ url: lineUrl, success: true, message: data.message || '已提交' })
            } catch (e) {
                results.push({ url: lineUrl, success: false, message: e.response?.data?.detail || '失败' })
            }
        }
        setBatchResults(results)
        const successCount = results.filter(r => r.success).length
        if (successCount === results.length) {
            message.success(`${successCount} 个链接已全部提交转存`)
        } else {
            message.warning(`${successCount} 成功 / ${results.length - successCount} 失败`)
        }
        setBatchTransferring(false)
        setTargetAccounts([]) // 清空选择，防止误操作
        fetchTasks()
    }

    const handleTransfer = async () => {
        if (!url.trim()) {
            message.warning('请输入分享链接')
            return
        }
        if (targetAccounts.length === 0) {
            message.warning('请至少选择一个目标网盘')
            return
        }

        const diskType = detectDiskType(url)
        if (diskType === -1) {
            message.error('无法识别的分享链接')
            return
        }

        // 验证是否包含同类型网盘（作为网关）
        const hasGateway = targetAccounts.some(id => {
            const acc = accounts.find(a => a.id === id)
            return acc && acc.type === diskType
        })

        if (!hasGateway) {
            message.error(`所选网盘中必须包含一个 ${DISK_TYPES[diskType]?.name || '同类型'} 账号作为转存中转`)
            return
        }

        setLoading(true)
        try {
            // 构建 targets 列表（使用用户选择的路径）
            const targets = targetAccounts.map(id => ({
                account_id: id,
                path: targetPaths[id] || '/',
                need_share: needShare[id] !== undefined ? needShare[id] : true,
                expired_type: sharePeriods[id] !== undefined ? sharePeriods[id] : 1
            }))

            const { data } = await transferApi.execute({
                url: url.trim(),
                code: code.trim(),
                targets: targets,
                expired_type: 1,
                enable_cross_pan: true // 快速转存保持原有逻辑，默认开启互传以保证通过网关账户分发
            })
            message.success(data.message || `任务已提交，任务ID: ${data.task_id}`)
            setUrl('')
            setCode('')
            fetchTasks()
        } catch (error) {
            message.error(error.response?.data?.detail || '转存失败')
        } finally {
            setLoading(false)
        }
    }

    const detectedType = url ? detectDiskType(url) : -1

    const columns = [
        {
            title: '资源标题',
            dataIndex: 'result_title',
            key: 'result_title',
            ellipsis: true,
            render: (text, record) => {
                const titleStr = text || record.source_url.substring(0, 50) + '...'
                return (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        {titleStr}
                        {record.result_share_url && (
                            <Button
                                type="link"
                                size="small"
                                icon={<ShareAltOutlined />}
                                onClick={(e) => {
                                    e.stopPropagation()
                                    navigator.clipboard.writeText(record.result_share_url)
                                    message.success('分享链接已复制到剪贴板')
                                }}
                            >
                                复制分享
                            </Button>
                        )}
                    </div>
                )
            }
        },
        {
            title: '源网盘',
            dataIndex: 'source_type',
            key: 'source_type',
            width: 90,
            render: (type) => (
                <Tag color={DISK_TYPES[type]?.color}>{DISK_TYPES[type]?.name}</Tag>
            )
        },
        {
            title: '目标网盘',
            dataIndex: 'target_account_name',
            key: 'target_account_name',
            width: 120,
            render: (name, record) => (
                <span>
                    {DISK_TYPES[record.target_account_type]?.icon} {name || '-'}
                </span>
            )
        },
        {
            title: '状态',
            dataIndex: 'status',
            key: 'status',
            width: 100,
            render: (status, record) => {
                let displayStatus = status
                if (record.children && record.children.length > 0) {
                    const childStatuses = record.children.map(child => {
                        if (child.cross_parent) return child.cross_parent.status
                        if (child.cross_tasks && child.cross_tasks.length > 0) {
                            if (child.cross_tasks.every(ct => ct.status === 2)) return 2
                            if (child.cross_tasks.some(ct => ct.status === 1 || ct.status === 0)) return 1
                            if (child.cross_tasks.every(ct => ct.status === 3 || ct.status === 6)) return 3
                            return 5
                        }
                        return child.status
                    })

                    const allSuccess = childStatuses.every(s => s === 2)
                    const anyRunning = childStatuses.some(s => s === 1 || s === 0)
                    const allFailed = childStatuses.every(s => s === 3 || s === 6)

                    if (anyRunning) displayStatus = 1
                    else if (allSuccess) displayStatus = 2
                    else if (allFailed) displayStatus = 3
                    else displayStatus = 5 // 部分成功
                }

                const statusMap = {
                    0: { text: '待处理', icon: <LoadingOutlined />, color: 'default' },
                    1: { text: '进行中', icon: <LoadingOutlined spin />, color: 'processing' },
                    2: { text: '成功', icon: <CheckCircleOutlined />, color: 'success' },
                    3: { text: '失败', icon: <CloseCircleOutlined />, color: 'error' },
                    5: { text: '部分成功', icon: <CheckCircleOutlined />, color: 'warning' },
                    6: { text: '已取消', icon: <CloseCircleOutlined />, color: 'default' }
                }
                const s = statusMap[displayStatus] || statusMap[0]

                let chainText = record.chain_status && !record.chain_status.startsWith('need_share:') ? record.chain_status : ''
                if (displayStatus !== 1 && chainText.includes('正在分发')) {
                    chainText = '分发已结束'
                }

                return (
                    <span>
                        <Tag icon={s.icon} color={s.color}>{s.text}</Tag>
                        {chainText && (
                            <div style={{ fontSize: 12, color: '#888', marginTop: 2 }}>{chainText}</div>
                        )}
                    </span>
                )
            }
        },
        {
            title: '创建时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 170
        }
    ]

    // 展开子任务渲染（复刻互传页面效果）
    const expandedRowRender = (record) => {
        if (!record.children || record.children.length === 0) return null
        return (
            <div style={{ padding: '4px 32px 8px 32px' }}>
                <Table
                    columns={[
                        {
                            title: '目标账户',
                            key: 'target',
                            width: 250,
                            render: (_, r) => {
                                const typeEmoji = { 0: '🌟', 1: '💡', 2: '☁️', 4: '⚡', 3: '🐿️' }
                                return (
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                        <span style={{ fontWeight: 500 }}>{typeEmoji[r.target_account_type]} {r.target_account_name || '-'}</span>
                                        {r.result_share_url && (
                                            <Button
                                                type="link"
                                                size="small"
                                                icon={<ShareAltOutlined />}
                                                onClick={(e) => {
                                                    e.stopPropagation()
                                                    navigator.clipboard.writeText(r.result_share_url)
                                                    message.success('分享链接已复制到剪贴板')
                                                }}
                                                style={{ padding: 0, height: 'auto', fontSize: 12 }}
                                            >
                                                提取分享
                                            </Button>
                                        )}
                                    </div>
                                )
                            }
                        },
                        {
                            title: '完成度',
                            key: 'progress',
                            width: 120,
                            render: (_, r) => {
                                if (r.cross_parent) {
                                    return <span style={{ color: '#444' }}>{r.cross_parent.completed_files || 0} / {r.cross_parent.total_files || 0}</span>
                                }
                                return <span style={{ color: '#ccc' }}>-</span>
                            }
                        },
                        {
                            title: '状态',
                            key: 'status',
                            width: 100,
                            render: (_, r) => {
                                let displayStatus = r.status
                                let displayStatusName = ''

                                if (r.cross_parent) {
                                    displayStatus = r.cross_parent.status
                                    displayStatusName = r.cross_parent.status_name
                                } else if (r.cross_tasks && r.cross_tasks.length > 0) {
                                    const allSuccess = r.cross_tasks.every(ct => ct.status === 2)
                                    const anyRunning = r.cross_tasks.some(ct => ct.status === 1 || ct.status === 0)
                                    const allFailed = r.cross_tasks.every(ct => ct.status === 3 || ct.status === 6)
                                    if (allSuccess) displayStatus = 2
                                    else if (anyRunning) displayStatus = 1
                                    else if (allFailed) displayStatus = 3
                                    else displayStatus = 5
                                }

                                const colors = { 0: 'default', 1: 'processing', 2: 'success', 3: 'error', 4: 'warning', 5: 'warning', 6: 'default' }
                                const texts = { 0: '待处理', 1: '进行中', 2: '成功', 3: '失败', 4: '已暂停', 5: '部分成功', 6: '已取消' }

                                return <Tag color={colors[displayStatus]} style={{ borderRadius: 10, fontSize: 11 }}>{displayStatusName || texts[displayStatus] || '未知'}</Tag>
                            }
                        },
                        {
                            title: '说明',
                            key: 'info',
                            ellipsis: true,
                            render: (_, r) => {
                                let statusText = r.chain_status && !r.chain_status.startsWith('need_share:') ? r.chain_status : r.error_message || '-'
                                let isDone = false
                                if ([2, 3, 5, 6].includes(r.status)) isDone = true  // 任务本身已是终态
                                else if (r.cross_parent && [2, 3, 5, 6].includes(r.cross_parent.status)) isDone = true
                                else if (r.cross_tasks && r.cross_tasks.length > 0 && r.cross_tasks.every(ct => [2, 3, 5, 6].includes(ct.status))) isDone = true

                                // 如果子任务底层互传已结束，但总任务(由于在生成分享)依然是[进行中]，我们提示正在生成分享
                                if (isDone && r.status === 1 && (statusText.includes('正在互传') || statusText.includes('互传处理中') || statusText.includes('互传进度'))) {
                                    statusText = '互传已完成，正在生成分享...'
                                }
                                return <span style={{ color: '#666', fontSize: 12 }}>{statusText}</span>
                            }
                        }
                    ]}
                    dataSource={record.children}
                    rowKey="id"
                    size="small"
                    pagination={false}
                    style={{ border: '1px solid #f0f0f0', borderRadius: 8, background: '#fcfcfc', overflow: 'hidden', width: '100%' }}
                    expandable={{
                        expandedRowRender: (childTask) => {
                            if (!childTask.cross_tasks || childTask.cross_tasks.length === 0) {
                                return <div style={{ padding: '12px 48px', color: '#999', background: '#fff', fontSize: 12 }}>暂无子文件任务或暂未解析传输目录</div>
                            }
                            return (
                                <div style={{ margin: '8px 0 8px 48px', border: '1px solid #f0f0f0', borderRadius: 8, background: '#fcfcfc', overflow: 'hidden' }}>
                                    <div style={{ background: '#fafafa', padding: '8px 16px', borderBottom: '1px solid #f0f0f0', fontSize: 12, fontWeight: 500, color: '#666' }}>子文件传输状态</div>
                                    <div style={{ padding: '8px 0', background: '#fff' }}>
                                        {childTask.cross_tasks.map((ct) => (
                                            <div key={ct.id} style={{
                                                display: 'flex',
                                                alignItems: 'center',
                                                padding: '6px 16px',
                                                borderBottom: '1px solid #f9f9f9',
                                                fontSize: 12
                                            }}>
                                                <div style={{ width: 300, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#444' }}>
                                                    📄 {ct.source_file_name || '未命名文件'}
                                                </div>
                                                <div style={{ flex: 1, padding: '0 12px', color: '#999', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {(!ct.target_path || ct.target_path === '0') ? '/' : ct.target_path}
                                                </div>
                                                <div style={{ width: 80, textAlign: 'center' }}>
                                                    {(() => {
                                                        const colors = { '待处理': 'default', '进行中': 'processing', '成功': 'success', '失败': 'error', '已取消': 'default' }
                                                        return <Tag color={colors[ct.status_name]} style={{ fontSize: 10, margin: 0, zoom: 0.85 }}>{ct.status_name}</Tag>
                                                    })()}
                                                </div>
                                                <div style={{ width: 150, paddingLeft: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {ct.status_name === '失败' ? (
                                                        <span style={{ color: '#ff4d4f', fontSize: 11 }} title={ct.error_message}>{ct.error_message}</span>
                                                    ) : ct.status_name === '成功' ? (
                                                        <span style={{ color: '#52c41a' }}>✓ 已完成</span>
                                                    ) : (
                                                        <span style={{ color: '#999', fontSize: 11 }}>{ct.current_step || '-'}</span>
                                                    )}
                                                </div>
                                                <div style={{ width: 140, paddingLeft: 8, color: '#999', fontSize: 11 }}>
                                                    {(['成功', '失败'].includes(ct.status_name)) && ct.completed_at ? new Date(ct.completed_at).toLocaleString() : '-'}
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )
                        },
                        rowExpandable: (childTask) => true
                    }}
                />
            </div>
        )
    }
    return (
        <div>
            <div className="page-header">
                <h2>转存工具</h2>
            </div>

            <Card>
                <Tabs
                    defaultActiveKey="quick"
                    items={[
                        {
                            key: 'quick',
                            label: <span><SwapOutlined /> 快速转存</span>,
                            children: (
                                <Space direction="vertical" style={{ width: '100%' }} size="middle">
                                    <div>
                                        <label style={{ display: 'block', marginBottom: 8 }}>分享链接：</label>
                                        <TextArea
                                            rows={3}
                                            placeholder="请粘贴分享链接，支持夸克、阿里、百度、UC、迅雷"
                                            value={url}
                                            onChange={(e) => setUrl(e.target.value)}
                                        />
                                        {detectedType !== -1 && (
                                            <Alert
                                                message={`检测到 ${DISK_TYPES[detectedType]?.icon} ${DISK_TYPES[detectedType]?.name} 链接`}
                                                type="info"
                                                showIcon
                                                style={{ marginTop: 8 }}
                                            />
                                        )}
                                    </div>

                                    <div>
                                        <label style={{ display: 'block', marginBottom: 8 }}>选择目标网盘：</label>
                                        <Space direction="vertical" style={{ width: '100%' }}>
                                            {accounts.map(account => (
                                                <div key={account.id} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', borderBottom: '1px solid #f0f0f0' }}>
                                                    <Switch
                                                        checked={targetAccounts.includes(account.id)}
                                                        onChange={(checked) => {
                                                            if (checked) {
                                                                setTargetAccounts(prev => [...prev, account.id])
                                                            } else {
                                                                setTargetAccounts(prev => prev.filter(id => id !== account.id))
                                                            }
                                                        }}
                                                        size="small"
                                                    />
                                                    <span style={{ minWidth: 120 }}>{DISK_TYPES[account.type]?.icon} {account.name}</span>
                                                    {targetAccounts.includes(account.id) && (
                                                        <>
                                                            <div style={{ flex: 1, maxWidth: 350 }}>
                                                                <FolderPicker
                                                                    key={`${account.id}-${resetKey}`}
                                                                    accountId={account.id}
                                                                    placeholder="选择目录（默认根目录）"
                                                                    onChange={(info) => setTargetPaths(prev => ({ ...prev, [account.id]: info?.path || '/' }))}
                                                                />
                                                            </div>
                                                            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                                                <Switch
                                                                    checked={needShare[account.id] !== false}
                                                                    onChange={(v) => setNeedShare(prev => ({ ...prev, [account.id]: v }))}
                                                                    size="small"
                                                                />
                                                                <span style={{ fontSize: 12, color: '#555' }}>转存后分享</span>
                                                                {needShare[account.id] !== false && (
                                                                    <Select
                                                                        size="small"
                                                                        style={{ width: 100 }}
                                                                        value={sharePeriods[account.id] ?? 1}
                                                                        onChange={(v) => setSharePeriods(prev => ({ ...prev, [account.id]: v }))}
                                                                    >
                                                                        <Select.Option value={1}>永久</Select.Option>
                                                                        <Select.Option value={2}>7天</Select.Option>
                                                                        <Select.Option value={3}>1天</Select.Option>
                                                                        <Select.Option value={4}>30天</Select.Option>
                                                                    </Select>
                                                                )}
                                                            </div>
                                                        </>
                                                    )}
                                                </div>
                                            ))}
                                        </Space>
                                    </div>

                                    <Button
                                        type="primary"
                                        icon={<SwapOutlined />}
                                        loading={loading}
                                        onClick={handleTransfer}
                                        size="large"
                                    >
                                        开始转存
                                    </Button>
                                </Space>
                            )
                        },
                        {
                            key: 'batch',
                            label: <span><LinkOutlined /> 批量转存</span>,
                            children: (
                                <Space direction="vertical" style={{ width: '100%' }} size="middle">
                                    <div>
                                        <label style={{ display: 'block', marginBottom: 8 }}>分享链接（每行一个）：</label>
                                        <TextArea
                                            rows={7}
                                            placeholder={`https://pan.quark.cn/s/xxxx\nhttps://pan.baidu.com/s/xxxx\nhttps://pan.xunlei.com/s/xxxx`}
                                            value={batchUrls}
                                            onChange={e => setBatchUrls(e.target.value)}
                                        />
                                    </div>

                                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
                                        <label>开启跨网盘互传：</label>
                                        <Switch
                                            checked={enableCrossPan}
                                            onChange={setEnableCrossPan}
                                            checkedChildren="开启"
                                            unCheckedChildren="关闭"
                                        />
                                        <div style={{ fontSize: 12, color: '#888' }}>
                                            {enableCrossPan ? '支持互传：所有链接均会分发到选中的全部账号' : '仅限同盘：检测链接类型并自动转存到对应的账号'}
                                        </div>
                                    </div>

                                    {!enableCrossPan && batchUrls.trim() && (() => {
                                        const linkTypes = getLinksDiskTypes(batchUrls);
                                        const selectedAccountTypes = targetAccounts.map(id => accounts.find(a => a.id === id)?.type);
                                        const missingTypes = linkTypes.filter(lt => !selectedAccountTypes.includes(lt));
                                        if (missingTypes.length > 0) {
                                            return (
                                                <Alert
                                                    message={`警告：已选择关闭互传，但未选择存储以下链接类型的账户：${missingTypes.map(t => DISK_TYPES[t]?.name).join(', ')}`}
                                                    type="warning"
                                                    showIcon
                                                    style={{ marginBottom: 12 }}
                                                />
                                            );
                                        }
                                        return null;
                                    })()}

                                    <div>
                                        <label style={{ display: 'block', marginBottom: 8 }}>选择目标网盘：</label>
                                        <Space direction="vertical" style={{ width: '100%' }}>
                                            {accounts.map(account => (
                                                <div key={account.id} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', borderBottom: '1px solid #f0f0f0' }}>
                                                    <Switch
                                                        checked={targetAccounts.includes(account.id)}
                                                        onChange={(checked) => {
                                                            if (checked) {
                                                                setTargetAccounts(prev => [...prev, account.id])
                                                            } else {
                                                                setTargetAccounts(prev => prev.filter(id => id !== account.id))
                                                            }
                                                        }}
                                                        size="small"
                                                    />
                                                    <span style={{ minWidth: 120 }}>
                                                        {DISK_TYPES[account.type]?.icon} {account.name}
                                                        {!enableCrossPan && batchUrls.includes(Object.keys(URL_PATTERNS).find(k => URL_PATTERNS[k] === account.type)) && (
                                                            <Tag color="green" style={{ marginLeft: 8 }}>匹配链接</Tag>
                                                        )}
                                                    </span>
                                                    {targetAccounts.includes(account.id) && (
                                                        <>
                                                            <div style={{ flex: 1, maxWidth: 350 }}>
                                                                <FolderPicker
                                                                    key={`batch-${account.id}-${resetKey}`}
                                                                    accountId={account.id}
                                                                    placeholder="选择目录（默认根目录）"
                                                                    onChange={(info) => setTargetPaths(prev => ({ ...prev, [account.id]: info?.path || '/' }))}
                                                                />
                                                            </div>
                                                            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                                                <Switch
                                                                    checked={needShare[account.id] !== false}
                                                                    onChange={(v) => setNeedShare(prev => ({ ...prev, [account.id]: v }))}
                                                                    size="small"
                                                                />
                                                                <span style={{ fontSize: 12, color: '#555' }}>转存后分享</span>
                                                                {needShare[account.id] !== false && (
                                                                    <Select
                                                                        size="small"
                                                                        style={{ width: 100 }}
                                                                        value={sharePeriods[account.id] ?? 1}
                                                                        onChange={(v) => setSharePeriods(prev => ({ ...prev, [account.id]: v }))}
                                                                    >
                                                                        <Select.Option value={1}>永久</Select.Option>
                                                                        <Select.Option value={2}>7天</Select.Option>
                                                                        <Select.Option value={3}>1天</Select.Option>
                                                                        <Select.Option value={4}>30天</Select.Option>
                                                                    </Select>
                                                                )}
                                                            </div>
                                                        </>
                                                    )}
                                                </div>
                                            ))}
                                        </Space>
                                    </div>

                                    <Button
                                        type="primary"
                                        icon={<SwapOutlined />}
                                        loading={batchTransferring}
                                        onClick={handleBatchTransfer}
                                        size="large"
                                    >
                                        批量开始转存 {batchUrls.split('\n').filter(l => l.trim()).length > 0 ? `(${batchUrls.split('\n').filter(l => l.trim()).length} 条)` : ''}
                                    </Button>

                                    {batchResults && (
                                        <div>
                                            <Divider style={{ margin: '8px 0' }} />
                                            {batchResults.map((r, i) => (
                                                <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 6 }}>
                                                    {r.success
                                                        ? <CheckCircleOutlined style={{ color: '#52c41a', marginTop: 2 }} />
                                                        : <CloseCircleOutlined style={{ color: '#ff4d4f', marginTop: 2 }} />
                                                    }
                                                    <div>
                                                        <div style={{ fontSize: 12, color: '#666', wordBreak: 'break-all' }}>{r.url}</div>
                                                        <div style={{ fontSize: 12, color: r.success ? '#52c41a' : '#ff4d4f' }}>{r.message}</div>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                </Space>
                            )
                        }
                    ]}
                />
            </Card>


            <Divider />

            <Card
                title="转存记录"
                extra={<Button icon={<LinkOutlined />} onClick={fetchTasks}>刷新</Button>}
            >
                <Table
                    columns={columns}
                    dataSource={tasks}
                    rowKey="id"
                    loading={tasksLoading}
                    pagination={{
                        ...pagination,
                        showSizeChanger: true,
                        showTotal: total => `共 ${total} 条记录`
                    }}
                    onChange={(p) => fetchTasks(false, p.current, p.pageSize)}
                    childrenColumnName="__ignored_children"
                    expandable={{
                        expandedRowRender,
                        rowExpandable: (record) => record.children && record.children.length > 0
                    }}
                />
            </Card>
        </div >
    )
}
