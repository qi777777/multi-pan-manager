import { useState, useEffect } from 'react'
import {
    Card,
    Table,
    Select,
    Button,
    Space,
    message,
    Popconfirm,
    Modal,
    Form,
    Input,
    Tag,
    Typography,
    Tooltip
} from 'antd'
import {
    DatabaseOutlined,
    EditOutlined,
    DeleteOutlined,
    ReloadOutlined,
    ExclamationCircleOutlined,
    CodeOutlined,
    PlayCircleOutlined
} from '@ant-design/icons'
import api from '../services/api'

const { Text } = Typography
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export default function DatabasePage() {
    const [tables, setTables] = useState([])
    const [metadata, setMetadata] = useState({})
    const [currentTable, setCurrentTable] = useState(null)
    const [data, setData] = useState([])
    const [loading, setLoading] = useState(false)
    const [total, setTotal] = useState(0)
    const [pagination, setPagination] = useState({ current: 1, pageSize: 20 })
    const [selectedRowKeys, setSelectedRowKeys] = useState([])

    const [editModalVisible, setEditModalVisible] = useState(false)
    const [editingRecord, setEditingRecord] = useState(null)
    const [form] = Form.useForm()

    useEffect(() => {
        fetchInitialData()
    }, [])

    const [sqlConsoleVisible, setSqlConsoleVisible] = useState(false)
    const [sqlText, setSqlText] = useState('')
    const [sqlResult, setSqlResult] = useState(null)
    const [sqlExecuting, setSqlExecuting] = useState(false)

    useEffect(() => {
        if (currentTable && currentTable !== 'null') {
            fetchData()
        }
    }, [currentTable, pagination.current, pagination.pageSize])

    const fetchInitialData = async () => {
        try {
            const [tablesRes, metaRes] = await Promise.all([
                api.get('/system/db/tables'),
                api.get('/system/db/metadata')
            ])
            setTables(tablesRes.data)
            setMetadata(metaRes.data)

            if (tablesRes.data.length > 0 && !currentTable) {
                setCurrentTable(tablesRes.data[0])
            }
        } catch (err) {
            message.error('初始化数据失败')
        }
    }

    const fetchData = async () => {
        if (!currentTable || currentTable === 'null') return
        setLoading(true)
        setSelectedRowKeys([]) // 切换表或刷新时清除选择
        try {
            const skip = (pagination.current - 1) * pagination.pageSize
            const res = await api.get(`/system/db/${currentTable}`, {
                params: { skip, limit: pagination.pageSize }
            })
            setData(res.data.data)
            setTotal(res.data.total)
        } catch (err) {
            message.error(`获取表 ${currentTable} 数据失败`)
        } finally {
            setLoading(false)
        }
    }

    const handleExecuteSql = async () => {
        if (!sqlText.trim()) return
        setSqlExecuting(true)
        try {
            const res = await api.post('/system/db/execute-sql', { sql: sqlText })
            setSqlResult(res.data)
            message.success('SQL 执行成功')
        } catch (err) {
            message.error(err.response?.data?.detail || 'SQL 执行出错')
        } finally {
            setSqlExecuting(false)
        }
    }

    const handleDelete = async (id) => {
        try {
            await api.delete(`/system/db/${currentTable}/${id}`)
            message.success('删除成功')
            fetchData()
        } catch (err) {
            message.error('删除失败')
        }
    }

    const handleBatchDelete = async () => {
        if (selectedRowKeys.length === 0) return

        try {
            await api.post(`/system/db/${currentTable}/batch-delete`, {
                ids: selectedRowKeys
            })
            message.success(`成功删除 ${selectedRowKeys.length} 条记录`)
            setSelectedRowKeys([])
            fetchData()
        } catch (err) {
            message.error('批量删除失败')
        }
    }

    const handleEdit = (record) => {
        setEditingRecord(record)
        form.setFieldsValue(record)
        setEditModalVisible(true)
    }

    const handleUpdate = async () => {
        try {
            const values = await form.validateFields()
            await api.put(`/system/db/${currentTable}/${editingRecord.id}`, values)
            message.success('更新成功')
            setEditModalVisible(false)
            fetchData()
        } catch (err) {
            message.error('更新失败')
        }
    }

    const onSelectChange = (newSelectedRowKeys) => {
        setSelectedRowKeys(newSelectedRowKeys)
    }

    const rowSelection = {
        selectedRowKeys,
        onChange: onSelectChange,
    }

    // 获取字段中文名
    const getColumnTitle = (key) => {
        const tableMeta = metadata[currentTable]
        if (tableMeta && tableMeta.fields && tableMeta.fields[key]) {
            return tableMeta.fields[key]
        }
        return key
    }

    const columns = data.length > 0 ? Object.keys(data[0]).map(key => {
        const titleCn = getColumnTitle(key);
        // 如果中文名不等于英文名，就组合显示
        const displayTitle = titleCn !== key ? `${titleCn} (${key})` : key;

        return {
            title: (
                <Tooltip title={key}>
                    <span>{displayTitle}</span>
                </Tooltip>
            ),
            dataIndex: key,
            key: key,
            ellipsis: true,
            width: key === 'id' ? 80 : 180,
            render: (text) => {
                // 尝试查找枚举映射
                const tableMeta = metadata[currentTable]
                if (tableMeta && tableMeta.enums && tableMeta.enums[key]) {
                    const enumMap = tableMeta.enums[key]
                    // 兼容数字和字符串类型的 key
                    const label = enumMap[text] || enumMap[String(text)]
                    if (label) {
                        const color = label.includes('正常') || label.includes('有效') || label.includes('成功') ? 'success' :
                            label.includes('过期') || label.includes('失效') || label.includes('失败') || label.includes('取消') ? 'error' :
                                label.includes('进行中') || label.includes('中') ? 'processing' : 'default'
                        return <Tag color={color}>{label}</Tag>
                    }
                }

                if (typeof text === 'object' && text !== null) return JSON.stringify(text)
                if (typeof text === 'string' && text.length > 100) return text.substring(0, 100) + '...'
                return String(text ?? '')
            }
        }
    }).concat([{
        title: '操作',
        key: 'action',
        fixed: 'right',
        width: 120,
        render: (_, record) => (
            <Space>
                <Button size="small" icon={<EditOutlined />} onClick={() => handleEdit(record)} />
                <Popconfirm title="确定删除此记录？" onConfirm={() => handleDelete(record.id)}>
                    <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
            </Space>
        )
    }]) : []

    const tableCnName = metadata[currentTable]?.name || currentTable

    return (
        <div>
            <div className="page-header">
                <h2>数据库管理</h2>
            </div>

            <Card style={{ marginBottom: 16 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
                    <Space size="large">
                        <Space>
                            <Text strong>数据表：</Text>
                            <Select
                                style={{ minWidth: 260 }}
                                popupMatchSelectWidth={false}
                                value={currentTable}
                                onChange={(val) => {
                                    setCurrentTable(val)
                                    setPagination({ ...pagination, current: 1 })
                                }}
                            >
                                {tables.map(t => (
                                    <Select.Option key={t} value={t}>
                                        {metadata[t]?.name ? `${metadata[t].name} (${t})` : t}
                                    </Select.Option>
                                ))}
                            </Select>
                        </Space>
                        <Button icon={<ReloadOutlined />} onClick={fetchData}>刷新数据</Button>
                        <Button
                            icon={<CodeOutlined />}
                            type={sqlConsoleVisible ? 'primary' : 'default'}
                            onClick={() => setSqlConsoleVisible(!sqlConsoleVisible)}
                        >
                            SQL 控制台
                        </Button>
                        <Tag icon={<DatabaseOutlined />} color="blue" style={{ padding: '2px 10px' }}>
                            当前表: {tableCnName} | 总数: {total} 条
                        </Tag>
                    </Space>

                    <Space>
                        {selectedRowKeys.length > 0 && (
                            <Popconfirm
                                title={`确定批量删除选中的 ${selectedRowKeys.length} 条记录？`}
                                onConfirm={handleBatchDelete}
                                icon={<ExclamationCircleOutlined style={{ color: 'red' }} />}
                            >
                                <Button type="primary" danger icon={<DeleteOutlined />}>
                                    批量删除 ({selectedRowKeys.length})
                                </Button>
                            </Popconfirm>
                        )}
                    </Space>
                </div>
            </Card>

            {sqlConsoleVisible && (
                <Card title="SQL 控制台" style={{ marginBottom: 16 }} styles={{ body: { padding: 12 } }}>
                    <Space direction="vertical" style={{ width: '100%' }}>
                        <Input.TextArea
                            placeholder="请输入 SQL 语句... (例如: SELECT * FROM disk_accounts WHERE type = 1)"
                            rows={4}
                            value={sqlText}
                            onChange={e => setSqlText(e.target.value)}
                            style={{ fontFamily: 'monospace' }}
                        />
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <Space>
                                <Button type="primary" icon={<PlayCircleOutlined />} loading={sqlExecuting} onClick={handleExecuteSql}>
                                    运行 SQL
                                </Button>
                                <Button onClick={() => { setSqlResult(null); setSqlText('') }}>清空</Button>
                            </Space>
                            {sqlResult?.affected_rows !== undefined && (
                                <Text type="success">执行成功，受影响行数: {sqlResult.affected_rows}</Text>
                            )}
                        </div>

                        {sqlResult?.type === 'query' && (
                            <div style={{ marginTop: 12 }}>
                                <Table
                                    size="small"
                                    dataSource={sqlResult.data}
                                    columns={sqlResult.columns.map(col => ({
                                        title: col,
                                        dataIndex: col,
                                        key: col,
                                        ellipsis: true,
                                        render: (val) => typeof val === 'object' ? JSON.stringify(val) : String(val ?? '')
                                    }))}
                                    pagination={{ pageSize: 10, size: 'small' }}
                                    scroll={{ x: 'max-content' }}
                                    bordered
                                />
                            </div>
                        )}
                    </Space>
                </Card>
            )}

            <Table
                dataSource={data}
                columns={columns}
                rowKey="id"
                loading={loading}
                rowSelection={rowSelection}
                pagination={{
                    ...pagination,
                    total: total,
                    showSizeChanger: true,
                    showTotal: (total) => `共 ${total} 条数据`,
                    onChange: (page, pageSize) => setPagination({ current: page, pageSize })
                }}
                scroll={{ x: 'max-content' }}
                size="middle"
                bordered
            />

            <Modal
                title={`编辑记录 - ${tableCnName} (ID: ${editingRecord?.id})`}
                open={editModalVisible}
                onOk={handleUpdate}
                onCancel={() => setEditModalVisible(false)}
                width={700}
                destroyOnHidden
            >
                <Form form={form} layout="vertical" style={{ maxHeight: '65vh', overflowY: 'auto', paddingRight: 12 }}>
                    {editingRecord && Object.keys(editingRecord).map(key => {
                        if (key === 'id') return null

                        const tableMeta = metadata[currentTable]
                        const fieldEnums = tableMeta?.enums?.[key]

                        return (
                            <Form.Item key={key} name={key} label={`${getColumnTitle(key)} (${key})`}>
                                {fieldEnums ? (
                                    <Select placeholder={`请选择 ${getColumnTitle(key)}`}>
                                        {Object.entries(fieldEnums).map(([value, label]) => (
                                            <Select.Option key={value} value={isNaN(value) ? value : Number(value)}>
                                                {label}
                                            </Select.Option>
                                        ))}
                                    </Select>
                                ) : (
                                    <Input.TextArea autoSize={{ minRows: 1, maxRows: 8 }} placeholder={`请输入 ${getColumnTitle(key)}`} />
                                )}
                            </Form.Item>
                        )
                    })}
                </Form>
            </Modal>
        </div>
    )
}
