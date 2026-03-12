import React, { useState, useEffect } from 'react';
import { Modal, Tree, message, Breadcrumb, Table, Button, Space } from 'antd';
import { FolderOutlined, HomeOutlined, ArrowUpOutlined } from '@ant-design/icons';
import { fileApi } from '../services/api';

export default function FolderSelector({ open, accountId, onCancel, onSelect }) {
    const [pathStack, setPathStack] = useState([{ fid: '0', name: '根目录' }]);
    const [folders, setFolders] = useState([]);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        if (open && accountId) {
            setPathStack([{ fid: '0', name: '根目录' }]);
            fetchFolders('0');
        }
    }, [open, accountId]);

    const fetchFolders = async (pdirFid) => {
        setLoading(true);
        try {
            const { data } = await fileApi.getList(accountId, pdirFid);
            // 只显示文件夹
            setFolders(data.filter(item => item.is_dir));
        } catch (error) {
            message.error('加载目录失败');
        } finally {
            setLoading(false);
        }
    };

    const handleFolderClick = (record) => {
        const newStack = [...pathStack, { fid: record.fid, name: record.name }];
        setPathStack(newStack);
        fetchFolders(record.fid);
    };

    const handleBreadcrumbClick = (index) => {
        const newStack = pathStack.slice(0, index + 1);
        setPathStack(newStack);
        fetchFolders(newStack[newStack.length - 1].fid);
    };

    const handleBack = () => {
        if (pathStack.length > 1) {
            handleBreadcrumbClick(pathStack.length - 2);
        }
    };

    const currentFolder = pathStack[pathStack.length - 1];

    const columns = [
        {
            title: '文件夹名称',
            dataIndex: 'name',
            key: 'name',
            render: (text) => (
                <Space>
                    <FolderOutlined style={{ color: '#faad14' }} />
                    {text}
                </Space>
            )
        }
    ];

    return (
        <Modal
            title="选择上传目录"
            open={open}
            onCancel={onCancel}
            onOk={() => onSelect(currentFolder)}
            width={600}
        >
            <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center' }}>
                <Button
                    icon={<ArrowUpOutlined />}
                    disabled={pathStack.length <= 1}
                    onClick={handleBack}
                    style={{ marginRight: 8 }}
                />
                <Breadcrumb
                    items={pathStack.map((item, index) => ({
                        key: item.fid,
                        title: (
                            <span
                                onClick={() => handleBreadcrumbClick(index)}
                                style={{ cursor: 'pointer' }}
                            >
                                {index === 0 ? <HomeOutlined /> : item.name}
                            </span>
                        )
                    }))}
                />
            </div>

            <Table
                columns={columns}
                dataSource={folders}
                rowKey="fid"
                loading={loading}
                pagination={false}
                size="small"
                onRow={(record) => ({
                    onClick: () => handleFolderClick(record),
                    style: { cursor: 'pointer' }
                })}
                scroll={{ y: 300 }}
            />

            <div style={{ marginTop: 16 }}>
                当前选择: <b>{currentFolder.name}</b>
            </div>
        </Modal>
    );
}
