import { useState, useEffect, useRef } from 'react'
import { Card, Input, Space, Button, Select, Tag, Typography, Empty } from 'antd'
import {
    DeleteOutlined,
    PauseCircleOutlined,
    PlayCircleOutlined,
    SearchOutlined,
    CodeOutlined
} from '@ant-design/icons'

const { Text } = Typography

export default function LogsPage() {
    const [logs, setLogs] = useState([])
    const [isPaused, setIsPaused] = useState(false)
    const [searchText, setSearchText] = useState('')
    const [filterLevel, setFilterLevel] = useState(null)
    const [autoScroll, setAutoScroll] = useState(true)

    const logsEndRef = useRef(null)
    const eventSourceRef = useRef(null)

    useEffect(() => {
        // 连接 SSE 日志流
        const token = localStorage.getItem('token')
        const url = `${import.meta.env.VITE_API_URL || ''}/api/system/logs?token_query=${token}&t=${new Date().getTime()}`
        const eventSource = new EventSource(url)
        eventSourceRef.current = eventSource

        eventSource.onmessage = (event) => {
            if (isPaused) return

            try {
                const logData = JSON.parse(event.data)
                setLogs(prev => {
                    const next = [...prev, logData]
                    return next.slice(-1000) // 最多保留1000条记录
                })
            } catch (e) {
                // 非 JSON 格式直接作为文本
                setLogs(prev => [...prev.slice(-999), { message: event.data, level: 'INFO', timestamp: new Date().toISOString() }])
            }
        }

        eventSource.onerror = (err) => {
            console.error("SSE Error:", err)
        }

        return () => {
            eventSource.close()
        }
    }, [isPaused])

    useEffect(() => {
        if (autoScroll && logsEndRef.current) {
            logsEndRef.current.scrollIntoView({ behavior: 'smooth' })
        }
    }, [logs, autoScroll])

    const getLevelColor = (level) => {
        switch (level?.toUpperCase()) {
            case 'ERROR': return 'red'
            case 'WARNING': return 'orange'
            case 'DEBUG': return 'blue'
            default: return 'green'
        }
    }

    const filteredLogs = logs.filter(log => {
        const matchesSearch = !searchText ||
            log.message?.toLowerCase().includes(searchText.toLowerCase()) ||
            log.module?.toLowerCase().includes(searchText.toLowerCase())
        const matchesLevel = !filterLevel || log.level === filterLevel
        return matchesSearch && matchesLevel
    })

    return (
        <div>
            <div className="page-header">
                <h2>实时日志</h2>
            </div>

            <Card styles={{ body: { padding: 12 } }} style={{ marginBottom: 16 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
                    <Space wrap>
                        <Input
                            placeholder="搜索日志内容..."
                            prefix={<SearchOutlined />}
                            style={{ width: 250 }}
                            value={searchText}
                            onChange={e => setSearchText(e.target.value)}
                            allowClear
                        />
                        <Select
                            placeholder="级别"
                            style={{ width: 100 }}
                            allowClear
                            value={filterLevel}
                            onChange={setFilterLevel}
                        >
                            <Select.Option value="INFO">INFO</Select.Option>
                            <Select.Option value="DEBUG">DEBUG</Select.Option>
                            <Select.Option value="WARNING">WARNING</Select.Option>
                            <Select.Option value="ERROR">ERROR</Select.Option>
                        </Select>
                        <Button
                            icon={isPaused ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
                            onClick={() => setIsPaused(!isPaused)}
                        >
                            {isPaused ? '恢复推送' : '暂停推送'}
                        </Button>
                        <Button
                            icon={<DeleteOutlined />}
                            onClick={() => setLogs([])}
                        >
                            重置
                        </Button>
                    </Space>
                    <Space>
                        <Tag color={autoScroll ? 'blue' : 'default'} style={{ cursor: 'pointer' }} onClick={() => setAutoScroll(!autoScroll)}>
                            {autoScroll ? '自动滚动已开启' : '自动滚动已关闭'}
                        </Tag>
                        <Text type="secondary">内存记录: {logs.length} 条</Text>
                    </Space>
                </div>
            </Card>

            <div style={{
                background: '#1a1a1a',
                color: '#d4d4d4',
                borderRadius: 8,
                padding: '16px 20px',
                height: 'calc(100vh - 250px)',
                overflowY: 'auto',
                fontFamily: 'Consolas, "Courier New", monospace',
                fontSize: '13px',
                lineHeight: '1.6'
            }}>
                {filteredLogs.length === 0 ? (
                    <Empty description={<Text style={{ color: '#666' }}>等待日志输入...</Text>} image={Empty.PRESENTED_IMAGE_SIMPLE} />
                ) : (
                    filteredLogs.map((log, index) => (
                        <div key={index} style={{ marginBottom: 4, display: 'flex', borderBottom: '1px solid #2a2a2a', paddingBottom: 2 }}>
                            <span style={{ color: '#888', marginRight: 12, flexShrink: 0 }}>
                                {log.timestamp?.split('T')[1].split('.')[0]}
                            </span>
                            <span style={{ color: getLevelColor(log.level), fontWeight: 'bold', minWidth: 60, flexShrink: 0 }}>
                                [{log.level}]
                            </span>
                            <span style={{ color: '#569cd6', marginRight: 12, flexShrink: 0 }}>
                                [{log.module || 'app'}]
                            </span>
                            <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                                {log.message}
                            </span>
                        </div>
                    ))
                )}
                <div ref={logsEndRef} />
            </div>
        </div>
    )
}
