import { useState, useEffect } from 'react'
import {
    Card, Table, Button, Modal, Form, Input, Select, Tag, Space, message, Popconfirm
} from 'antd'
import {
    PlusOutlined, CheckCircleOutlined, CloseCircleOutlined,
    ExclamationCircleOutlined, ReloadOutlined, DeleteOutlined, EditOutlined
} from '@ant-design/icons'
import { accountApi, DISK_TYPES } from '../services/api'

export default function AccountsPage() {
    const [accounts, setAccounts] = useState([])
    const [loading, setLoading] = useState(false)
    const [modalVisible, setModalVisible] = useState(false)
    const [editingAccount, setEditingAccount] = useState(null)
    const [form] = Form.useForm()

    useEffect(() => {
        fetchAccounts()
    }, [])

    const fetchAccounts = async () => {
        setLoading(true)
        try {
            const { data } = await accountApi.getList()
            setAccounts(data)
        } catch (error) {
            message.error('获取账户列表失败')
        } finally {
            setLoading(false)
        }
    }

    const handleAdd = () => {
        setEditingAccount(null)
        form.resetFields()
        setModalVisible(true)
    }

    const handleEdit = async (record) => {
        try {
            // 获取账户详情（包含凭证）
            const { data } = await accountApi.getDetail(record.id)
            setEditingAccount(data)
            form.setFieldsValue({
                name: data.name,
                type: data.type,
                credentials: data.credentials || ''  // 预填充凭证
            })
            setModalVisible(true)
        } catch (error) {
            message.error('获取账户详情失败')
        }
    }

    const handleSubmit = async () => {
        try {
            const values = await form.validateFields()
            if (editingAccount) {
                await accountApi.update(editingAccount.id, values)
                message.success('更新成功')
            } else {
                await accountApi.create(values)
                message.success('添加成功')
            }
            setModalVisible(false)
            fetchAccounts()
        } catch (error) {
            message.error(error.response?.data?.detail || '操作失败')
        }
    }

    const handleDelete = async (id) => {
        try {
            await accountApi.delete(id)
            message.success('删除成功')
            fetchAccounts()
        } catch (error) {
            message.error('删除失败')
        }
    }

    const handleCheck = async (id) => {
        try {
            const { data } = await accountApi.checkStatus(id)
            if (data.status === 1) {
                message.success(data.message)
            } else {
                message.warning(data.message)
            }
            fetchAccounts()
        } catch (error) {
            message.error('检测失败')
        }
    }

    const columns = [
        {
            title: '账户名称',
            dataIndex: 'name',
            key: 'name',
            render: (text, record) => (
                <Space>
                    <span>{DISK_TYPES[record.type]?.icon}</span>
                    <span>{text}</span>
                </Space>
            )
        },
        {
            title: '网盘类型',
            dataIndex: 'type',
            key: 'type',
            render: (type) => (
                <Tag color={DISK_TYPES[type]?.color}>{DISK_TYPES[type]?.name}</Tag>
            )
        },
        {
            title: '存储路径',
            dataIndex: 'storage_path',
            key: 'storage_path',
            ellipsis: true
        },
        {
            title: '状态',
            dataIndex: 'status',
            key: 'status',
            render: (status) => {
                const statusMap = {
                    0: { text: '已禁用', icon: <CloseCircleOutlined />, color: 'default' },
                    1: { text: '正常', icon: <CheckCircleOutlined />, color: 'success' },
                    2: { text: '凭证过期', icon: <ExclamationCircleOutlined />, color: 'warning' }
                }
                const s = statusMap[status] || statusMap[0]
                return <Tag icon={s.icon} color={s.color}>{s.text}</Tag>
            }
        },
        {
            title: '操作',
            key: 'action',
            render: (_, record) => (
                <Space>
                    <Button
                        type="link"
                        icon={<ReloadOutlined />}
                        onClick={() => handleCheck(record.id)}
                    >
                        检测
                    </Button>
                    <Button
                        type="link"
                        icon={<EditOutlined />}
                        onClick={() => handleEdit(record)}
                    >
                        编辑
                    </Button>
                    <Popconfirm
                        title="确定删除该账户？"
                        onConfirm={() => handleDelete(record.id)}
                    >
                        <Button type="link" danger icon={<DeleteOutlined />}>
                            删除
                        </Button>
                    </Popconfirm>
                </Space>
            )
        }
    ]

    return (
        <div>
            <div className="page-header">
                <h2>账户管理</h2>
            </div>

            <Card
                extra={
                    <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>
                        添加账户
                    </Button>
                }
            >
                <Table
                    columns={columns}
                    dataSource={accounts}
                    rowKey="id"
                    loading={loading}
                    pagination={false}
                />
            </Card>

            <Modal
                title={editingAccount ? '编辑账户' : '添加账户'}
                open={modalVisible}
                onOk={handleSubmit}
                onCancel={() => setModalVisible(false)}
                width={600}
            >
                <Form form={form} layout="vertical">
                    <Form.Item
                        name="name"
                        label="账户名称"
                        rules={[{ required: true, message: '请输入账户名称' }]}
                    >
                        <Input placeholder="例如：我的夸克网盘" />
                    </Form.Item>

                    <Form.Item
                        name="type"
                        label="网盘类型"
                        rules={[{ required: true, message: '请选择网盘类型' }]}
                    >
                        <Select placeholder="请选择网盘类型" disabled={!!editingAccount}>
                            {Object.entries(DISK_TYPES).map(([key, val]) => (
                                <Select.Option key={key} value={Number(key)}>
                                    {val.icon} {val.name}
                                </Select.Option>
                            ))}
                        </Select>
                    </Form.Item>

                    <Form.Item
                        name="credentials"
                        label="凭证 (Cookie/Token)"
                        rules={[{ required: !editingAccount, message: '请输入凭证' }]}
                        extra="夸克/UC/百度使用Cookie，阿里/迅雷使用refresh_token"
                    >
                        <Input.TextArea
                            rows={4}
                            placeholder={editingAccount ? '留空表示不修改' : '请输入Cookie或Token'}
                        />
                    </Form.Item>
                </Form>
            </Modal>
        </div>
    )
}
