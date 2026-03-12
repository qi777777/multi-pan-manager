import { useState, useEffect, useRef } from 'react'
import {
    Card, Table, Space, Button, Tag, Modal, Form, Input, Select,
    Breadcrumb, Popconfirm, message, Progress, Checkbox, Switch, Divider, Typography,
    Radio, Empty, Upload
} from 'antd'
import {
    FolderOutlined, FileOutlined, ReloadOutlined,
    DeleteOutlined, HomeOutlined, UploadOutlined, ShareAltOutlined, SwapOutlined
} from '@ant-design/icons'

import { accountApi, fileApi, shareApi, transferApi, DISK_TYPES } from '../services/api'
import FolderPicker from '../components/FolderPicker'

export default function FilesPage() {
    const [accounts, setAccounts] = useState([])
    const [selectedAccount, setSelectedAccount] = useState(null)
    const [files, setFiles] = useState([])
    const [loading, setLoading] = useState(false)
    const [pathStack, setPathStack] = useState([{ fid: '0', name: '根目录' }])
    const [selectedRowKeys, setSelectedRowKeys] = useState([])

    // 搜索相关状态
    const [searchKeyword, setSearchKeyword] = useState('')
    const [isSearchMode, setIsSearchMode] = useState(false)

    // 上传相关
    const [uploadModalVisible, setUploadModalVisible] = useState(false)
    const [isFolderUpload, setIsFolderUpload] = useState(false)
    const [uploadTargets, setUploadTargets] = useState([])
    const [uploading, setUploading] = useState(false)
    const [fileList, setFileList] = useState([])

    // 目录选择相关
    const [uploadPaths, setUploadPaths] = useState({}) // { accountId: { fid: '0', name: '根目录' } }
    const [uploadProgress, setUploadProgress] = useState(0) // 总体进度（可选，保留用于兼容）
    const [targetProgress, setTargetProgress] = useState({}) // { accountId: percentage }
    const [targetStatus, setTargetStatus] = useState({}) // { accountId: 'waiting' | 'uploading' | 'success' | 'error' }
    const [targetFilenames, setTargetFilenames] = useState({}) // { accountId: filename }
    const [targetStage, setTargetStage] = useState({}) // 1: 传输中, 2: 同步到网盘
    const [currentFile, setCurrentFile] = useState('')
    const [folderRootFids, setFolderRootFids] = useState({}) // { accountId: rootFid }
    const [sharedUploadProgress, setSharedUploadProgress] = useState(0) // 公共一阶段进度 (0-100)

    // 使用 Ref 记录是否已经进入二阶段，避免 axios 的 onUploadProgress 闭包过期导致进度回跳
    const stage2StartedRef = useRef({})

    // 验证码相关
    const [verifyModalVisible, setVerifyModalVisible] = useState(false)
    const [verifyInfo, setVerifyInfo] = useState(null)
    const [vcode, setVcode] = useState('')
    const [verifying, setVerifying] = useState(false)
    const [countdown, setCountdown] = useState(0)

    // 批量分享相关
    const [batchShareModalVisible, setBatchShareModalVisible] = useState(false)
    const [batchShareExpiredType, setBatchShareExpiredType] = useState(1)
    const [batchSharing, setBatchSharing] = useState(false)
    const [batchShareResults, setBatchShareResults] = useState(null)

    // 上传后自动分享
    const [autoShare, setAutoShare] = useState(false)
    const [autoShareExpiredType, setAutoShareExpiredType] = useState(1)
    const [showSummary, setShowSummary] = useState(false)
    const [finalResults, setFinalResults] = useState([]) // [{ account_id, name, uploadSuccess, shareSuccess, url, password, error }]

    useEffect(() => {
        fetchAccounts()
    }, [])

    useEffect(() => {
        let timer
        if (countdown > 0) {
            timer = setInterval(() => {
                setCountdown(prev => prev - 1)
            }, 1000)
        }
        return () => clearInterval(timer)
    }, [countdown])

    // 核心进度计算逻辑：根据当前各盘状态计算总进度
    useEffect(() => {
        if (!uploading || uploadTargets.length === 0) return

        const total = uploadTargets.reduce((sum, id) => {
            const sid = String(id)
            const stage = targetStage[sid] || 1
            // 累计进度 = (阶段1 ? 公共传输进度 * 0.2 : 20 + 该盘个别保存进度 * 0.8)
            const contribution = stage === 1
                ? (sharedUploadProgress * 0.2)
                : (20 + (targetProgress[sid] || 0) * 0.8)
            return sum + contribution
        }, 0)

        setUploadProgress(Math.round(total / uploadTargets.length))
    }, [sharedUploadProgress, targetProgress, targetStage, uploadTargets, uploading])

    // ... (fetchAccounts, fetchFiles, etc. unchanged)

    const handleDelete = async () => {
        if (selectedRowKeys.length === 0) {
            message.warning('请选择要删除的文件')
            return
        }
        try {
            await fileApi.delete(selectedAccount, selectedRowKeys)
            message.success('删除成功')
            fetchFiles(selectedAccount, pathStack[pathStack.length - 1].fid)
        } catch (error) {
            // Check for 403 (Verification needed)
            const detail = error.response?.data?.detail
            if (detail?.code === 403) {
                console.log('Verification Info:', detail.data)
                setVerifyInfo(detail.data)
                setVerifyModalVisible(true)
            } else {
                message.error(detail?.message || '删除失败')
            }
        }
    }

    // ====== 批量分享 ======
    const handleBatchShare = async () => {
        if (selectedRowKeys.length === 0) return
        setBatchSharing(true)
        setBatchShareResults(null)
        try {
            const items = selectedRowKeys.map(fid => {
                const f = files.find(f => f.fid === fid)
                return { fid, name: f?.name || fid }
            })
            const prefix = pathStack.slice(1).map(p => p.name).join('/') || '/'
            const { data } = await shareApi.batchCreate({
                account_id: selectedAccount,
                items,
                expired_type: batchShareExpiredType,
                file_path_prefix: prefix
            })
            setBatchShareResults(data)
            if (data.failed === 0) {
                message.success(`成功分享 ${data.success} 个文件，可在分享管理中查看`)
            } else {
                message.warning(`分享完成：${data.success} 成功 / ${data.failed} 失败`)
            }
        } catch (e) {
            message.error(e.response?.data?.detail || '批量分享失败')
        } finally {
            setBatchSharing(false)
        }
    }


    const handleSendVerification = async () => {
        if (!verifyInfo || !verifyInfo.authwidget) return
        try {
            const { authwidget } = verifyInfo
            await fileApi.sendVerificationCode(selectedAccount, {
                safetpl: authwidget.safetpl,
                saferand: authwidget.saferand,
                safesign: authwidget.safesign,
                type: 'sms'
            })
            message.success('验证码发送成功')
            setCountdown(60)
        } catch (error) {
            message.error(error.response?.data?.detail || '发送验证码失败')
        }
    }

    const fetchAccounts = async () => {
        try {
            const { data } = await accountApi.getList()
            setAccounts(data.filter(a => a.status === 1))
        } catch (error) {
            message.error('获取账户列表失败')
        }
    }

    const fetchFiles = async (accountId, pdirFid = '0') => {
        if (!accountId) return
        setLoading(true)
        try {
            const { data } = await fileApi.getList(accountId, pdirFid)
            setFiles(data)
            setSelectedRowKeys([])
        } catch (error) {
            message.error(error.response?.data?.detail || '获取文件列表失败')
            setFiles([])
        } finally {
            setLoading(false)
        }
    }

    const handleAccountChange = (accountId) => {
        setSelectedAccount(accountId)
        setPathStack([{ fid: '0', name: '根目录' }])
        setSearchKeyword('')  // 清理搜索关键词
        setIsSearchMode(false)  // 退出搜索模式
        fetchFiles(accountId, '0')
    }

    const handleFolderClick = (record) => {
        if (record.is_dir) {
            setPathStack([...pathStack, { fid: record.fid, name: record.name }])
            fetchFiles(selectedAccount, record.fid)
        }
    }

    const handleBreadcrumbClick = (index) => {
        const newPath = pathStack.slice(0, index + 1)
        setPathStack(newPath)
        setSearchKeyword('')  // 清理搜索
        setIsSearchMode(false)  // 退出搜索模式
        fetchFiles(selectedAccount, newPath[newPath.length - 1].fid)
    }

    const handleSearch = async (keyword) => {
        if (!keyword || !keyword.trim()) {
            // 清空搜索，返回当前目录
            setSearchKeyword('')
            setIsSearchMode(false)
            fetchFiles(selectedAccount, pathStack[pathStack.length - 1].fid)
            return
        }

        setIsSearchMode(true)
        setLoading(true)
        try {
            const { data } = await fileApi.search(selectedAccount, keyword.trim())
            // 后端返回格式: {list: [...], total: n}
            setFiles(data.list || [])
            setSearchKeyword(keyword.trim())
            setSelectedRowKeys([])  // 清空选择
        } catch (error) {
            message.error(error.response?.data?.detail || '搜索失败')
            setFiles([])
        } finally {
            setLoading(false)
        }
    }



    const handleCheckVerification = async () => {
        if (!vcode) {
            message.warning('请输入验证码')
            return
        }
        setVerifying(true)
        try {
            const { authwidget } = verifyInfo
            await fileApi.checkVerificationCode(selectedAccount, {
                safetpl: authwidget.safetpl,
                saferand: authwidget.saferand,
                safesign: authwidget.safesign,
                vcode: vcode
            })
            message.success('验证通过，正在重试删除...')
            setVerifyModalVisible(false)
            setVcode('')
            setVerifyInfo(null)
            // Retry delete
            handleDelete()
        } catch (error) {
            message.error(error.response?.data?.detail || '验证失败')
        } finally {
            setVerifying(false)
        }
    }

    const handleUploadClick = () => {
        setUploadTargets([])
        setFileList([])
        const initialPaths = {}
        const initialProgress = {}
        const initialStatus = {}
        const initialStage = {}
        accounts.forEach(acc => {
            initialPaths[acc.id] = { fid: '0', name: '根目录' }
            initialProgress[acc.id] = 0
            initialStatus[acc.id] = 'waiting'
            initialStage[acc.id] = 1
        })
        setUploadPaths(initialPaths)
        setTargetProgress(initialProgress)
        setTargetStatus(initialStatus)
        setTargetStage(initialStage)
        setSharedUploadProgress(0)
        stage2StartedRef.current = {} // 清空引用锁定

        setUploadProgress(0)
        setAutoShare(false)
        setAutoShareExpiredType(1)
        setShowSummary(false)
        setFinalResults([])
        setUploadModalVisible(true)
    }

    const handleFolderChange = (accountId, info) => {
        setUploadPaths(prev => ({
            ...prev,
            [accountId]: { fid: info.fid, name: info.path }
        }))
    }

    const handleUpload = async () => {
        if (fileList.length === 0) {
            message.warning('请选择要上传的文件或文件夹')
            return
        }
        if (uploadTargets.length === 0) {
            message.warning('请选择目标网盘')
            return
        }

        setUploading(true)
        setUploadProgress(0)
        setFinalResults([]) // 重置结果

        // 分批上传或循环上传
        // 为了保持 UI 进度显示的一致性，如果是多文件，我们汇总进度
        const totalFiles = fileList.length
        let completedFiles = 0

        for (let i = 0; i < totalFiles; i++) {
            const file = fileList[i]

            const taskId = `upload_${Date.now()}_${i}`
            const filename = file.name
            const relativePath = file.webkitRelativePath || ""
            setCurrentFile(filename)

            // 初始化各网盘进度
            stage2StartedRef.current = {}
            const initialStatus = {}
            const initialProgress = {}
            const initialStage = {}
            const initialFilenames = {}
            uploadTargets.forEach(id => {
                const sid = String(id)
                initialStatus[sid] = 'uploading'
                initialProgress[sid] = 0
                initialStage[sid] = 1
                initialFilenames[sid] = filename
                stage2StartedRef.current[sid] = false
            })
            setTargetStatus(initialStatus)
            setTargetProgress(initialProgress)
            setTargetStage(initialStage)
            setTargetFilenames(initialFilenames)

            // 订阅 SSE (针对当前文件)
            const token = localStorage.getItem('token')
            const eventSource = new EventSource(`/api/files/events/subscribe?token_query=${token}&t=${new Date().getTime()}`)
            eventSource.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data)
                    if (data.type === 'disk_upload_progress' && data.task_id === taskId) {
                        const accId = String(data.account_id)
                        stage2StartedRef.current[accId] = true
                        setTargetStage(prev => ({ ...prev, [accId]: 2 }))
                        setTargetProgress(prev => ({ ...prev, [accId]: data.progress }))
                        if (data.filename) {
                            setTargetFilenames(prev => ({ ...prev, [accId]: data.filename }))
                        }
                    }
                } catch (err) { }
            }

            try {
                const formData = new FormData()
                formData.append('file', file)
                formData.append('account_ids', uploadTargets.join(','))
                formData.append('task_id', taskId)
                if (relativePath) {
                    formData.append('relative_path', relativePath)
                }

                const targetDirs = {}
                uploadTargets.forEach(id => {
                    targetDirs[id] = uploadPaths[id]?.fid || '0'
                })
                formData.append('target_dirs', JSON.stringify(targetDirs))
                formData.append('pdir_fid', uploadPaths[uploadTargets[0]]?.fid || '0')

                const { data } = await fileApi.upload(formData, (progressEvent) => {
                    const realPercent = Math.round((progressEvent.loaded * 100) / progressEvent.total)
                    setSharedUploadProgress(realPercent)
                })

                // 记录文件夹根节点 ID
                if (isFolderUpload && data.results) {
                    data.results.forEach(r => {
                        if (r.root_folder_fid) {
                            setFolderRootFids(prev => ({ ...prev, [String(r.account_id)]: r.root_folder_fid }))
                        }
                    })
                }

                if (i === totalFiles - 1 && data.results) {
                    // 仅在最后一个文件完成后显示汇总
                    const summary = []
                    const successTasks = data.results || []

                    if (autoShare) {
                        message.loading('正在创建自动分享链接...')
                        for (const res of uploadTargets) {
                            const accId = Number(res)
                            const acc = accounts.find(a => a.id === accId)
                            const uploadRes = successTasks.find(t => t.account_id === accId)

                            let shareInfo = { success: false }
                            if (uploadRes && uploadRes.code === 200) {
                                try {
                                    const prefix = pathStack.slice(1).map(p => p.name).join('/') || '/'

                                    // 确定分享对象：如果是文件夹上传，分享根目录；否则分享文件本身
                                    let targetFid = uploadRes.data?.fid || uploadRes.fid
                                    let shareName = filename

                                    if (isFolderUpload) {
                                        // 优先采用本次上传返回的 root_folder_fid，如果没有则从 map 中获取（可能之前的文件已经创建过了）
                                        const rootFid = uploadRes.root_folder_fid || folderRootFids[String(accId)]
                                        if (rootFid) {
                                            targetFid = rootFid
                                            shareName = (relativePath.split('/')[0]) || filename
                                        }
                                    }

                                    const { data: sData } = await shareApi.batchCreate({
                                        account_id: accId,
                                        items: [{ fid: targetFid, name: shareName }],
                                        expired_type: autoShareExpiredType,
                                        file_path_prefix: prefix
                                    })
                                    if (sData.success > 0 && sData.results?.[0]?.success) {
                                        shareInfo = {
                                            success: true,
                                            url: sData.results[0].share_url,
                                            password: sData.results[0].password
                                        }
                                    }
                                } catch (e) {
                                    console.error('Share error:', e)
                                }
                            }

                            summary.push({
                                account_id: accId,
                                name: acc?.name || '未知',
                                uploadSuccess: uploadRes?.code === 200,
                                shareSuccess: shareInfo.success,
                                url: shareInfo.url,
                                password: shareInfo.password,
                                error: uploadRes?.code !== 200 ? uploadRes?.message : null
                            })
                        }
                    } else {
                        successTasks.forEach(res => {
                            const accId = Number(res.account_id)
                            const acc = accounts.find(a => a.id === accId)
                            summary.push({
                                account_id: accId,
                                name: acc?.name || '未知',
                                uploadSuccess: res.code === 200,
                                shareSuccess: false,
                                error: res.code !== 200 ? res.message : null
                            })
                        })
                    }
                    setFinalResults(summary)
                }
            } catch (error) {
                console.error('Upload error:', error)
            } finally {
                eventSource.close()
                completedFiles++
                setUploadProgress(Math.round((completedFiles / totalFiles) * 100))
            }
        }

        setUploading(false)
        setShowSummary(true)
        fetchFiles(selectedAccount, pathStack[pathStack.length - 1].fid)
    }

    const formatSize = (bytes) => {
        if (!bytes) return '-'
        const units = ['B', 'KB', 'MB', 'GB', 'TB']
        let i = 0
        while (bytes >= 1024 && i < units.length - 1) {
            bytes /= 1024
            i++
        }
        return `${bytes.toFixed(1)} ${units[i]} `
    }

    const columns = [
        {
            title: '文件名',
            dataIndex: 'name',
            key: 'name',
            render: (text, record) => (
                <Space
                    style={{ cursor: record.is_dir ? 'pointer' : 'default' }}
                    onClick={() => handleFolderClick(record)}
                >
                    {record.is_dir ?
                        <FolderOutlined style={{ color: '#faad14', fontSize: 18 }} /> :
                        <FileOutlined style={{ color: '#1890ff', fontSize: 18 }} />
                    }
                    <span style={{ color: record.is_dir ? '#1890ff' : 'inherit' }}>{text}</span>
                </Space>
            )
        },
        {
            title: '大小',
            dataIndex: 'size',
            key: 'size',
            width: 120,
            render: (size, record) => record.is_dir ? '-' : formatSize(size)
        },
        {
            title: '修改时间',
            dataIndex: 'updated_at',
            key: 'updated_at',
            width: 180,
            render: (text) => {
                if (!text) return '-';
                return new Date(text).toLocaleString('zh-CN', { hour12: false });
            }
        }
    ]

    const currentAccount = accounts.find(a => a.id === selectedAccount)

    return (
        <div>
            <div className="page-header">
                <h2>文件管理</h2>
            </div>

            <Card>
                <Space style={{ marginBottom: 16, width: '100%', justifyContent: 'space-between' }}>
                    <Space>
                        <span>选择网盘：</span>
                        <Select
                            style={{ width: 200 }}
                            placeholder="请选择网盘账户"
                            value={selectedAccount}
                            onChange={handleAccountChange}
                        >
                            {accounts.map(account => (
                                <Select.Option key={account.id} value={account.id}>
                                    {DISK_TYPES[account.type]?.icon} {account.name}
                                </Select.Option>
                            ))}
                        </Select>

                        <Input.Search
                            placeholder="搜索文件"
                            value={searchKeyword}
                            onChange={(e) => setSearchKeyword(e.target.value)}
                            onSearch={handleSearch}
                            style={{ width: 300 }}
                            disabled={!selectedAccount}
                            allowClear
                        />

                        {selectedAccount && !isSearchMode && (
                            <Button
                                icon={<ReloadOutlined />}
                                onClick={() => fetchFiles(selectedAccount, pathStack[pathStack.length - 1].fid)}
                            >
                                刷新
                            </Button>
                        )}
                    </Space>

                    {selectedRowKeys.length > 0 && (
                        <Space>
                            <Popconfirm title="确定删除选中的文件？" onConfirm={handleDelete}>
                                <Button danger icon={<DeleteOutlined />}>
                                    删除选中 ({selectedRowKeys.length})
                                </Button>
                            </Popconfirm>
                            <Button
                                icon={<ShareAltOutlined />}
                                onClick={() => { setBatchShareResults(null); setBatchShareModalVisible(true) }}
                            >
                                批量分享 ({selectedRowKeys.length})
                            </Button>
                        </Space>
                    )}

                    <Button type="primary" icon={<UploadOutlined />} onClick={handleUploadClick}>
                        上传文件
                    </Button>
                </Space>

                {selectedAccount && !isSearchMode && (
                    <Breadcrumb
                        style={{ marginBottom: 16 }}
                        items={pathStack.map((item, index) => ({
                            key: item.fid,
                            title: index === 0 ? <HomeOutlined /> : item.name,
                            onClick: () => handleBreadcrumbClick(index),
                            style: { cursor: 'pointer' }
                        }))}
                    />
                )}

                {isSearchMode && selectedAccount && (
                    <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center' }}>
                        <span style={{ color: '#1890ff', marginRight: 8 }}>🔍 搜索结果: "{searchKeyword}"</span>
                        <Button size="small" onClick={() => handleSearch('')}>返回文件列表</Button>
                    </div>
                )}

                {!selectedAccount ? (
                    <Empty description="请选择网盘账户" />
                ) : (
                    <Table
                        columns={columns}
                        dataSource={files}
                        rowKey="fid"
                        loading={loading}
                        rowSelection={{
                            selectedRowKeys,
                            onChange: setSelectedRowKeys
                        }}
                        pagination={{ pageSize: 20 }}
                    />
                )}

            </Card>

            {/* ===== 批量分享 Modal ===== */}
            <Modal
                title={`批量分享(${selectedRowKeys.length} 个文件)`}
                open={batchShareModalVisible}
                onCancel={() => {
                    setBatchShareModalVisible(false)
                    if (batchShareResults) setBatchShareResults(null)
                }}
                onOk={batchShareResults ? () => {
                    setBatchShareModalVisible(false)
                    setBatchShareResults(null)
                } : handleBatchShare}
                confirmLoading={batchSharing}
                okText={batchShareResults ? '完成' : '确认分享'}
                cancelButtonProps={batchShareResults ? { style: { display: 'none' } } : {}}
                width={batchShareResults ? 600 : 480}
            >
                <Space direction="vertical" style={{ width: '100%' }} size="middle">
                    {!batchShareResults && (
                        <div>
                            <div style={{ marginBottom: 8 }}>分享有效期：</div>
                            <Select
                                value={batchShareExpiredType}
                                onChange={setBatchShareExpiredType}
                                style={{ width: '100%' }}
                            >
                                <Select.Option value={1}>永久有效</Select.Option>
                                <Select.Option value={2}>7 天</Select.Option>
                                <Select.Option value={3}>1 天</Select.Option>
                                <Select.Option value={4}>30 天</Select.Option>
                            </Select>
                        </div>
                    )}

                    {batchShareResults && (
                        <div>
                            <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <span>分享完成状态：</span>
                                <Space>
                                    <span style={{ color: '#52c41a' }}>成功: {batchShareResults.success}</span>
                                    {batchShareResults.failed > 0 && <span style={{ color: '#ff4d4f' }}>失败: {batchShareResults.failed}</span>}
                                </Space>
                            </div>

                            <div style={{ maxHeight: '400px', overflowY: 'auto', border: '1px solid #f0f0f0', borderRadius: '4px', padding: '8px' }}>
                                {batchShareResults.results?.map((r, i) => (
                                    <div key={i} style={{ marginBottom: i === batchShareResults.results.length - 1 ? 0 : 12, paddingBottom: i === batchShareResults.results.length - 1 ? 0 : 12, borderBottom: i === batchShareResults.results.length - 1 ? 'none' : '1px dashed #f0f0f0' }}>
                                        <div style={{ fontWeight: 'bold', marginBottom: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                            {r.success ? '✅' : '❌'} {r.name}
                                        </div>
                                        {r.success ? (
                                            <div style={{ paddingLeft: 24 }}>
                                                <Typography.Text copyable={{ text: `${r.share_url}${r.password ? ' 提取码: ' + r.password : ''} ` }} style={{ color: '#1890ff', fontSize: 13 }}>
                                                    {r.share_url}
                                                </Typography.Text>
                                                {r.password && (
                                                    <div style={{ fontSize: 12, color: '#666', marginTop: 2 }}>
                                                        提取码: <Typography.Text code>{r.password}</Typography.Text>
                                                    </div>
                                                )}
                                            </div>
                                        ) : (
                                            <div style={{ paddingLeft: 24, color: '#ff4d4f', fontSize: 12 }}>
                                                错误: {r.message}
                                            </div>
                                        )}
                                    </div>
                                ))}
                            </div>
                            <div style={{ marginTop: 12, textAlign: 'center', color: '#8c8c8c', fontSize: 12 }}>
                                提示：点击链接图标可快速复制“链接+提取码”
                            </div>
                        </div>
                    )}
                </Space>
            </Modal>


            <Modal
                title={showSummary ? "任务处理结果" : "上传文件到多个网盘"}
                open={uploadModalVisible}
                onOk={showSummary ? () => setUploadModalVisible(false) : handleUpload}
                onCancel={(uploading) ? undefined : () => setUploadModalVisible(false)}
                confirmLoading={uploading}
                okText={showSummary ? "完成" : "开始上传"}
                cancelButtonProps={showSummary ? { style: { display: 'none' } } : {}}
                width={showSummary ? 650 : 600}
                maskClosable={!uploading && showSummary}
            >
                {showSummary ? (
                    <div style={{ padding: '4px 0' }}>
                        <div style={{ marginBottom: 16, fontWeight: 'bold', fontSize: 15 }}>
                            上传对象: <span style={{ color: '#1890ff' }}>{isFolderUpload ? `文件夹(${fileList.length} 个文件)` : fileList[0]?.name}</span>
                        </div>
                        <div style={{ maxHeight: '420px', overflowY: 'auto', border: '1px solid #f0f0f0', borderRadius: '8px', padding: '12px' }}>
                            {finalResults.map((res, i) => (
                                <div key={i} style={{
                                    marginBottom: i === finalResults.length - 1 ? 0 : 16,
                                    paddingBottom: i === finalResults.length - 1 ? 0 : 16,
                                    borderBottom: i === finalResults.length - 1 ? 'none' : '1px solid #f5f5f5'
                                }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                                        <Space>
                                            <Tag color={DISK_TYPES[accounts.find(a => a.id === res.account_id)?.type]?.color}>
                                                {res.name}
                                            </Tag>
                                            <span style={{ fontWeight: 500 }}>
                                                {res.uploadSuccess ? <span style={{ color: '#52c41a' }}>上传成功</span> : <span style={{ color: '#ff4d4f' }}>上传失败</span>}
                                            </span>
                                        </Space>
                                        {res.shareSuccess && <Tag color="blue">分享成功</Tag>}
                                    </div>

                                    {!res.uploadSuccess && (
                                        <div style={{ fontSize: 12, color: '#ff4d4f', paddingLeft: 8 }}>原因: {res.error}</div>
                                    )}

                                    {res.shareSuccess ? (
                                        <div style={{
                                            marginTop: 8,
                                            background: '#f6ffed',
                                            border: '1px solid #b7eb8f',
                                            borderRadius: '4px',
                                            padding: '8px 12px'
                                        }}>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                                <Typography.Text
                                                    copyable={{ text: `${res.url}${res.password ? ' 提取码: ' + res.password : ''} ` }}
                                                    style={{ color: '#389e0d', fontSize: 13, wordBreak: 'break-all', flex: 1 }}
                                                >
                                                    {res.url}
                                                </Typography.Text>
                                            </div>
                                            {res.password && (
                                                <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
                                                    提取码: <Typography.Text code>{res.password}</Typography.Text>
                                                </div>
                                            )}
                                        </div>
                                    ) : (
                                        res.uploadSuccess && autoShare && (
                                            <div style={{ fontSize: 12, color: '#faad14', marginTop: 4, paddingLeft: 8 }}>分享未能创建 (请前往分享管理重试)</div>
                                        )
                                    )}
                                </div>
                            ))}
                        </div>
                        <div style={{ marginTop: 16, textAlign: 'center', color: '#8c8c8c', fontSize: 12 }}>
                            提示：所有分享记录已同步到“分享管理”页面。
                        </div>
                    </div>
                ) : (
                    <Space direction="vertical" style={{ width: '100%' }} size="middle">
                        <div style={{ background: '#f5f5f5', padding: '12px', borderRadius: '8px', marginBottom: 16 }}>
                            <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                                <Space>
                                    <span style={{ fontWeight: 500 }}>上传类型：</span>
                                    <Radio.Group
                                        value={isFolderUpload}
                                        onChange={e => {
                                            setIsFolderUpload(e.target.value)
                                            setFileList([]) // 切换类型时清空文件列表
                                        }}
                                        disabled={uploading}
                                        optionType="button"
                                        buttonStyle="solid"
                                    >
                                        <Radio.Button value={false}>单文件</Radio.Button>
                                        <Radio.Button value={true}>文件夹</Radio.Button>
                                    </Radio.Group>
                                </Space>
                            </div>

                            <Upload
                                beforeUpload={(file) => {
                                    // 过滤系统垃圾文件
                                    const junkFiles = ['.DS_Store', 'Thumbs.db', 'desktop.ini', '__MACOSX']
                                    if (junkFiles.includes(file.name) || file.name.startsWith('._')) {
                                        return Upload.LIST_IGNORE;
                                    }

                                    if (isFolderUpload) {
                                        setFileList(prev => [...prev, file])
                                    } else {
                                        setFileList([file])
                                    }
                                    return false
                                }}
                                fileList={fileList}
                                onRemove={(file) => {
                                    const index = fileList.indexOf(file);
                                    const newFileList = fileList.slice();
                                    newFileList.splice(index, 1);
                                    setFileList(newFileList);
                                }}
                                directory={isFolderUpload}
                                multiple={isFolderUpload}
                                disabled={uploading}
                            >
                                <Button icon={<UploadOutlined />} disabled={uploading}>
                                    {isFolderUpload ? '选择文件夹' : '选择文件'}
                                </Button>
                            </Upload>
                            {isFolderUpload && fileList.length > 0 && (
                                <div style={{ marginTop: 8, fontSize: 12, color: '#666' }}>
                                    已选择 {fileList.length} 个文件
                                </div>
                            )}
                        </div>

                        <div>
                            <div style={{ marginBottom: 8 }}>选择目标网盘：</div>
                            <Checkbox.Group
                                value={uploadTargets}
                                onChange={setUploadTargets}
                                style={{ width: '100%' }}
                                disabled={uploading}
                            >
                                <Space direction="vertical" style={{ width: '100%' }}>
                                    {accounts.map(account => (
                                        <div key={account.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                            <Checkbox value={account.id}>
                                                {DISK_TYPES[account.type]?.icon} {account.name}
                                            </Checkbox>

                                            {uploadTargets.includes(account.id) && (
                                                <div style={{ flex: 1, marginLeft: 16 }}>
                                                    <div style={{ display: 'flex', alignItems: 'center', marginBottom: 4 }}>
                                                        <div style={{ flex: 1, maxWidth: 320 }}>
                                                            <FolderPicker
                                                                accountId={account.id}
                                                                placeholder={`选择目录(默认: 根目录)`}
                                                                value={uploadPaths[account.id]?.fid === '0' ? undefined : [uploadPaths[account.id]?.fid]}
                                                                onChange={(info) => handleFolderChange(account.id, info)}
                                                                disabled={uploading}
                                                            />
                                                        </div>
                                                        <div style={{ marginLeft: 10, width: 100, display: 'flex', alignItems: 'center' }}>
                                                            {targetStatus[String(account.id)] === 'uploading' && (
                                                                <div style={{ fontSize: 11, color: '#999', lineHeight: '14px' }}>
                                                                    {targetStage[String(account.id)] === 2 ? (
                                                                        <span style={{ color: '#52c41a' }}>同步到网盘... {targetProgress[String(account.id)] || 0}%</span>
                                                                    ) : (
                                                                        <span style={{ color: '#1890ff' }}>传输中... {sharedUploadProgress}%</span>
                                                                    )}
                                                                </div>
                                                            )}
                                                            {targetStatus[String(account.id)] === 'success' && (
                                                                <div style={{ fontSize: 11, color: '#52c41a' }}>上传成功</div>
                                                            )}
                                                            {targetStatus[String(account.id)] === 'error' && (
                                                                <div style={{ fontSize: 11, color: '#ff4d4f' }}>上传失败</div>
                                                            )}
                                                        </div>
                                                    </div>
                                                    {targetStatus[String(account.id)] && targetStatus[String(account.id)] !== 'waiting' && (
                                                        <div style={{ marginTop: 4 }}>
                                                            {targetStatus[String(account.id)] === 'uploading' && (
                                                                <div style={{ fontSize: 10, color: '#1890ff', marginBottom: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                                    正在处理: {targetFilenames[String(account.id)] || '查询中...'}
                                                                </div>
                                                            )}
                                                            <Progress
                                                                percent={targetStage[String(account.id)] === 2 ? (targetProgress[String(account.id)] || 0) : sharedUploadProgress}
                                                                size="small"
                                                                status={targetStatus[String(account.id)] === 'error' ? 'exception' : (targetStatus[String(account.id)] === 'success' ? 'success' : 'active')}
                                                                strokeColor={targetStage[String(account.id)] === 2 ? '#52c41a' : '#1890ff'}
                                                                showInfo={false}
                                                            />
                                                        </div>
                                                    )}
                                                </div>
                                            )}
                                        </div>
                                    ))}
                                </Space>
                            </Checkbox.Group>
                        </div>

                        <Divider style={{ margin: '8px 0' }} />

                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <Space>
                                <span style={{ fontWeight: 'bold' }}>上传后自动分享:</span>
                                <Switch checked={autoShare} onChange={setAutoShare} disabled={uploading} />
                            </Space>
                            {autoShare && (
                                <Space>
                                    <span>分享时长:</span>
                                    <Select
                                        size="small"
                                        style={{ width: 100 }}
                                        value={autoShareExpiredType}
                                        onChange={setAutoShareExpiredType}
                                        disabled={uploading}
                                    >
                                        <Select.Option value={1}>永久</Select.Option>
                                        <Select.Option value={3}>1天</Select.Option>
                                        <Select.Option value={2}>7天</Select.Option>
                                        <Select.Option value={4}>30天</Select.Option>
                                    </Select>
                                </Space>
                            )}
                        </div>

                        {(uploading || uploadProgress > 0) && (
                            <div style={{ marginTop: 8 }}>
                                <div style={{ marginBottom: 4, display: 'flex', justifyContent: 'space-between' }}>
                                    <span>正在上传：</span>
                                    <span style={{ color: '#1890ff', maxWidth: '75%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {currentFile}
                                    </span>
                                </div>
                                <div style={{ marginBottom: 4, display: 'flex', justifyContent: 'space-between' }}>
                                    <span>总进度：</span>
                                    <span style={{ fontWeight: 'bold' }}>{uploadProgress}%</span>
                                </div>
                                <Progress
                                    percent={uploadProgress}
                                    status={uploading ? 'active' : 'success'}
                                    strokeColor={{
                                        '0%': '#108ee9',
                                        '100%': '#87d068',
                                    }}
                                />
                            </div>
                        )}

                        <div style={{ color: '#999', fontSize: 12 }}>
                            * 进度反映实时传输状态，完成后将自动标记为“已同步”。
                        </div>
                    </Space>
                )}
            </Modal>
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
                            {countdown > 0 ? `${countdown} 秒后重发` : '发送验证码'}
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


        </div >
    )
}
