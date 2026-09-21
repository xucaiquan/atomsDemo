/**
 * 认证上下文（设计文档 2026-09-20 S2）。
 *
 * 三态模型：loading / authenticated / anonymous。
 * - `client.auth.me()` 未返回前**不得**假定未登录，也不得自动跳转登录；
 * - 匿名可用是产品前提（FR-014）：登录入口是顶栏按钮，绝不自动 toLogin；
 * - 业务接口失败（限流/余额等）不触发登录跳转，只有本文件的显式 login() 才会。
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';
import { createClient } from '@metagptx/web-sdk';
import { clearAnonKey } from '@/lib/atoms';

const client = createClient();

export interface AuthUser {
  id: string;
  email?: string;
  name?: string;
}

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface AuthContextType {
  user: AuthUser | null;
  status: AuthStatus;
  /** 跳转平台登录页（回调后回到 /auth/callback 再回首页）。 */
  login: () => void;
  /** 退出登录；调用方负责清空工作区状态（见 Index 的身份切换重置）。 */
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}

function normalizeUser(raw: unknown): AuthUser | null {
  const data = raw as {
    id?: string;
    sub?: string;
    user_id?: string;
    email?: string;
    name?: string;
    nickname?: string;
  } | null;
  const id = data?.id || data?.sub || data?.user_id;
  if (!id) return null;
  return { id: String(id), email: data?.email, name: data?.name || data?.nickname };
}

export const AuthProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');

  // 只在挂载时探测一次登录态；me() 失败即匿名，不重试不跳转。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await client.auth.me();
        if (cancelled) return;
        const normalized = normalizeUser(res?.data);
        if (normalized) {
          setUser(normalized);
          setStatus('authenticated');
        } else {
          setStatus('anonymous');
        }
      } catch {
        if (!cancelled) setStatus('anonymous');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(() => {
    void client.auth.toLogin();
  }, []);

  const logout = useCallback(async () => {
    try {
      await client.auth.logout();
    } catch {
      /* 平台登出失败也要回到匿名态 */
    }
    clearAnonKey(); // 登录↔匿名切换时清掉浏览器里的匿名标识
    setUser(null);
    setStatus('anonymous');
  }, []);

  const value: AuthContextType = { user, status, login, logout };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};
