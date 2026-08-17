import { apiGet, apiSend, setCsrfToken } from './client';
import type { UserAccount } from '../types/kubernetes';
export async function getCurrentUser(){const user=await apiGet<UserAccount>('/api/auth/me');setCsrfToken(user.csrf??'');return user;}
export async function login(username:string,password:string){const user=await apiSend<UserAccount>('/api/auth/login','POST',{username,password});setCsrfToken(user.csrf??'');return user;}
export const logout=()=>apiSend<{ok:boolean}>('/api/auth/logout','POST',{});
export const changePassword=(currentPassword:string,newPassword:string,repeatPassword=newPassword)=>apiSend<{ok:boolean}>('/api/auth/change-password','POST',{currentPassword,newPassword,repeatPassword});
export const getUsers=()=>apiGet<UserAccount[]>('/api/users');
export const createUser=(username:string)=>apiSend<{user:UserAccount;temporaryPassword:string}>('/api/users','POST',{username});
export const resetUserPassword=(id:string)=>apiSend<{ok:boolean;temporaryPassword:string}>(`/api/users/${encodeURIComponent(id)}/reset-password`,'POST',{});
export const getPreferences=()=>apiGet<Record<string,string>>('/api/preferences');
export const savePreferences=(data:Record<string,string>)=>apiSend<{ok:boolean}>('/api/preferences','PUT',data);
