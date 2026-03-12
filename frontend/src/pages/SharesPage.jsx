import { useState, useEffect, useRef } from 'react'
import { Card, Table, Button, Tag, Space, message, Popconfirm, Select, Tooltip, Spin, Modal, Input } from 'antd'
import { DeleteOutlined, CopyOutlined, ReloadOutlined, LinkOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { accountApi, shareApi, fileApi, api, DISK_TYPES } from '../services/api'

export default function SharesPage() {
    const [accounts, setAccounts] = useState([])
    const [shares, setShares] = useState([])
    const [loading, setLoading] = useState(false)
    const [filterAccount, setFilterAccount] = useState(null)
    const [filterStatus, setFilterStatus] = useState(null)
    const [filterExpiredType, setFilterExpiredType] = useState(null)
    const [filterTimeStatus, setFilterTimeStatus] = useState(null)
    const [filterTitle, setFilterTitle] = useState('')
    const [checkingIds, setCheckingIds] = useState(new Set())
    const [selectedRowKeys, setSelectedRowKeys] = useState([])
    const [exportModalVisible, setExportModalVisible] = useState(false)
    const [exportText, setExportText] = useState('')
    const [pagination, setPagination] = useState({
        current: 1,
        pageSize: 15,
        total: 0
    })

    // 百度网盘安全验证相关
    const [verifyModalVisible, setVerifyModalVisible] = useState(false)
    const [verifyInfo, setVerifyInfo] = useState(null)
    const [vcode, setVcode] = useState('')
    const [verifying, setVerifying] = useState(false)
    const [countdown, setCountdown] = useState(0)
    const pendingDeleteIdRef = useRef(null)  // 记录正在等待验证码的删除 ID

    useEffect(() => {
        let timer
        if (countdown > 0) {
            timer = setInterval(() => setCountdown(prev => prev - 1), 1000)
        }
        return () => clearInterval(timer)
    }, [countdown])

    useEffect(() => {
        fetchAccounts()
        fetchShares(1, pagination.pageSize)
    }, [])

    const fetchAccounts = async () => {
        try {
            const { data } = await accountApi.getList()
            setAccounts(data)
        } catch {
            message.error('获取账户列表失败')
        }
    }

    const fetchShares = async (page = pagination.current, pageSize = pagination.pageSize) => {
        // 增加安全检查，防止 event 对象误入或非数字参数
        const targetPage = typeof page === 'number' ? page : pagination.current;
        const targetPageSize = typeof pageSize === 'number' ? pageSize : pagination.pageSize;

        setLoading(true)
        try {
            const filters = {}
            if (filterAccount) filters.account_id = filterAccount
            if (filterStatus !== null) filters.status = filterStatus
            if (filterExpiredType) filters.expired_type = filterExpiredType
            if (filterTimeStatus) filters.time_status = filterTimeStatus
            if (filterTitle) filters.title = filterTitle

            const skip = (targetPage - 1) * targetPageSize
            const { data } = await shareApi.getList(filters, skip, targetPageSize)
            // 兼容旧版(Array)和新版({total, items})
            if (Array.isArray(data)) {
                setShares(data)
                setPagination(prev => ({ ...prev, total: data.length }))
            } else {
                setShares(data.items || [])
                setPagination(prev => ({ ...prev, current: targetPage, pageSize: targetPageSize, total: data.total || 0 }))
            }
        } catch {
            message.error('获取分享列表失败')
        } finally {
            setLoading(false)
        }
    }

    const handleFilterChange = () => {
        fetchShares()
    }

    // 当任何筛选条件改变时，从第一页开始获取列表
    useEffect(() => {
        fetchShares(1, pagination.pageSize)
    }, [filterAccount, filterStatus, filterExpiredType, filterTimeStatus])

    const handleDelete = async (id) => {
        try {
            const { data } = await shareApi.delete(id)
            if (data?.message?.includes('网盘取消失败') || data?.message?.includes('跳过')) {
                message.warning(data.message)
            } else {
                message.success('分享已取消并记录删除')
            }
            fetchShares()
        } catch {
            message.error('删除分享失败')
        }
    }

    const handleDeleteWithFile = async (id) => {
        try {
            const { data } = await shareApi.deleteWithFile(id)
            if (data?.message?.includes('源文件删除失败') || data?.message?.includes('跳过') || data?.message?.includes('异常')) {
                message.warning(data.message)
            } else {
                message.success('分享及对应网盘源文件已彻底删除')
            }
            fetchShares()
        } catch (error) {
            const detail = error.response?.data?.detail
            if (error.response?.status === 403 && detail) {
                // 百度网盘安全验证：保存当前删除 ID 待验证后重试
                pendingDeleteIdRef.current = id
                setVerifyInfo(detail)
                setVerifyModalVisible(true)
            } else {
                message.error(detail?.message || '源文件或分享删除失败')
            }
        }
    }

    const handleSendVerification = async () => {
        if (!verifyInfo || !verifyInfo.data?.authwidget) return
        try {
            const { authwidget } = verifyInfo.data
            // 获取关联账户（从 pendingDeleteId 对应的分享记录取 account_id）
            const targetShare = shares.find(s => s.id === pendingDeleteIdRef.current)
            if (!targetShare) return
            await fileApi.sendVerificationCode(targetShare.account_id, {
                safetpl: authwidget.safetpl,
                saferand: authwidget.saferand,
                safesign: authwidget.safesign,
                type: 'sms'
            })
            message.success('验证码已发送')
            setCountdown(60)
        } catch (err) {
            message.error(err.response?.data?.detail || '发送验证码失败')
        }
    }

    const handleCheckVerification = async () => {
        if (!vcode) {
            message.warning('请输入验证码')
            return
        }
        const targetShare = shares.find(s => s.id === pendingDeleteIdRef.current)
        if (!targetShare) return
        setVerifying(true)
        try {
            const { authwidget } = verifyInfo.data
            await fileApi.checkVerificationCode(targetShare.account_id, {
                safetpl: authwidget.safetpl,
                saferand: authwidget.saferand,
                safesign: authwidget.safesign,
                vcode: vcode
            })
            message.success('验证通过，正在重试删除...')
            setVerifyModalVisible(false)
            setVcode('')
            setVerifyInfo(null)
            // 验证成功后重新发起删除
            await handleDeleteWithFile(pendingDeleteIdRef.current)
        } catch (err) {
            message.error(err.response?.data?.detail || '验证失败')
        } finally {
            setVerifying(false)
        }
    }

    const handleCheck = async (record) => {
        setCheckingIds(prev => new Set([...prev, record.id]))
        try {
            const { data } = await api.post(`/shares/${record.id}/check`)
            if (data.is_valid) {
                message.success('链接有效')
            } else {
                message.warning(data.reason || '链接已失效')
            }
            fetchShares()
        } catch {
            message.error('检测失败')
        } finally {
            setCheckingIds(prev => {
                const next = new Set(prev)
                next.delete(record.id)
                return next
            })
        }
    }

    const handleCopy = (url, password) => {
        const text = password ? `${url}\n提取码: ${password}` : url
        navigator.clipboard.writeText(text)
        message.success('已复制')
    }

    const handleBatchAction = async (action) => {
        if (selectedRowKeys.length === 0) return

        setLoading(true)
        try {
            const { data } = await shareApi.batchAction({
                ids: selectedRowKeys,
                action: action
            })

            const actionText = {
                'cancel': '取消',
                'delete_local': '删除',
                'check': '校验'
            }[action] || '操作'

            if (action === 'check') {
                Modal.success({
                    title: '批量校验完成',
                    content: (
                        <div>
                            <p>已成功校验 {data.total} 条链接：</p>
                            <p style={{ color: '#52c41a' }}>● 有效: {data.valid} 条</p>
                            <p style={{ color: '#ff4d4f' }}>● 失效: {data.invalid} 条</p>
                            {data.failed > 0 && <p style={{ color: 'orange' }}>● 检查失败: {data.failed} 条</p>}
                        </div>
                    ),
                    okText: '好的'
                })
            } else if (data.failed > 0) {
                message.warning(`批量${actionText}完成。成功: ${data.success}, 失败: ${data.failed}`)
            } else {
                message.success(`成功执行批量${actionText}操作 (${data.success}条)`)
            }
            setSelectedRowKeys([])
            fetchShares()
        } catch {
            message.error('批量操作失败')
        } finally {
            setLoading(false)
        }
    }

    const handleBatchExport = () => {
        if (selectedRowKeys.length === 0) return

        const selectedShares = shares.filter(s => selectedRowKeys.includes(s.id))
        const text = selectedShares.map(s => {
            const pwd = s.password ? `  提取码: ${s.password}` : ''
            return `${s.title}\n链接: ${s.share_url}${pwd}`
        }).join('\n\n')

        setExportText(text)
        setExportModalVisible(true)
    }

    const getAccountInfo = (accountId) => {
        const account = accounts.find(a => a.id === accountId)
        return account ? { name: account.name, type: account.type } : null
    }

    const formatExpiredAt = (expiredAt) => {
        if (!expiredAt) return <Tag>永久</Tag>
        const d = new Date(expiredAt)
        const now = new Date()
        if (d < now) return <Tag color="red">已过期</Tag>
        const days = Math.ceil((d - now) / 86400000)
        return <Tag color="orange">{days}天后到期</Tag>
    }

    const columns = [
        {
            title: '资源标题',
            dataIndex: 'title',
            key: 'title',
            ellipsis: true,
            width: 150
        },
        {
            title: '网盘',
            dataIndex: 'account_id',
            key: 'account_id',
            width: 120,
            render: (accountId) => {
                const info = getAccountInfo(accountId)
                if (!info) return '-'
                return <Tag color={DISK_TYPES[info.type]?.color}>{DISK_TYPES[info.type]?.icon} {info.name}</Tag>
            }
        },
        {
            title: '分享链接',
            dataIndex: 'share_url',
            key: 'share_url',
            ellipsis: true,
            render: (url) => (
                <a href={url} target="_blank" rel="noopener noreferrer">
                    <LinkOutlined /> {url}
                </a>
            )
        },
        {
            title: '提取码',
            dataIndex: 'password',
            key: 'password',
            width: 80,
            render: (pwd) => pwd ? <Tag color="blue">{pwd}</Tag> : <span style={{ color: '#bbb' }}>-</span>
        },
        {
            title: '时效',
            dataIndex: 'expired_at',
            key: 'expired_at',
            width: 100,
            render: formatExpiredAt
        },
        {
            title: '文件路径',
            dataIndex: 'file_path',
            key: 'file_path',
            ellipsis: true,
            width: 180,
            render: (path) => path ? <span style={{ color: '#888' }}>{path}</span> : <span style={{ color: '#ccc' }}>未记录</span>
        },
        {
            title: '状态',
            dataIndex: 'status',
            key: 'status',
            width: 68,
            render: (status) => (
                <Tag color={status === 1 ? 'success' : 'default'}>
                    {status === 1 ? '有效' : '失效'}
                </Tag>
            )
        },
        {
            title: '创建时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 148,
            render: (t) => t ? t.replace('T', ' ').slice(0, 16) : '-'
        },
        {
            title: '操作',
            key: 'action',
            width: 160,
            fixed: 'right',
            render: (_, record) => (
                <Space size={4}>
                    <Tooltip title="复制链接">
                        <Button
                            type="text"
                            size="small"
                            icon={<CopyOutlined />}
                            onClick={() => handleCopy(record.share_url, record.password)}
                        />
                    </Tooltip>
                    <Tooltip title="检测是否有效">
                        <Button
                            type="text"
                            size="small"
                            icon={checkingIds.has(record.id)
                                ? <Spin size="small" />
                                : <SafetyCertificateOutlined style={{ color: '#1677ff' }} />
                            }
                            disabled={checkingIds.has(record.id)}
                            onClick={() => handleCheck(record)}
                        />
                    </Tooltip>
                    <Tooltip title="仅取消分享并删除记录">
                        <Popconfirm
                            title="确定仅取消并删除分享记录？"
                            description="这只会取消分享链接，不会删除您网盘中的源文件。"
                            onConfirm={() => handleDelete(record.id)}
                            okText="确定"
                            cancelText="取消"
                        >
                            <Button
                                type="text"
                                size="small"
                                icon={<DeleteOutlined />}
                            />
                        </Popconfirm>
                    </Tooltip>
                    <Tooltip title="删除源文件及分享">
                        <Popconfirm
                            title="高危操作：连同源文件一并删除？"
                            description={<span style={{ color: 'red' }}>此操作除取消分享外，还会将该条目对应的【网盘深处的源实体文件】一并销毁且不可恢复！</span>}
                            onConfirm={() => handleDeleteWithFile(record.id)}
                            okText="彻底删除"
                            okType="danger"
                            cancelText="再想想"
                        >
                            <Button
                                type="text"
                                size="small"
                                danger
                                style={{ backgroundColor: '#fff1f0', borderColor: '#ffa39e' }}
                                icon={<DeleteOutlined />}
                            />
                        </Popconfirm>
                    </Tooltip>
                </Space>
            )
        }
    ]

    return (
        <div>
            <div className="page-header">
                <h2>分享管理</h2>
            </div>

            <Card style={{ marginBottom: 16 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '16px' }}>
                    {/* 左侧筛选区域 */}
                    <Space wrap size={[8, 16]}>
                        <span>筛选：</span>
                        <Select
                            style={{ width: 150 }}
                            placeholder="网盘"
                            allowClear
                            value={filterAccount}
                            onChange={setFilterAccount}
                        >
                            {accounts.map(account => (
                                <Select.Option key={account.id} value={account.id}>
                                    {DISK_TYPES[account.type]?.icon} {account.name}
                                </Select.Option>
                            ))}
                        </Select>

                        <Select
                            style={{ width: 110 }}
                            placeholder="链接状态"
                            allowClear
                            value={filterStatus}
                            onChange={setFilterStatus}
                        >
                            <Select.Option value={1}>有效</Select.Option>
                            <Select.Option value={0}>失效</Select.Option>
                        </Select>

                        <Select
                            style={{ width: 110 }}
                            placeholder="时效状态"
                            allowClear
                            value={filterTimeStatus}
                            onChange={setFilterTimeStatus}
                        >
                            <Select.Option value={1}>永久</Select.Option>
                            <Select.Option value={2}>未过期</Select.Option>
                            <Select.Option value={3}>已过期</Select.Option>
                        </Select>

                        <Select
                            style={{ width: 110 }}
                            placeholder="分享时长"
                            allowClear
                            value={filterExpiredType}
                            onChange={setFilterExpiredType}
                        >
                            <Select.Option value={1}>永久</Select.Option>
                            <Select.Option value={3}>1天</Select.Option>
                            <Select.Option value={2}>7天</Select.Option>
                            <Select.Option value={4}>30天</Select.Option>
                        </Select>

                        <Input.Search
                            style={{ width: 220 }}
                            placeholder="搜索资源标题..."
                            value={filterTitle}
                            onChange={e => setFilterTitle(e.target.value)}
                            onSearch={handleFilterChange}
                            allowClear
                        />

                        <Button icon={<ReloadOutlined />} onClick={() => fetchShares()}>
                            刷新
                        </Button>
                    </Space>

                    {/* 右侧批量操作区域 */}
                    {selectedRowKeys.length > 0 && (
                        <div style={{
                            padding: '8px 16px',
                            background: '#e6f4ff',
                            border: '1px solid #91caff',
                            borderRadius: '6px',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '12px',
                            boxShadow: '0 2px 4px rgba(0,0,0,0.05)'
                        }}>
                            <span style={{ fontWeight: 'bold', color: '#0958d9' }}>已选 {selectedRowKeys.length} 项</span>
                            <div style={{ height: '16px', borderLeft: '1px solid #91caff' }}></div>
                            <Space size={8}>
                                <Popconfirm
                                    title="确定批量取消分享并删除记录？"
                                    description="仅取消网盘分享链接，网盘源文件将保留。"
                                    onConfirm={() => handleBatchAction('cancel')}
                                >
                                    <Button size="small" icon={<DeleteOutlined />}>批量取消链接</Button>
                                </Popconfirm>

                                <Popconfirm
                                    title="确定批量取消分享并彻底删除网盘源文件？"
                                    description="警告：此操作不可恢复，网盘内的源文件将被同步删除！"
                                    onConfirm={() => handleBatchAction('cancel_with_file')}
                                >
                                    <Button size="small" danger type="primary" icon={<DeleteOutlined />}>批量取消(含源文件)</Button>
                                </Popconfirm>
                                <Button
                                    size="small"
                                    icon={<SafetyCertificateOutlined />}
                                    type="primary"
                                    onClick={() => handleBatchAction('check')}
                                >
                                    批量校验
                                </Button>
                                <Button
                                    size="small"
                                    icon={<CopyOutlined />}
                                    onClick={handleBatchExport}
                                >
                                    导出链接
                                </Button>
                                <Popconfirm
                                    title="确定仅从本地批量删除记录？"
                                    description="此操作不会去网盘执行任何操作，仅清理本地列表。"
                                    onConfirm={() => handleBatchAction('delete_local')}
                                >
                                    <Button size="small" type="text" danger>清理本地记录</Button>
                                </Popconfirm>
                            </Space>
                        </div>
                    )}
                </div>

                <Table
                    rowSelection={{
                        selectedRowKeys,
                        onChange: (keys) => setSelectedRowKeys(keys)
                    }}
                    columns={columns}
                    dataSource={shares}
                    rowKey="id"
                    loading={loading}
                    pagination={{
                        ...pagination,
                        showSizeChanger: true,
                        showTotal: total => `共 ${total} 条记录`
                    }}
                    onChange={(p) => fetchShares(p.current, p.pageSize)}
                    scroll={{ x: 900 }}
                />
            </Card>

            {/* 百度网盘安全验证弹窗 */}
            <Modal
                title="安全验证"
                open={verifyModalVisible}
                onCancel={() => setVerifyModalVisible(false)}
                footer={null}
                width={400}
            >
                <div style={{ padding: '20px 0' }}>
                    <div style={{ marginBottom: 16 }}>
                        <span>验证方式：</span>
                        <Select defaultValue="sms" style={{ width: '100%' }} disabled>
                            <Select.Option value="sms">
                                短信验证 ({verifyInfo?.data?.sms || '未知号码'})
                            </Select.Option>
                        </Select>
                    </div>
                    <Space size="small" style={{ width: '100%', display: 'flex' }}>
                        <Input
                            placeholder="请输入验证码"
                            value={vcode}
                            onChange={e => setVcode(e.target.value)}
                        />
                        <Button onClick={handleSendVerification} disabled={countdown > 0}>
                            {countdown > 0 ? `${countdown}秒后重发` : '发送验证码'}
                        </Button>
                    </Space>
                    <Button
                        type="primary"
                        style={{ marginTop: 20, width: '100%' }}
                        onClick={handleCheckVerification}
                        loading={verifying}
                    >
                        确定
                    </Button>
                </div>
            </Modal>

            {/* 批量导出弹窗 */}
            <Modal
                title="批量导出分享链接"
                open={exportModalVisible}
                onCancel={() => setExportModalVisible(false)}
                onOk={() => {
                    navigator.clipboard.writeText(exportText)
                    message.success('已复制到剪贴板')
                    setExportModalVisible(false)
                }}
                okText="全选并复制"
                cancelText="关闭"
                width={600}
            >
                <Input.TextArea
                    rows={15}
                    value={exportText}
                    readOnly
                    style={{ fontFamily: 'monospace', fontSize: '12px' }}
                />
            </Modal>
        </div>
    )
}
