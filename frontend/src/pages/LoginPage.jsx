import { useState } from 'react'
import { Form, Input, Button, Card, Typography, Space } from 'antd'
import { UserOutlined, LockOutlined, CloudOutlined } from '@ant-design/icons'
import { useAuth } from '../contexts/AuthContext'
import { useNavigate, useLocation } from 'react-router-dom'
import { hashPassword } from '../utils/crypto'

const { Title, Text } = Typography

export default function LoginPage() {
    const [loading, setLoading] = useState(false)
    const { login } = useAuth()
    const navigate = useNavigate()
    const location = useLocation()

    // 登录后重定向到之前的页面，或者根路径
    const from = location.state?.from?.pathname || '/'

    const onFinish = async (values) => {
        setLoading(true)
        try {
            // 前端预哈希，支持 Secure 和 Non-Secure 上下文
            const hashedPassword = await hashPassword(values.password)
            const success = await login(values.username, hashedPassword)
            if (success) {
                navigate(from, { replace: true })
            }
        } catch (error) {
            console.error('Login failed:', error)
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="login-container">
            <div className="login-glass-card">
                <Card variant="borderless" style={{ background: 'transparent' }}>
                    <div className="login-header">
                        <div className="login-logo">
                            <CloudOutlined style={{ fontSize: 40, color: '#1890ff' }} />
                        </div>
                        <Title level={2} style={{ marginBottom: 0 }}>多网盘协同管理</Title>
                        <Text type="secondary">请登录以访问控制台</Text>
                    </div>

                    <Form
                        name="login"
                        onFinish={onFinish}
                        layout="vertical"
                        size="large"
                        style={{ marginTop: 24 }}
                    >
                        <Form.Item
                            name="username"
                            rules={[{ required: true, message: '请输入用户名' }]}
                        >
                            <Input
                                prefix={<UserOutlined style={{ color: '#bfbfbf' }} />}
                                placeholder="用户名"
                            />
                        </Form.Item>

                        <Form.Item
                            name="password"
                            rules={[{ required: true, message: '请输入密码' }]}
                        >
                            <Input.Password
                                prefix={<LockOutlined style={{ color: '#bfbfbf' }} />}
                                placeholder="密码"
                            />
                        </Form.Item>

                        <Form.Item>
                            <Button
                                type="primary"
                                htmlType="submit"
                                loading={loading}
                                block
                                style={{ height: 45, fontSize: 16, borderRadius: 8 }}
                            >
                                立即登录
                            </Button>
                        </Form.Item>
                    </Form>

                    <div style={{ textAlign: 'center', marginTop: 12 }}>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                            默认账户：admin / admin123
                        </Text>
                    </div>
                </Card>
            </div>

            <style jsx="true">{`
                .login-container {
                    width: 100vw;
                    height: 100vh;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    background: radial-gradient(circle at top left, #1a1a2e, #16213e);
                    position: relative;
                    overflow: hidden;
                }

                .login-container::before {
                    content: '';
                    position: absolute;
                    width: 40%;
                    height: 40%;
                    background: radial-gradient(circle, rgba(24, 144, 255, 0.15) 0%, transparent 70%);
                    top: -10%;
                    left: -10%;
                    filter: blur(50px);
                }

                .login-container::after {
                    content: '';
                    position: absolute;
                    width: 30%;
                    height: 30%;
                    background: radial-gradient(circle, rgba(82, 196, 26, 0.1) 0%, transparent 70%);
                    bottom: -5%;
                    right: -5%;
                    filter: blur(50px);
                }

                .login-glass-card {
                    width: 400px;
                    padding: 20px;
                    background: rgba(255, 255, 255, 0.05);
                    backdrop-filter: blur(20px);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 20px;
                    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                    z-index: 10;
                }

                .login-header {
                    text-align: center;
                    margin-bottom: 0;
                }

                .login-logo {
                    width: 70px;
                    height: 70px;
                    background: rgba(255, 255, 255, 0.05);
                    border-radius: 18px;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    margin: 0 auto 16px;
                    border: 1px solid rgba(255, 255, 255, 0.1);
                }

                :global(.ant-typography) {
                    color: rgba(255, 255, 255, 0.9) !important;
                }

                :global(.ant-typography-secondary) {
                    color: rgba(255, 255, 255, 0.45) !important;
                }

                :global(.ant-input-affix-wrapper), :global(.ant-input) {
                    background: rgba(255, 255, 255, 0.05) !important;
                    border-color: rgba(255, 255, 255, 0.1) !important;
                    color: white !important;
                }

                :global(.ant-input::placeholder) {
                    color: rgba(255, 255, 255, 0.25) !important;
                }

                :global(.ant-form-item-label label) {
                    color: rgba(255, 255, 255, 0.85) !important;
                }
            `}</style>
        </div>
    )
}
