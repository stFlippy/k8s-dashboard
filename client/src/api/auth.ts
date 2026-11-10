import { apiGet, apiSend, setCsrfToken } from './client';
import type { UserAccount } from '../types/kubernetes';
export async function getCurrentUser(){const user=await apiGet<UserAccount>('/api/auth/me');setCsrfToken(user.csrf??'');return user;}
export async function login(username:string,password:string){const user=await apiSend<UserAccount>('/api/auth/login','POST',{username,password});setCsrfToken(user.csrf??'');return user;}
export const logout=()=>apiSend<{ok:boolean}>('/api/auth/logout','POST',{});
export const getUsers=()=>apiGet<UserAccount[]>('/api/users');
export const setUserAdministrator=(id:string,isAdmin:boolean)=>apiSend<{ok:boolean}>(`/api/users/${encodeURIComponent(id)}/administrator`,'PUT',{isAdmin});
export interface SystemStatus { databaseBytes: number; auditRetentionDays: number; cpuPercent: number | null; memoryTotalBytes: number; memoryUsedBytes: number; diskTotalBytes: number; diskUsedBytes: number; recent: Array<{ id: string; operation: string; cluster_id: string | null; namespace: string | null; kind: string; name: string | null; uid: string | null; state: string; http_status: number | null; requested_at: string }> }
export const getSystemStatus=()=>apiGet<SystemStatus>('/api/system/status');
export const getPreferences=()=>apiGet<Record<string,string>>('/api/preferences');
export const savePreferences=(data:Record<string,string>)=>apiSend<{ok:boolean}>('/api/preferences','PUT',data);
