/**
 * 旧 Axios 认证封装已按设计文档 2026-09-20 S2.1 移除。
 *
 * 认证统一改走平台 Web SDK（`client.auth.me / toLogin / logout`），
 * 见 src/contexts/AuthContext.tsx。此文件保留为空模块仅为兼容历史引用，
 * 新代码不得再从这里导入任何内容。
 */
export {};
