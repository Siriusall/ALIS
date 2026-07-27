import { createContext, FormEvent, ReactNode, useContext, useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { getToken, readApi, setToken, writeApi } from "./api";

type User = { id: number; username: string; created_at?: string };
type AuthValue = { user: User | null; loading: boolean; login: (u: string, p: string) => Promise<void>; register: (u: string, p: string) => Promise<void>; logout: () => void };
const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null); const [loading, setLoading] = useState(true);
  useEffect(() => { let active=true; if (!getToken()) { setLoading(false); return; } readApi("/api/auth/me").then(x=>active&&setUser(x as User)).catch(()=>setToken(null)).finally(()=>active&&setLoading(false)); return()=>{active=false}; },[]);
  useEffect(()=>{const expired=()=>{setToken(null);setUser(null)};window.addEventListener("alis:session-expired",expired);return()=>window.removeEventListener("alis:session-expired",expired)},[]);
  async function authenticate(path:string, username:string,password:string){const result=await writeApi(path,{username,password});setToken(String(result.access_token));setUser(result.user as User);}
  return <AuthContext.Provider value={{user,loading,login:(u,p)=>authenticate("/api/auth/login",u,p),register:(u,p)=>authenticate("/api/auth/register",u,p),logout:()=>{setToken(null);setUser(null)}}}>{children}</AuthContext.Provider>;
}
export function useAuth(){const value=useContext(AuthContext);if(!value)throw new Error("AuthProvider missing");return value;}
export function RequireAuth({children}:{children:ReactNode}){const {user,loading}=useAuth();if(loading)return <div className="auth-loading">正在验证会话</div>;return user?<>{children}</>:<Navigate to="/login" replace/>;}

export function AuthPage({ mode }:{mode:"login"|"register"}){
  const auth=useAuth();const navigate=useNavigate();const [username,setUsername]=useState("");const [password,setPassword]=useState("");const [error,setError]=useState("");const [busy,setBusy]=useState(false);
  useEffect(()=>{if(auth.user)navigate("/overview",{replace:true})},[auth.user,navigate]);
  async function submit(event:FormEvent){event.preventDefault();setError("");if(!username.trim()||password.length<6){setError("请输入用户名和至少 6 位密码");return}setBusy(true);try{await (mode==="login"?auth.login:auth.register)(username.trim(),password);navigate("/overview",{replace:true})}catch(e:any){setError(String(e?.message||"认证失败"))}finally{setBusy(false)}}
  return <main className="auth-page"><section className="auth-panel"><div className="brand auth-brand"><div>AL</div><span><strong>ALIS</strong><small>模拟投资控制台</small></span></div><h1>{mode==="login"?"登录":"创建本机账户"}</h1><p>访问账户、策略和运行证据。</p><form onSubmit={submit}><label>用户名<input value={username} onChange={e=>setUsername(e.target.value)} autoComplete="username"/></label><label>密码<input type="password" value={password} onChange={e=>setPassword(e.target.value)} autoComplete={mode==="login"?"current-password":"new-password"}/></label>{error&&<div className="auth-error">{error}</div>}<button disabled={busy}>{busy?"正在验证":mode==="login"?"登录":"注册"}</button></form><button className="auth-switch" onClick={()=>navigate(mode==="login"?"/register":"/login")}>{mode==="login"?"创建账户":"返回登录"}</button></section></main>;
}
