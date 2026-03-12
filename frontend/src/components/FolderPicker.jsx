import { useState, useEffect } from 'react'
import { Cascader, Space, message } from 'antd'
import { FolderOutlined } from '@ant-design/icons'
import { fileApi } from '../services/api'

/**
 * 级联目录选择器组件
 * 类似 xinyue-search 的目录选择效果
 */
export default function FolderPicker({ accountId, value, onChange, placeholder = "选择目录", disabled }) {
    const [options, setOptions] = useState([])
    const [loading, setLoading] = useState(false)

    // 初始化根目录
    useEffect(() => {
        if (accountId) {
            loadRootFolders()
        } else {
            setOptions([])
        }
    }, [accountId])

    const loadRootFolders = async () => {
        setLoading(true)
        try {
            const { data } = await fileApi.getList(accountId, '0')
            const folders = data
                .filter(f => f.is_dir)
                .map(f => ({
                    value: f.fid,
                    label: f.name,
                    isLeaf: false  // 假设都有子目录，点击时再加载
                }))

            // 添加根目录选项
            setOptions([
                { value: '0', label: '根目录', isLeaf: false, children: folders.length > 0 ? folders : undefined }
            ])
        } catch (error) {
            console.error('加载目录失败:', error)
            message.error('加载目录失败')
        } finally {
            setLoading(false)
        }
    }

    // 动态加载子目录
    const loadSubFolders = async (selectedOptions) => {
        const targetOption = selectedOptions[selectedOptions.length - 1]
        targetOption.loading = true

        try {
            const { data } = await fileApi.getList(accountId, targetOption.value)
            const folders = data
                .filter(f => f.is_dir)
                .map(f => ({
                    value: f.fid,
                    label: f.name,
                    isLeaf: false
                }))

            targetOption.loading = false
            targetOption.children = folders.length > 0 ? folders : []

            // 如果没有子目录，标记为叶子节点
            if (folders.length === 0) {
                targetOption.isLeaf = true
            }

            setOptions([...options])
        } catch (error) {
            targetOption.loading = false
            message.error('加载子目录失败')
        }
    }

    const handleChange = (value, selectedOptions) => {
        if (onChange && selectedOptions && selectedOptions.length > 0) {
            // 构建路径字符串
            const path = selectedOptions.map(opt => opt.label).join('/')
            const fid = value[value.length - 1]
            onChange({
                fid,
                path: path === '根目录' ? '/' : '/' + path.replace('根目录/', '') + '/'
            })
        }
    }

    // 显示渲染
    const displayRender = (labels) => {
        return labels.map((label, i) => (
            <span key={i}>
                {i > 0 && ' / '}
                {label}
            </span>
        ))
    }

    return (
        <Cascader
            options={options}
            loadData={loadSubFolders}
            onChange={handleChange}
            changeOnSelect
            displayRender={displayRender}
            placeholder={placeholder}
            disabled={disabled || !accountId}
            loading={loading}
            style={{ width: '100%' }}
            expandTrigger="click"
            suffixIcon={<FolderOutlined />}
        />
    )
}
