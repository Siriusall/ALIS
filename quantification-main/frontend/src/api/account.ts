import { api } from "./client";

export interface AccountInfo {
  balance: number;
  position_value: number;
  assets: number;
}

export async function fetchAccount(): Promise<AccountInfo> {
  const { data } = await api.get<AccountInfo>("/api/account");
  return data;
}

export async function topUp(amount: number): Promise<AccountInfo> {
  const { data } = await api.post<AccountInfo>("/api/account/topup", { amount });
  return data;
}

export async function withdraw(amount: number): Promise<AccountInfo> {
  const { data } = await api.post<AccountInfo>("/api/account/withdraw", { amount });
  return data;
}

export async function setBalance(balance: number): Promise<AccountInfo> {
  const { data } = await api.put<AccountInfo>("/api/account", { balance });
  return data;
}
