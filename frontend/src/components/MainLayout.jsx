import { useState } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Button, Avatar, Popover, Modal, Form, message, Input } from 'antd'
import {
    CloudServerOutlined,
    FolderOutlined,
    SwapOutlined,
    ShareAltOutlined,
    CloudOutlined,
    CloudSyncOutlined,
    ConsoleSqlOutlined,
    FieldTimeOutlined,
    LogoutOutlined,
    UserOutlined,
    EditOutlined
} from '@ant-design/icons'
import { useAuth } from '../contexts/AuthContext'
import api from '../services/api'
import { hashPassword } from '../utils/crypto'

const { Sider, Content } = Layout

const menuItems = [
    { key: '/accounts', icon: <CloudServerOutlined />, label: '账户管理' },
    { key: '/files', icon: <FolderOutlined />, label: '文件管理' },
    { key: '/transfer', icon: <SwapOutlined />, label: '转存工具' },
    { key: '/cross-transfer', icon: <CloudSyncOutlined />, label: '网盘互传' },
    { key: '/shares', icon: <ShareAltOutlined />, label: '分享管理' },
    { key: '/logs', icon: <FieldTimeOutlined />, label: '实时日志' },
    { key: '/database', icon: <ConsoleSqlOutlined />, label: '数据库管理' }
]

export default function MainLayout() {
    const navigate = useNavigate()
    const location = useLocation()
    const [collapsed, setCollapsed] = useState(false)
    const { user, logout } = useAuth()

    const [passwordModalVisible, setPasswordModalVisible] = useState(false)
    const [passwordForm] = Form.useForm()
    const [updatingPassword, setUpdatingPassword] = useState(false)

    const handleMenuClick = ({ key }) => navigate(key)

    const handleLogout = () => {
        logout()
        navigate('/login')
    }

    const handleUpdatePassword = async () => {
        try {
            const values = await passwordForm.validateFields()
            setUpdatingPassword(true)

            const payload = {
                old_password: await hashPassword(values.old_password),
                new_password: await hashPassword(values.new_password)
            }

            await api.put('/auth/password', payload)

            message.success('密码更新成功，请重新登录')
            setPasswordModalVisible(false)
            handleLogout()
        } catch (err) {
            if (err.response) {
                message.error(err.response.data.detail || '更新失败')
            } else {
                message.error('更新失败，请检查网络或稍后再试')
            }
        } finally {
            setUpdatingPassword(false)
        }
    }

    return (
        <Layout className="main-layout" style={{ minHeight: '100vh' }}>
            <Sider
                collapsible
                collapsed={collapsed}
                onCollapse={setCollapsed}
                width={200}
                style={{
                    overflow: 'hidden',
                    height: '100vh',
                    position: 'fixed',
                    left: 0, top: 0, bottom: 0,
                    background: 'linear-gradient(180deg, #1a1a2e 0%, #16213e 100%)',
                    display: 'flex',
                    flexDirection: 'column'
                }}
            >
                <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
                    <div className="logo" style={{ padding: '16px', color: 'white', display: 'flex', alignItems: 'center', gap: 12 }}>
                        <img src="/logo.png" alt="Logo" style={{ width: 32, height: 32, borderRadius: 6 }} />
                        {!collapsed && <span style={{ fontWeight: 'bold', fontSize: 16, whiteSpace: 'nowrap' }}>Multi-Pan Manager</span>}
                    </div>

                    <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden' }}>
                        <Menu
                            theme="dark"
                            mode="inline"
                            selectedKeys={[location.pathname]}
                            items={menuItems}
                            onClick={handleMenuClick}
                            style={{ background: 'transparent', borderRight: 0 }}
                        />
                    </div>

                    <div style={{ padding: '16px', borderTop: '1px solid rgba(255,255,255,0.05)', background: 'rgba(0,0,0,0.1)' }}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: collapsed ? 'center' : 'space-between' }}>
                            {!collapsed ? (
                                <Popover
                                    placement="rightBottom"
                                    trigger="click"
                                    content={
                                        <div style={{ width: 120 }}>
                                            <Button type="text" block onClick={() => setPasswordModalVisible(true)} icon={<EditOutlined />}>修改密码</Button>
                                            <Button type="text" block danger onClick={handleLogout} icon={<LogoutOutlined />}>退出登录</Button>
                                        </div>
                                    }
                                >
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, overflow: 'hidden', cursor: 'pointer' }}>
                                        <Avatar size="small" icon={<UserOutlined />} style={{ backgroundColor: '#1890ff', flexShrink: 0 }} />
                                        <span style={{ color: 'rgba(255,255,255,0.85)', fontSize: 13, textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }}>
                                            {user?.username || 'Admin'}
                                        </span>
                                    </div>
                                </Popover>
                            ) : (
                                <Avatar size="small" icon={<UserOutlined />} style={{ backgroundColor: '#1890ff', cursor: 'pointer' }} onClick={handleLogout} />
                            )}
                            {!collapsed && (
                                <Button
                                    type="text"
                                    icon={<LogoutOutlined />}
                                    onClick={handleLogout}
                                    style={{ color: 'rgba(255,255,255,0.45)', padding: '0 4px' }}
                                    title="退出登录"
                                />
                            )}
                        </div>
                    </div>
                </div>
            </Sider>

            <Modal
                title="修改管理员密码"
                open={passwordModalVisible}
                onOk={handleUpdatePassword}
                onCancel={() => setPasswordModalVisible(false)}
                confirmLoading={updatingPassword}
                destroyOnHidden
            >
                <Form form={passwordForm} layout="vertical">
                    <Form.Item
                        name="old_password"
                        label="原密码"
                        rules={[{ required: true, message: '请输入原密码' }]}
                    >
                        <Input.Password placeholder="请输入当前使用的密码" />
                    </Form.Item>
                    <Form.Item
                        name="new_password"
                        label="新密码"
                        rules={[
                            { required: true, message: '请输入新密码' },
                            { min: 6, message: '密码长度至少为 6 位' }
                        ]}
                    >
                        <Input.Password placeholder="请输入新密码" />
                    </Form.Item>
                    <Form.Item
                        name="confirm_password"
                        label="确认新密码"
                        dependencies={['new_password']}
                        rules={[
                            { required: true, message: '请确认新密码' },
                            ({ getFieldValue }) => ({
                                validator(_, value) {
                                    if (!value || getFieldValue('new_password') === value) {
                                        return Promise.resolve();
                                    }
                                    return Promise.reject(new Error('两次输入的密码不一致'));
                                },
                            }),
                        ]}
                    >
                        <Input.Password placeholder="请再次输入新密码" />
                    </Form.Item>
                </Form>
            </Modal>
            <Layout style={{ marginLeft: collapsed ? 80 : 200, transition: 'margin-left 0.2s', background: '#f0f2f5' }}>
                <Content className="content-wrapper" style={{ padding: 24, minHeight: 280 }}>
                    <Outlet />
                </Content>
            </Layout>
        </Layout>
    )
}
